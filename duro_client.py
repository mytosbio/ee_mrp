"""
Minimal client for the Duro PLM GraphQL API.

Pulls a board's BOM at a given revision and writes it out as a CSV in the
same shape as the hand-exported files in boms/, named
"{PN}-{REV} {Name} BOM.csv" (e.g. "950-00015-3 StepperController BOM.csv"),
so mrp_report.py picks it up unchanged.

A component is looked up by its CPN (e.g. "950-00015") and the revision by
its revisionValue (e.g. "3"), taken from the component's revision history --
i.e. the frozen snapshot of that revision, not whatever the component looks
like now. With no revision given, the last released revision is used.

Each BOM line's Manufacturer/MPN comes from the child component's primary
source, falling back to its first listed manufacturer. Duro has no notion of
our "Status" column (Critical/Connector/Generic/...), which mrp_report.py
needs to bucket parts, so it's carried over by MPN from any existing BOM
file for the same PN in the output directory (same revision preferred).
Lines it can't fill are left blank and reported, to be filled in by hand.

Requires a DURO_API_KEY environment variable, generated in Duro under
Settings > Integrations. DURO_API_URL overrides the endpoint if your
workspace lives somewhere other than the default.
"""

import csv
import os
import re
import sys
from pathlib import Path

import requests

DEFAULT_API_URL = "https://mfg.duro.app/graphql"
REQUEST_TIMEOUT = 30
SEARCH_FIRST = 20

BOMS_DIR = Path(__file__).resolve().parent / "boms"

CSV_COLUMNS = [
    "Line #",
    "CPN",
    "Name",
    "Description",
    "Reference Designators",
    "Quantity",
    "Manufacturer",
    "Manufacturer Part Number",
    "Status",
]

_FIND_COMPONENT_QUERY = """
query FindComponent($search: SearchFields, $first: Int) {
  components(search: $search) {
    connection(first: $first) {
      edges {
        node {
          id
          name
          revisionValue
          status
          cpn { displayValue }
          lastReleaseRevision { id revisionValue }
          revisionHistory { id revisionValue status }
        }
      }
    }
  }
}
"""

_CHILDREN_FIELDS = """
  id
  name
  revisionValue
  status
  cpn { displayValue }
  children {
    itemNumber
    quantity
    refDes
    component {
      name
      description
      cpn { displayValue }
      primarySource { manufacturer mpn }
      manufacturers { name mpn { key } }
    }
  }
"""

_REVISION_BOM_QUERY = f"""
query RevisionBom($ids: [ID]) {{
  componentRevisionsByIds(ids: $ids) {{ {_CHILDREN_FIELDS} }}
}}
"""

_CURRENT_BOM_QUERY = f"""
query CurrentBom($ids: [ID]) {{
  componentsByIds(ids: $ids) {{ {_CHILDREN_FIELDS} }}
}}
"""


class DuroError(Exception):
    """Raised when the API key is missing, a request fails, or a lookup can't be resolved."""


def credentials_present():
    return bool(os.environ.get("DURO_API_KEY"))


def _normalize(text):
    # Same punctuation/case-insensitive comparison as the other clients.
    return re.sub(r"[^A-Za-z0-9]", "", text or "").upper()


def _query(query, variables):
    api_key = os.environ.get("DURO_API_KEY")
    if not api_key:
        raise DuroError("DURO_API_KEY is not set")

    try:
        response = requests.post(
            os.environ.get("DURO_API_URL") or DEFAULT_API_URL,
            headers={"apiToken": api_key, "Content-Type": "application/json"},
            json={"query": query, "variables": variables},
            timeout=REQUEST_TIMEOUT,
        )
        # Duro answers GraphQL validation errors with a 400 and the reason in
        # the body, which raise_for_status alone would throw away.
        if response.status_code == 400:
            raise DuroError(f"bad request: {response.text[:500]}")
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise DuroError(f"request failed: {exc}") from exc
    except ValueError as exc:
        raise DuroError(f"response was not JSON: {exc}") from exc

    if payload.get("errors"):
        raise DuroError(f"GraphQL error: {payload['errors']}")
    return payload["data"]


def _cpn(node):
    return ((node or {}).get("cpn") or {}).get("displayValue") or ""


def find_component(pn):
    """
    Returns the Duro component whose CPN is exactly `pn` (with its revision
    history), raising DuroError if there's no exact match.
    """
    data = _query(_FIND_COMPONENT_QUERY, {"search": {"cpn": pn}, "first": SEARCH_FIRST})
    nodes = [edge["node"] for edge in data["components"]["connection"]["edges"] or []]

    # The search may be a substring match, so insist on the exact CPN.
    matches = [node for node in nodes if _cpn(node).strip().upper() == pn.strip().upper()]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise DuroError(f"{len(matches)} components have CPN {pn}")

    near = ", ".join(sorted({_cpn(node) for node in nodes})) or "none"
    raise DuroError(f"no component with CPN {pn} (search returned: {near})")


def _pick_revision(component, rev):
    """
    Returns (query, id) for fetching the requested revision's children.
    """
    if rev is None:
        released = component.get("lastReleaseRevision")
        if not released:
            raise DuroError(f"{_cpn(component)} has no released revision; pass REV explicitly")
        return _REVISION_BOM_QUERY, released["id"]

    for revision in component.get("revisionHistory") or []:
        if (revision.get("revisionValue") or "").upper() == rev.upper():
            return _REVISION_BOM_QUERY, revision["id"]

    # The working revision may not have a history entry until it's released.
    if (component.get("revisionValue") or "").upper() == rev.upper():
        print(f"  WARNING: {_cpn(component)} rev {rev} is the working revision; it may have unreleased edits.")
        return _CURRENT_BOM_QUERY, component["id"]

    known = [r.get("revisionValue") for r in component.get("revisionHistory") or []]
    known.append(component.get("revisionValue"))
    available = ", ".join(sorted({r for r in known if r}))
    raise DuroError(f"{_cpn(component)} has no revision {rev} (available: {available or 'none'})")


def _manufacturer_and_mpn(component):
    source = component.get("primarySource") or {}
    if source.get("mpn"):
        return (source.get("manufacturer") or "").strip(), source["mpn"].strip()

    for manufacturer in component.get("manufacturers") or []:
        mpn = ((manufacturer.get("mpn") or {}).get("key") or "").strip()
        if mpn:
            return (manufacturer.get("name") or "").strip(), mpn
    return "", ""


def _format_quantity(value):
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else str(value)


def get_bom(pn, rev=None):
    """
    Returns the BOM for component `pn` at revision `rev` (last released
    revision if None) as a dict:

        {"cpn", "name", "revision", "status", "lines": [{column: value}, ...]}

    where each line has the CSV_COLUMNS keys (Status left blank).

    Raises DuroError if the API key is missing, a request fails, or the
    component/revision can't be found.
    """
    component = find_component(pn)
    query, node_id = _pick_revision(component, rev)

    data = _query(query, {"ids": [node_id]})
    nodes = next(iter(data.values())) or []
    if not nodes:
        raise DuroError(f"Duro returned nothing for {pn} (id {node_id})")
    node = nodes[0]

    # An empty file in boms/ would silently zero this board's demand in mrp_report.py.
    if not node.get("children"):
        raise DuroError(f"{pn} rev {node.get('revisionValue') or rev} has no BOM lines in Duro")

    # Duro item numbers when set, otherwise the order Duro returned them in.
    children = sorted(
        enumerate(node.get("children") or []),
        key=lambda pair: (pair[1].get("itemNumber") is None, pair[1].get("itemNumber") or 0, pair[0]),
    )

    lines = []
    for index, (_, child) in enumerate(children, start=1):
        part = child.get("component") or {}
        manufacturer, mpn = _manufacturer_and_mpn(part)
        lines.append({
            "Line #": child.get("itemNumber") or index,
            "CPN": _cpn(part),
            "Name": part.get("name") or "",
            "Description": part.get("description") or "",
            "Reference Designators": child.get("refDes") or "",
            "Quantity": _format_quantity(child.get("quantity")),
            "Manufacturer": manufacturer,
            "Manufacturer Part Number": mpn,
            "Status": "",
        })

    return {
        "cpn": _cpn(node) or _cpn(component),
        "name": node.get("name") or component.get("name") or "",
        "revision": node.get("revisionValue") or rev or "",
        "status": node.get("status") or "",
        "lines": lines,
    }


def bom_filename(bom):
    # Keep the name filesystem-safe; it's the only free-text part.
    name = re.sub(r'[\\/:*?"<>|]+', "_", bom["name"]).strip() or "Unnamed"
    return f"{bom['cpn']}-{bom['revision']} {name} BOM.csv"


def _existing_boms(out_dir, cpn, revision):
    """
    Existing BOM files for `cpn` in `out_dir`, the same revision first.
    """
    same_rev = sorted(out_dir.glob(f"{cpn}-{revision} *.csv"))
    others = sorted(set(out_dir.glob(f"{cpn}-*.csv")) - set(same_rev), reverse=True)
    return same_rev, others


def _load_statuses(paths):
    """
    Maps normalized MPN -> Status across `paths`, earlier files taking
    precedence.
    """
    statuses = {}
    for path in paths:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                row = {(k or "").strip(): v for k, v in row.items()}
                mpn = _normalize(row.get("Manufacturer Part Number"))
                status = (row.get("Status") or "").strip()
                if mpn and status:
                    statuses.setdefault(mpn, status)
    return statuses


def write_bom(bom, out_dir=BOMS_DIR):
    """
    Writes `bom` (from get_bom) to `out_dir`, filling Status from existing
    BOMs for the same PN. Returns (path, lines with no Status).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / bom_filename(bom)

    same_rev, others = _existing_boms(out_dir, bom["cpn"], bom["revision"])
    # Read before writing, since `path` itself may be one of them.
    statuses = _load_statuses(same_rev + others)

    unclassified = []
    for line in bom["lines"]:
        line["Status"] = statuses.get(_normalize(line["Manufacturer Part Number"]), "")
        if not line["Status"]:
            unclassified.append(line)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(bom["lines"])

    # mrp_report.find_file picks an arbitrary match if a PN-REV has several files.
    for stale in same_rev:
        if stale != path:
            print(f"  WARNING: {stale.name} is also a BOM for {bom['cpn']}-{bom['revision']}; "
                  f"remove one so mrp_report.py doesn't pick the wrong file.")

    return path, unclassified


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Pull data from Duro PLM.")
    parser.add_argument("--bom", nargs="+", metavar=("PN", "REV"), required=True,
                        help="Write the BOM for board PN (e.g. 950-00015) at revision REV "
                             "(default: last released revision)")
    parser.add_argument("-o", "--out-dir", default=BOMS_DIR, type=Path,
                        help=f"Directory to write the BOM CSV to (default: {BOMS_DIR})")
    args = parser.parse_args()

    if len(args.bom) > 2:
        parser.error("--bom takes a PN and an optional REV")
    pn, rev = args.bom[0], (args.bom[1] if len(args.bom) == 2 else None)

    try:
        bom = get_bom(pn, rev)
    except DuroError as exc:
        sys.exit(f"Error: {exc}")

    path, unclassified = write_bom(bom, args.out_dir)
    print(f"{bom['cpn']} rev {bom['revision']} ({bom['name']}, {bom['status'] or 'unknown status'}): "
          f"{len(bom['lines'])} lines -> {path}")

    missing_mpn = [line for line in bom["lines"] if not line["Manufacturer Part Number"]]
    if missing_mpn:
        print(f"  WARNING: {len(missing_mpn)} line(s) have no manufacturer part number: "
              + ", ".join(line["CPN"] or line["Name"] for line in missing_mpn))
    if unclassified:
        print(f"  NOTE: {len(unclassified)} line(s) have no Status to carry over; fill them in by hand: "
              + ", ".join(line["Manufacturer Part Number"] or line["CPN"] for line in unclassified))

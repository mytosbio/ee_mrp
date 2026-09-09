"""
Minimal client for Boltline's in-house inventory GraphQL API.

Looks up on-hand stock by (Manufacturer, Manufacturer Part Number). There's
no structured manufacturer/MPN field to filter on here -- PartInstance and
InventoryLot both have manufacturer/mfgPartNumber fields, but in practice
they're only sometimes populated (by certain receiving flows); the org's
actual convention is to put "{Manufacturer} {MPN}" as the free-text `Part`
name (e.g. "Texas Instruments LMR36015BRNXT"), searched via `searchParts`.

So matching here means: full-text search on the MPN (the more distinctive
of the two, and the one DigiKey/Mouser also match on -- manufacturer name
isn't used, to tolerate spelling differences like "TI" vs "Texas
Instruments"), then keep only candidates whose name actually contains the
MPN, as a safety net against the search returning loosely-related results.
`Part.quantityOnHand` gives the on-hand total directly (Boltline aggregates
this from AVAILABLE part instances server-side).

Requires a BOLTLINE_API_KEY environment variable, generated at
https://app.boltline.com/me
"""

import os
import re
import sys

import requests

API_URL = "https://api.boltline.com/graphql"
REQUEST_TIMEOUT = 10
SEARCH_TAKE = 20

_SEARCH_QUERY = """
query FindParts($search: String!, $take: Float!) {
  searchParts(search: $search, take: $take) {
    name
    quantityOnHand
  }
}
"""


class BoltlineError(Exception):
    """Raised when the API key is missing or a request to the API fails."""


def credentials_present():
    return bool(os.environ.get("BOLTLINE_API_KEY"))


def _normalize(text):
    # Mirrors digikey_client's punctuation/case-insensitive comparison, so a
    # part typed as "LMR36015-BRNXT" in one system still matches "LMR36015BRNXT"
    # in the other.
    return re.sub(r"[^A-Za-z0-9]", "", text or "").upper()


def _query(query, variables):
    api_key = os.environ.get("BOLTLINE_API_KEY")
    if not api_key:
        raise BoltlineError("BOLTLINE_API_KEY is not set")

    try:
        response = requests.post(
            API_URL,
            headers={
                "Authorization": f"Key {api_key}",
                "Content-Type": "application/json",
            },
            json={"query": query, "variables": variables},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise BoltlineError(f"request failed: {exc}") from exc

    payload = response.json()
    if "errors" in payload:
        raise BoltlineError(f"GraphQL error: {payload['errors']}")
    return payload["data"]


def _find_matching_parts(mpn):
    data = _query(_SEARCH_QUERY, {"search": mpn, "take": SEARCH_TAKE})
    target = _normalize(mpn)
    return [part for part in data["searchParts"] if target in _normalize(part.get("name"))]


def get_stock(manufacturer, mpn):
    """
    Returns total on-hand quantity (Part.quantityOnHand, summed across every
    matching Part) for the given manufacturer part number, as an int (0 if
    no match).

    Raises BoltlineError if the API key is missing or a request fails.
    """
    if not mpn:
        return 0

    matches = _find_matching_parts(mpn)
    return int(sum(part["quantityOnHand"] for part in matches))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Look up Boltline in-house stock for a single part.")
    parser.add_argument("-m", "--manufacturer", default="", help="Manufacturer name (not used for matching -- Boltline lookup matches on MPN only)")
    parser.add_argument("-p", "--mpn", required=True, help="Manufacturer part number")
    args = parser.parse_args()

    try:
        matches = _find_matching_parts(args.mpn)
    except BoltlineError as exc:
        sys.exit(f"Error: {exc}")

    if matches:
        total = int(sum(part["quantityOnHand"] for part in matches))
        names = ", ".join(repr(part["name"]) for part in matches)
        print(f"{args.mpn}: {total} in stock (matched {names})")
    else:
        print(f"{args.mpn}: no match in Boltline")

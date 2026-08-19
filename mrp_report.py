"""
Generates critical-component and connector demand reports from a 6-month
forecast and per-board BOM files.

A forecast row's Part Number may refer either to a board (a BOM file in
boms/) or a system (a file in systems/ listing child systems and/or boards
with a quantity-per-parent). Systems are resolved recursively -- a system
may contain other systems -- until boards are reached.

For each status of interest ("Critical" and "Connector"), sums, across all
boards reachable from the forecast, the total quantity required of each
unique (Manufacturer, Manufacturer Part Number) component:

    total quantity = sum over boards of (effective board quantity * populated quantity per board)

where a board's effective quantity is the forecast quantity multiplied by
the quantity-per-parent of every system in the chain above it. Components
shared by multiple boards or systems are combined into a single line.
"""

import csv
import sys
import time
from collections import defaultdict
from pathlib import Path

import digikey_client
import mouser_client

# Stays under Mouser's per-minute rate limit (their retry-after cooldown is
# a much costlier fallback if we blow through it instead).
MOUSER_REQUEST_INTERVAL = 2.5

BASE_DIR = Path(__file__).resolve().parent
FORECAST_FILE = BASE_DIR / "forecase_6m.csv"
BOMS_DIR = BASE_DIR / "boms"
SYSTEMS_DIR = BASE_DIR / "systems"

# Maps a raw BOM "Status" value (lowercased) to the report bucket it belongs to.
# Handles the "Conector" typo found in one of the BOM files.
STATUS_BUCKETS = {
    "critical": "critical",
    "connector": "connector",
    "conector": "connector",
}

REPORTS = {
    "critical": BASE_DIR / "critical_components_report.csv",
    "connector": BASE_DIR / "connectors_report.csv",
}

# Not committed by the GitHub Action -- regenerated fresh each run, so
# there's no history worth keeping for it the way there is for the reports
# above.
SHORTAGE_REPORT_FILE = BASE_DIR / "stock_shortage_report.csv"


def normalize_header(name):
    return name.strip() if name else name


def load_forecast(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            part_number = (row.get("Part Number") or "").strip()
            if not part_number:
                continue
            board_name = (row.get("Board Name") or "").strip()
            qty_raw = (row.get("Stock Predicted") or "").strip()
            try:
                forecast_qty = int(float(qty_raw))
            except ValueError:
                print(f"  WARNING: skipping forecast row with bad quantity: {row}")
                continue
            rows.append((part_number, board_name, forecast_qty))
    return rows


def find_file(directory, part_number):
    matches = list(directory.glob(f"{part_number} *.csv"))
    if not matches:
        matches = list(directory.glob(f"{part_number}*.csv"))
    return matches[0] if matches else None


def load_system(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            part_number = (row.get("Part Number") or "").strip()
            if not part_number:
                continue
            name = (row.get("Name") or "").strip()
            qty_raw = (row.get("Quantity") or "").strip()
            try:
                qty = int(float(qty_raw))
            except ValueError:
                print(f"  WARNING: skipping system row with bad quantity in {path.name}: {row}")
                continue
            rows.append((part_number, name, qty))
    return rows


def expand(part_number, name, multiplier, totals, missing_ident, visited):
    if part_number in visited:
        chain = " -> ".join(list(visited) + [part_number])
        print(f"  WARNING: circular system reference detected ({chain}); skipping.")
        return

    system_path = find_file(SYSTEMS_DIR, part_number)
    if system_path is not None:
        child_visited = visited | {part_number}
        for child_part, child_name, child_qty in load_system(system_path):
            expand(child_part, child_name, multiplier * child_qty, totals, missing_ident, child_visited)
        return

    bom_path = find_file(BOMS_DIR, part_number)
    if bom_path is not None:
        process_bom(bom_path, multiplier, totals, missing_ident)
        return

    print(f"  WARNING: no system or BOM file found for {part_number} ({name})")


def parse_quantity(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def process_bom(path, forecast_qty, totals, missing_ident):
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = {normalize_header(h) for h in reader.fieldnames or []}
        required = {"Manufacturer", "Manufacturer Part Number", "Quantity", "Status"}
        missing = required - headers
        if missing:
            print(f"  WARNING: {path.name} is missing columns {missing}; skipping file.")
            return

        for row in reader:
            row = {normalize_header(k): v for k, v in row.items() if k is not None}
            status_raw = (row.get("Status") or "").strip()
            bucket = STATUS_BUCKETS.get(status_raw.lower())
            if bucket is None:
                continue

            populated_qty = parse_quantity(row.get("Quantity"))
            if populated_qty is None:
                print(f"  WARNING: bad Quantity in {path.name}: {row}")
                continue

            manufacturer = (row.get("Manufacturer") or "").strip()
            mpn = (row.get("Manufacturer Part Number") or "").strip()

            if not manufacturer or not mpn:
                name = (row.get("Name") or "").strip()
                missing_ident.append((path.name, name, manufacturer, mpn))

            key = (manufacturer, mpn)
            totals[bucket][key] += forecast_qty * populated_qty


def _print_progress(i, total, label):
    # A live-updating single line in a terminal; discrete lines when piped
    # to a file/CI log, where \r just leaves garbage instead of updating.
    if sys.stdout.isatty():
        print(f"\r  [{i}/{total}] {label}" + " " * 20, end="", flush=True)
        if i == total:
            print()
    else:
        print(f"  [{i}/{total}] {label}")


def fetch_digikey_stock(totals):
    """
    Looks up DigiKey stock for every unique (Manufacturer, MPN) key across all
    buckets. Returns a dict of key -> quantity available (None if not found
    on DigiKey). If credentials are missing, or a request fails, lookups are
    abandoned for the rest of the run and the remaining keys are left absent
    (reported as blank in the CSV).
    """
    stock = {}
    if not digikey_client.credentials_present():
        print("  NOTE: DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET not set; skipping DigiKey stock lookup.")
        return stock

    keys = list({key for bucket_totals in totals.values() for key in bucket_totals})
    not_found = 0
    for i, (manufacturer, mpn) in enumerate(keys, start=1):
        _print_progress(i, len(keys), f"DigiKey: {mpn}")
        try:
            stock[(manufacturer, mpn)] = digikey_client.get_stock(manufacturer, mpn)
        except digikey_client.DigiKeyError as exc:
            print(f"  WARNING: DigiKey lookup aborted: {exc}")
            break
        if stock[(manufacturer, mpn)] is None:
            not_found += 1

    if not_found:
        print(f"  NOTE: {not_found} part(s) not found on DigiKey (left blank).")

    return stock


def fetch_mouser_stock(totals):
    """
    Same as fetch_digikey_stock, but against the Mouser Search API. Unlike
    DigiKey (where auth is a single up-front token call), Mouser failures
    happen per-part, so an isolated blip shouldn't sink the whole run --
    only abort early once failures start stacking up consecutively.
    """
    stock = {}
    if not mouser_client.credentials_present():
        print("  NOTE: MOUSER_API_KEY not set; skipping Mouser stock lookup.")
        return stock

    keys = list({key for bucket_totals in totals.values() for key in bucket_totals})
    not_found = 0
    consecutive_failures = 0
    for i, (manufacturer, mpn) in enumerate(keys, start=1):
        _print_progress(i, len(keys), f"Mouser: {mpn}")
        if i > 1:
            time.sleep(MOUSER_REQUEST_INTERVAL)
        try:
            stock[(manufacturer, mpn)] = mouser_client.get_stock(manufacturer, mpn)
        except mouser_client.MouserError as exc:
            print(f"  WARNING: Mouser lookup failed for {mpn!r}: {exc}")
            consecutive_failures += 1
            if consecutive_failures >= 3:
                print("  WARNING: 3 consecutive Mouser failures; aborting remaining lookups.")
                break
            continue
        consecutive_failures = 0
        if stock[(manufacturer, mpn)] is None:
            not_found += 1

    if not_found:
        print(f"  NOTE: {not_found} part(s) not found on Mouser (left blank).")

    return stock


def total_stock(stock_providers, key):
    # Unknown per-supplier stock (missing credentials, lookup failure, not
    # carried) counts as 0 rather than being excluded -- otherwise a part
    # nobody could look up would silently vanish from the shortage report
    # instead of correctly showing as fully short.
    return sum(stock.get(key) or 0 for _, stock in stock_providers)


def write_report(bucket, totals, path, stock_providers):
    rows = sorted(totals[bucket].items(), key=lambda kv: (kv[0][0].lower(), kv[0][1].lower()))
    header = ["Manufacturer", "Manufacturer Part Number", "Quantity"]
    header += [label for label, _ in stock_providers]
    header += ["Total Stock"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for key, qty in rows:
            manufacturer, mpn = key
            values = [stock.get(key) for _, stock in stock_providers]
            row = [manufacturer, mpn, qty]
            row += [v if v is not None else "" for v in values]
            row += [total_stock(stock_providers, key)]
            writer.writerow(row)
    print(f"Wrote {len(rows)} {bucket} component line(s) to {path.name}")


def write_shortage_report(totals, stock_providers, path):
    """
    Lists every critical/connector part whose combined stock across all
    suppliers falls short of the 6-month forecasted quantity.
    """
    rows = []
    for bucket, category in [("critical", "Critical"), ("connector", "Connector")]:
        for (manufacturer, mpn), qty in totals[bucket].items():
            available = total_stock(stock_providers, (manufacturer, mpn))
            if available < qty:
                rows.append((category, manufacturer, mpn, qty, available, qty - available))

    rows.sort(key=lambda r: (r[0], r[1].lower(), r[2].lower()))
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Category", "Manufacturer", "Manufacturer Part Number", "Quantity Required", "Total Stock", "Shortfall"])
        writer.writerows(rows)
    print(f"Wrote {len(rows)} shortage line(s) to {path.name}")


def main():
    if not FORECAST_FILE.exists():
        sys.exit(f"Forecast file not found: {FORECAST_FILE}")
    if not BOMS_DIR.exists():
        sys.exit(f"BOMs directory not found: {BOMS_DIR}")

    forecast_rows = load_forecast(FORECAST_FILE)

    totals = {"critical": defaultdict(int), "connector": defaultdict(int)}
    missing_ident = []

    for part_number, name, forecast_qty in forecast_rows:
        expand(part_number, name, forecast_qty, totals, missing_ident, frozenset())

    stock_providers = [
        ("DigiKey Stock", fetch_digikey_stock(totals)),
        ("Mouser Stock", fetch_mouser_stock(totals)),
    ]

    for bucket, path in REPORTS.items():
        write_report(bucket, totals, path, stock_providers)

    write_shortage_report(totals, stock_providers, SHORTAGE_REPORT_FILE)

    if missing_ident:
        print(f"\n{len(missing_ident)} matching line(s) had a blank Manufacturer and/or MPN (included with blank fields):")
        for bom_name, name, manufacturer, mpn in missing_ident:
            print(f"  {bom_name}: '{name}' -> Manufacturer='{manufacturer}' MPN='{mpn}'")


if __name__ == "__main__":
    main()

"""
Generates critical-component and connector demand reports from a 12-month
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
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
FORECAST_FILE = BASE_DIR / "forecase_12m.csv"
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


def write_report(bucket, totals, path):
    rows = sorted(totals[bucket].items(), key=lambda kv: (kv[0][0].lower(), kv[0][1].lower()))
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Manufacturer", "Manufacturer Part Number", "Quantity"])
        for (manufacturer, mpn), qty in rows:
            writer.writerow([manufacturer, mpn, qty])
    print(f"Wrote {len(rows)} {bucket} component line(s) to {path.name}")


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

    for bucket, path in REPORTS.items():
        write_report(bucket, totals, path)

    if missing_ident:
        print(f"\n{len(missing_ident)} matching line(s) had a blank Manufacturer and/or MPN (included with blank fields):")
        for bom_name, name, manufacturer, mpn in missing_ident:
            print(f"  {bom_name}: '{name}' -> Manufacturer='{manufacturer}' MPN='{mpn}'")


if __name__ == "__main__":
    main()

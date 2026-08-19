"""
Minimal client for the Mouser Search API v1.

Looks up available stock by (Manufacturer, Manufacturer Part Number) via
keyword search, since it's the best-documented endpoint for resolving an
arbitrary manufacturer part number to a Mouser listing.

Requires a MOUSER_API_KEY environment variable, issued from
https://www.mouser.com/api-hub/ for the Search API.
"""

import os
import re
import sys
import time

import requests

SEARCH_URL = "https://api.mouser.com/api/v1/search/keyword"
REQUEST_TIMEOUT = 10

# The free-tier key hits Mouser's per-minute cap partway through a full
# report run (~80 unique parts); retry a couple of times with a cooldown
# rather than losing the rest of the run to one busy minute.
RATE_LIMIT_RETRIES = 2
RATE_LIMIT_COOLDOWN = 20


class MouserError(Exception):
    """Raised when the API key is missing or a request to the API fails."""


def credentials_present():
    return bool(os.environ.get("MOUSER_API_KEY"))


def _normalize_pn(pn):
    # Unlike DigiKey, Mouser wants punctuation present -- it lists Molex
    # parts with dashes our BOMs omit (e.g. our "430450415" is their
    # "43045-0415"), but doesn't add a leading zero. Stripping punctuation
    # on both sides (no leading-zero handling) matches these without
    # weakening the match for parts that already agree.
    return re.sub(r"[^A-Za-z0-9]", "", pn or "").upper()


def _parse_availability(availability_in_stock):
    if not availability_in_stock:
        return None
    try:
        return int(availability_in_stock)
    except ValueError:
        return None


def _search_parts(mpn):
    api_key = os.environ.get("MOUSER_API_KEY")
    if not api_key:
        raise MouserError("MOUSER_API_KEY is not set")

    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            response = requests.post(
                SEARCH_URL,
                params={"apiKey": api_key},
                json={
                    "SearchByKeywordRequest": {
                        "keyword": mpn,
                        "records": 10,
                        "startingRecord": 0,
                        "searchOptions": "",
                        "searchWithYourSignUpLanguage": "",
                    }
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise MouserError(f"search failed for {mpn!r}: {exc}") from exc

        errors = (response.json().get("Errors") or []) if response.content else []
        rate_limited = any(e.get("Code") == "TooManyRequests" for e in errors)
        if rate_limited and attempt < RATE_LIMIT_RETRIES:
            time.sleep(RATE_LIMIT_COOLDOWN)
            continue
        break

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        # Scrub the API key: it rides along in the request URL, which
        # str(exc) includes, and this message ends up in logs/console output.
        message = str(exc).replace(api_key, "***")
        if response.text:
            message = f"{message} -- {response.text[:300]}"
        raise MouserError(f"search failed for {mpn!r}: {message}") from exc

    payload = response.json()
    if errors:
        raise MouserError(f"search failed for {mpn!r}: {errors}")

    return (payload.get("SearchResults") or {}).get("Parts") or []


def get_stock(manufacturer, mpn):
    """
    Returns the Mouser stock quantity for the given manufacturer part
    number as an int, or None if no matching product was found (or its
    availability couldn't be parsed as a number).

    Raises MouserError if the API key is missing or the request fails.
    """
    if not mpn:
        return None

    parts = _search_parts(mpn)

    target = _normalize_pn(mpn)
    for part in parts:
        if _normalize_pn(part.get("ManufacturerPartNumber")) == target:
            return _parse_availability(part.get("AvailabilityInStock"))

    return None


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Look up Mouser stock for a single part.")
    parser.add_argument("-m", "--manufacturer", default="", help="Manufacturer name (not used for matching -- Mouser's keyword search matches on MPN only)")
    parser.add_argument("-p", "--mpn", required=True, help="Manufacturer part number")
    args = parser.parse_args()

    try:
        parts = _search_parts(args.mpn)
    except MouserError as exc:
        sys.exit(f"Error: {exc}")

    target = _normalize_pn(args.mpn)
    match = next((p for p in parts if _normalize_pn(p.get("ManufacturerPartNumber")) == target), None)

    if match:
        stock = _parse_availability(match.get("AvailabilityInStock"))
        print(f"{args.mpn}: {stock} in stock (Manufacturer={match.get('Manufacturer')!r}, Mouser MPN={match.get('ManufacturerPartNumber')!r})")
    else:
        print(f"{args.mpn}: no match (even after normalizing punctuation)")
        if parts:
            print(f"  {len(parts)} candidate(s) returned by keyword search:")
            for p in parts:
                print(f"    {p.get('ManufacturerPartNumber')!r} (Manufacturer={p.get('Manufacturer')!r}, AvailabilityInStock={p.get('AvailabilityInStock')!r})")
        else:
            print("  no candidates returned at all")


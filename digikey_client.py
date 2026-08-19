"""
Minimal client for the DigiKey Product Information API v4.

Looks up available stock by (Manufacturer, Manufacturer Part Number). Search
is done via the keyword-search endpoint rather than the productdetails
endpoint, since the latter is keyed by DigiKey's own part number rather than
the manufacturer's.

Requires DIGIKEY_CLIENT_ID and DIGIKEY_CLIENT_SECRET environment variables,
issued from an app registered at https://developer.digikey.com against the
"Product Information V4" API.
"""

import os
import re
import sys
import time

import requests

PRODUCTION_HOST = "https://api.digikey.com"
SANDBOX_HOST = "https://sandbox-api.digikey.com"
REQUEST_TIMEOUT = 10


def _api_host():
    # DigiKey apps are sandbox-only until DigiKey approves them for
    # production, so this needs to be switchable while waiting on that.
    return SANDBOX_HOST if os.environ.get("DIGIKEY_SANDBOX") else PRODUCTION_HOST

_token = None
_token_expiry = 0.0


class DigiKeyError(Exception):
    """Raised when credentials are missing or a request to the API fails."""


def credentials_present():
    return bool(os.environ.get("DIGIKEY_CLIENT_ID")) and bool(os.environ.get("DIGIKEY_CLIENT_SECRET"))


def _normalize_pn(pn):
    # DigiKey lists Molex parts with dashes stripped and a leading zero
    # prepended (e.g. our "43045-0225" is their "0430450225"); normalizing
    # away punctuation and leading zeros on both sides matches these
    # without weakening the match for manufacturers that don't do this.
    return re.sub(r"[^A-Za-z0-9]", "", pn or "").upper().lstrip("0")


def _get_token():
    global _token, _token_expiry
    if _token and time.monotonic() < _token_expiry:
        return _token

    client_id = os.environ.get("DIGIKEY_CLIENT_ID")
    client_secret = os.environ.get("DIGIKEY_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise DigiKeyError("DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET are not set")

    try:
        response = requests.post(
            f"{_api_host()}/v1/oauth2/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise DigiKeyError(f"failed to fetch OAuth2 token: {exc}") from exc

    payload = response.json()
    _token = payload["access_token"]
    _token_expiry = time.monotonic() + payload.get("expires_in", 599) - 30
    return _token


def _search_products(mpn):
    token = _get_token()

    try:
        response = requests.post(
            f"{_api_host()}/products/v4/search/keyword",
            headers={
                "X-DIGIKEY-Client-Id": os.environ["DIGIKEY_CLIENT_ID"],
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={"Keywords": mpn, "Limit": 10},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise DigiKeyError(f"search failed for {mpn!r}: {exc}") from exc

    return response.json().get("Products") or []


def get_stock(manufacturer, mpn):
    """
    Returns the DigiKey QuantityAvailable for the given manufacturer part
    number as an int, or None if no matching product was found.

    Raises DigiKeyError if credentials are missing or the request fails
    (bad credentials, network error, rate limit, etc).
    """
    if not mpn:
        return None

    products = _search_products(mpn)

    target = _normalize_pn(mpn)
    for product in products:
        if _normalize_pn(product.get("ManufacturerProductNumber")) == target:
            return product.get("QuantityAvailable")

    return None


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Look up DigiKey stock for a single part.")
    parser.add_argument("-m", "--manufacturer", default="", help="Manufacturer name (not used for matching -- DigiKey's keyword search matches on MPN only)")
    parser.add_argument("-p", "--mpn", required=True, help="Manufacturer part number")
    args = parser.parse_args()

    try:
        products = _search_products(args.mpn)
    except DigiKeyError as exc:
        sys.exit(f"Error: {exc}")

    target = _normalize_pn(args.mpn)
    match = next((p for p in products if _normalize_pn(p.get("ManufacturerProductNumber")) == target), None)

    if match:
        manufacturer = (match.get("Manufacturer") or {}).get("Name")
        print(f"{args.mpn}: {match.get('QuantityAvailable')} in stock (Manufacturer={manufacturer!r}, DigiKey MPN={match.get('ManufacturerProductNumber')!r})")
    else:
        print(f"{args.mpn}: no match (even after normalizing punctuation/leading zeros)")
        if products:
            print(f"  {len(products)} candidate(s) returned by keyword search:")
            for p in products:
                manufacturer = (p.get("Manufacturer") or {}).get("Name")
                print(f"    {p.get('ManufacturerProductNumber')!r} (Manufacturer={manufacturer!r}, QuantityAvailable={p.get('QuantityAvailable')})")
        else:
            print("  no candidates returned at all")

#!/usr/bin/env python3
"""Fetch the E.ON Next account API key using a signed-in browser session.

E.ON Next retired email/password login for its Kraken API. Sign in at
https://www.eonnext.com, open the browser developer tools, select a request to
api.eonnext-kraken.energy in the Network tab, and copy the value of its
Authorization request header. Paste it when prompted; it is not echoed,
saved or sent anywhere except E.ON's API.

The key is printed once so it can be pasted into Home Assistant. With
--regenerate the script creates a new key when none exists, or replaces the
existing one with --regenerate --force; replacing it breaks any other client
that holds the old key.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import urllib.error
import urllib.request

API_URL = "https://api.eonnext-kraken.energy/v1/graphql/"

VIEWER_KEY = "query ViewerKey { viewer { liveSecretKey } }"
REGENERATE_KEY = "mutation RegenerateKey { regenerateSecretKey { key } }"


def graphql(token: str, operation: str, query: str) -> dict:
    request = urllib.request.Request(
        API_URL,
        data=json.dumps({"operationName": operation, "query": query}).encode(),
        headers={"Content-Type": "application/json", "Authorization": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.load(response)
    except urllib.error.HTTPError as err:
        try:
            body = json.load(err)
        except ValueError:
            raise RuntimeError(f"E.ON returned HTTP {err.code}") from None
    if not isinstance(body, dict):
        raise RuntimeError(f"E.ON returned an invalid {operation} response")
    errors = body.get("errors") or []
    if errors:
        codes = sorted(
            {
                str((error.get("extensions") or {}).get("errorCode", "unclassified"))
                for error in errors
                if isinstance(error, dict)
            }
        )
        raise RuntimeError(f"E.ON rejected {operation}: {', '.join(codes)}")
    return body.get("data") or {}


def normalise_token(value: str) -> str:
    value = value.strip()
    if value.lower().startswith("authorization:"):
        value = value.split(":", 1)[1].strip()
    if not value.upper().startswith("JWT "):
        value = f"JWT {value}"
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--regenerate", action="store_true", help="create a key if none exists")
    parser.add_argument("--force", action="store_true", help="with --regenerate, replace an existing key")
    args = parser.parse_args()

    raw = getpass.getpass("Authorization header from a signed-in eonnext.com session: ")
    if not raw.strip():
        print("No token supplied.", file=sys.stderr)
        return 2
    token = normalise_token(raw)

    try:
        key = (graphql(token, "ViewerKey", VIEWER_KEY).get("viewer") or {}).get("liveSecretKey")
        if key and args.regenerate and not args.force:
            print("A key already exists; pass --force to replace it.", file=sys.stderr)
        elif args.regenerate and (args.force or not key):
            key = (graphql(token, "RegenerateKey", REGENERATE_KEY).get("regenerateSecretKey") or {}).get("key")
    except (RuntimeError, urllib.error.URLError) as err:
        print(f"Failed: {err}", file=sys.stderr)
        return 1
    if not key:
        print("No API key exists for this account; rerun with --regenerate.", file=sys.stderr)
        return 1
    print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

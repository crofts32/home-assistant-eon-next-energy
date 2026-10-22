#!/usr/bin/env python3
"""Fetch the E.ON Next account API key using a signed-in browser session.

E.ON Next retired email/password login for its Kraken API. Sign in at
https://www.eonnext.com, then either copy the Authorization request header of
any request to api.eonnext-kraken.energy from the developer tools Network tab,
or read the Auth0 ID token from the page (see README). Paste it when prompted;
it is not echoed, saved or sent anywhere except E.ON's API.

The key is printed once so it can be pasted into Home Assistant. With
--regenerate the script creates a new key when none exists, or replaces the
existing one with --regenerate --force; replacing it breaks any other client
that holds the old key.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
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


def _claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _issuer(token: str) -> str:
    return str(_claims(token).get("iss", ""))


def describe_token(header: str) -> str:
    """Summarise a token without revealing it or any personal claim."""
    scheme, _, token = header.partition(" ")
    claims = _claims(token)
    parts = [f"scheme={scheme}", f"length={len(token)}", f"segments={token.count('.') + 1}"]
    if not claims:
        parts.append("payload=unreadable (not a JWT?)")
        return ", ".join(parts)
    issuer = urllib.parse.urlsplit(str(claims.get("iss", ""))).netloc or "missing"
    parts.append(f"issuer={issuer}")
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        minutes = int((exp - time.time()) // 60)
        parts.append(f"expired {-minutes} min ago" if minutes < 0 else f"expires in {minutes} min")
    for claim in ("token_use", "typ", "scope"):
        if isinstance(claims.get(claim), str):
            parts.append(f"{claim}={claims[claim][:40]}")
    parts.append(f"audience={'present' if claims.get('aud') else 'missing'}")
    return ", ".join(parts)


def normalise_token(value: str) -> str:
    value = value.strip().strip("'\"").strip()
    if value.lower().startswith("authorization:"):
        value = value.split(":", 1)[1].strip()
    scheme, _, rest = value.partition(" ")
    if rest and scheme.lower() in ("jwt", "bearer"):
        return f"{'JWT' if scheme.lower() == 'jwt' else 'Bearer'} {rest.strip()}"
    return f"{'JWT' if 'kraken' in _issuer(value).lower() else 'Bearer'} {value}"


def read_clipboard() -> str:
    for command in (["pbpaste"], ["wl-paste", "--no-newline"], ["xclip", "-o", "-selection", "clipboard"]):
        try:
            return subprocess.run(command, capture_output=True, check=True, text=True).stdout
        except (OSError, subprocess.CalledProcessError):
            continue
    raise RuntimeError("No clipboard tool found (pbpaste, wl-paste or xclip)")


def read_hidden_line(prompt: str) -> str:
    # Terminals in canonical mode cap a line at 1024 bytes on macOS, shorter than an Auth0 token.
    try:
        import termios
    except ImportError:
        return getpass.getpass(prompt)
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    updated = termios.tcgetattr(fd)
    updated[3] &= ~(termios.ICANON | termios.ECHO)
    sys.stderr.write(prompt)
    sys.stderr.flush()
    chars: list[str] = []
    try:
        termios.tcsetattr(fd, termios.TCSANOW, updated)
        while (char := sys.stdin.read(1)) and char not in "\r\n":
            if char in "\x7f\b":
                if chars:
                    chars.pop()
            elif char == "\x03":
                raise KeyboardInterrupt
            else:
                chars.append(char)
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, previous)
        sys.stderr.write("\n")
    return "".join(chars)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--regenerate", action="store_true", help="create a key if none exists")
    parser.add_argument("--force", action="store_true", help="with --regenerate, replace an existing key")
    parser.add_argument("--clipboard", action="store_true", help="read the token from the clipboard instead of prompting")
    args = parser.parse_args()

    try:
        if args.clipboard:
            raw = read_clipboard()
        elif not sys.stdin.isatty():
            raw = sys.stdin.read()
        else:
            raw = read_hidden_line("Paste the token from a signed-in eonnext.com session, then press Enter: ")
    except RuntimeError as err:
        print(f"Failed: {err}", file=sys.stderr)
        return 2
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
        print(f"Token sent: {describe_token(token)}", file=sys.stderr)
        return 1
    if not key:
        print("No API key exists for this account; rerun with --regenerate.", file=sys.stderr)
        return 1
    print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

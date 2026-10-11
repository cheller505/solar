#!/usr/bin/env python3
"""
Mint an Enphase IQ Gateway (Envoy) local JWT access token via the entrez flow.

Logs into https://entrez.enphaseenergy.com with an Enlighten username/password,
then requests a token for the given gateway serial number. The returned JWT is
what the local gateway's /production.json, /api/v1/production, etc. require as
a Bearer token.

Usage:
    enphase_get_token.py --user USER --password PASS --serial 202325167285 \
        [--token-name apollo] [--out /path/to/token.txt]
"""
import argparse
import getpass
import re
import sys
import time

import requests

ENTREZ_BASE = "https://entrez.enphaseenergy.com"
TOKEN_URL = f"{ENTREZ_BASE}/tokens"
LOGIN_URL = f"{ENTREZ_BASE}/login"


def get_csrf(session: requests.Session) -> str:
    r = session.get(f"{ENTREZ_BASE}/login_main_page", timeout=30)
    r.raise_for_status()
    m = re.search(r'name="_csrf"\s+value="([^"]+)"', r.text)
    if not m:
        raise RuntimeError("could not find _csrf in login page")
    return m.group(1)


def login(session: requests.Session, user: str, password: str) -> None:
    csrf = get_csrf(session)
    payload = {
        "_csrf": csrf,
        "username": user,
        "password": password,
        "authFlow": "entrezSession",
    }
    r = session.post(LOGIN_URL, data=payload, timeout=30, allow_redirects=True)
    r.raise_for_status()
    if "login" in r.url:
        raise RuntimeError(
            "login appears to have failed (still on a login page). "
            "Check username/password."
        )


def get_token(session: requests.Session, serial: str, token_name: str) -> str:
    payload = {
        "serial_num": serial,
        "username": "",
        "token_name": token_name or f"token-{int(time.time())}",
    }
    r = session.post(TOKEN_URL, json=payload, timeout=30)
    r.raise_for_status()
    token = r.text.strip()
    if not token or token.count(".") != 2:
        raise RuntimeError(f"unexpected token response: {token[:200]!r}")
    return token


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True, help="Enlighten username (email)")
    ap.add_argument("--password", help="Enlighten password (omit to be prompted)")
    ap.add_argument("--serial", required=True, help="Gateway serial number")
    ap.add_argument("--token-name", default="", help="Label for the token")
    ap.add_argument("--out", help="Write token to this file (else stdout)")
    args = ap.parse_args()

    password = args.password or getpass.getpass("Enlighten password: ")

    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (enphase-token-fetch)"
    login(s, args.user, password)
    token = get_token(s, args.serial, args.token_name)

    if args.out:
        with open(args.out, "w") as f:
            f.write(token)
        print(f"wrote token ({len(token)} chars) to {args.out}")
    else:
        print(token)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

#!/usr/bin/env python3
"""Refresh the Enphase IQ Gateway JWT token used by the enphase-exporter.

Performs the full entrez flow in one shot:
  1. Log into entrez.enphaseenergy.com with an Enlighten account.
  2. Look up the site + gateway serial (optionally auto-detect for the account).
  3. POST /entrez_tokens to mint a fresh ~1-year JWT.
  4. Write it to the live token file the exporter reads.

The exporter re-reads token.txt on every poll, so no container restart is
needed after a refresh.

Credentials come from a small env file (default /opt/appdata/enphase/refresh.env)
so they aren't on the command line / in shell history:

    ENPHASE_USER=you@example.com
    ENPHASE_PASS=secret
    ENPHASE_SITE=Christopher Heller      # optional; auto-detected if omitted
    ENPHASE_SERIAL=202325167285          # optional; first serial of the site
    ENPHASE_TOKEN_OUT=/opt/appdata/enphase/token.txt

Run:  python3 refresh_enphase_token.py
"""
import json
import os
import re
import sys
import time

import requests

ENTREZ = "https://entrez.enphaseenergy.com"
ENLIGHTEN = "https://enlighten.enphaseenergy.com"


def load_env(path):
    cfg = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def entrez_login(sess, user, password):
    r = sess.get(f"{ENTREZ}/login_main_page", timeout=30)
    csrf = re.search(r'name="_csrf"\s+value="([^"]+)"', r.text).group(1)
    r = sess.post(
        f"{ENTREZ}/login",
        data={"_csrf": csrf, "username": user, "password": password, "authFlow": "entrezSession"},
        timeout=30,
        allow_redirects=True,
    )
    if "Select Gateway" not in r.text and "entrez_tokens" not in r.text:
        raise RuntimeError("entrez login failed (unexpected landing page)")
    m = re.search(r'name="_csrf"\s+value="([^"]+)"', r.text)
    return m.group(1) if m else csrf


def discover_site(sess, user, password, site_hint):
    """Use the Enlighten app-api to find the account's first site + serial."""
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0"
    r = s.get(f"{ENLIGHTEN}/login", timeout=30)
    tok = re.search(r'name="authenticity_token" value="([^"]+)"', r.text).group(1)
    s.post(
        f"{ENLIGHTEN}/login/login",
        data={
            "utf8": "\u2713",
            "authenticity_token": tok,
            "user[email]": user,
            "user[password]": password,
            "secured_user": "true",
            "locale": "en",
            "commit": "Sign In",
        },
        timeout=30,
    )
    sites = s.get(f"{ENLIGHTEN}/app-api/user_sites.json", timeout=30).json().get("sites", [])
    if not sites:
        raise RuntimeError("account has no Enlighten sites")
    if site_hint:
        match = [x for x in sites if x["name"] == site_hint]
        if not match:
            raise RuntimeError(f"site {site_hint!r} not found; available: {[x['name'] for x in sites]}")
        return match[0]["name"]
    return sites[0]["name"]


def main():
    env_path = os.environ.get("ENPHASE_REFRESH_ENV", "/opt/appdata/enphase/refresh.env")
    cfg = load_env(env_path)
    user = os.environ.get("ENPHASE_USER") or cfg.get("ENPHASE_USER")
    password = os.environ.get("ENPHASE_PASS") or cfg.get("ENPHASE_PASS")
    site = os.environ.get("ENPHASE_SITE") or cfg.get("ENPHASE_SITE") or ""
    serial = os.environ.get("ENPHASE_SERIAL") or cfg.get("ENPHASE_SERIAL") or ""
    out = os.environ.get("ENPHASE_TOKEN_OUT") or cfg.get("ENPHASE_TOKEN_OUT") or "/opt/appdata/enphase/token.txt"

    if not user or not password:
        print(f"error: set ENPHASE_USER/ENPHASE_PASS in {env_path}", file=sys.stderr)
        return 1

    if not site:
        site = discover_site(requests.Session(), user, password, site or None)
        print(f"discovered site: {site!r}")

    sess = requests.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0"
    csrf = entrez_login(sess, user, password)

    # resolve serial from the site list if not provided
    if not serial:
        r = sess.get(f"{ENTREZ}/site/{requests.utils.quote(site)}", timeout=30)
        data = r.json()
        if not data:
            raise RuntimeError(f"no gateway found for site {site!r}")
        serial = data[0]["serial_numbers"][0]
        print(f"discovered serial: {serial}")

    r = sess.post(
        f"{ENTREZ}/entrez_tokens",
        data={"_csrf": csrf, "Site": site, "serialNum": serial},
        timeout=30,
    )
    m = re.search(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", r.text)
    if not m:
        raise RuntimeError("no JWT found in entrez_tokens response")
    token = m.group(0)

    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        f.write(token)
    os.chmod(tmp, 0o600)
    os.replace(tmp, out)
    print(f"wrote fresh token ({len(token)} chars) to {out} at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

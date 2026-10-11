#!/usr/bin/env python3
"""Enphase API v4 client + Prometheus remote-write backfill.

Uses the OAuth 2.0 flow (client_id/client_secret + refresh_token) to obtain
access tokens, then pulls historical production and writes it into the local
Prometheus remote-write receiver.

Auth model (API v4):
  - OAuth token in `Authorization: Bearer <access_token>` header
  - application API key in a header named `key`

Endpoints used (all available on the free Watt plan):
  GET /api/v4/systems/<id>/energy_lifetime        daily Wh, whole system life
  GET /api/v4/systems/<id>/telemetry/production_meter
                                                  15-minute Wh intervals,
                                                  ?start_at=&end_at=

Rate limits (Watt): 10 hits/minute, 1000 hits/month. Keep calls minimal.

Usage:
  python3 enphase_v4_backfill.py --daily-only
  python3 enphase_v4_backfill.py --intervals-days 90
  python3 enphase_v4_backfill.py --dry-run
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from promrw import TimeSeries, write_request, remote_write  # noqa: E402

API = "https://api.enphaseenergy.com"
CREDS = os.environ.get(
    "ENPHASE_OAUTH_ENV",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "oauth.env"),
)


def load_env(path):
    cfg = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def http(method, url, headers=None, data=None, timeout=60):
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return r.status, body


def refresh_access_token(cfg):
    import base64

    basic = base64.b64encode(
        f"{cfg['CLIENT_ID']}:{cfg['CLIENT_SECRET']}".encode()
    ).decode()
    q = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": cfg["REFRESH_TOKEN"]}
    )
    status, body = http(
        "POST",
        f"{API}/oauth/token?{q}",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        data=b"",
    )
    d = json.loads(body)
    cfg["ACCESS_TOKEN"] = d["access_token"]
    if d.get("refresh_token"):
        cfg["REFRESH_TOKEN"] = d["refresh_token"]
    save_env(cfg)
    return cfg


def save_env(cfg):
    tmp = CREDS + ".tmp"
    with open(tmp, "w") as f:
        for k in ("CLIENT_ID", "CLIENT_SECRET", "ACCESS_TOKEN", "REFRESH_TOKEN", "API_KEY", "SYSTEM_ID"):
            if k in cfg and cfg[k]:
                f.write(f"{k}={cfg[k]}\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CREDS)


def api_get(cfg, path, **params):
    params["key"] = cfg["API_KEY"]
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    hdrs = {"Authorization": f"Bearer {cfg['ACCESS_TOKEN']}", "key": cfg["API_KEY"], "Accept": "application/json"}
    status, body = http("GET", url, headers=hdrs)
    return json.loads(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--creds", default=CREDS)
    ap.add_argument("--daily-only", action="store_true", help="only pull daily lifetime data")
    ap.add_argument("--intervals-days", type=int, default=0, help="also pull N days of 15-min data")
    ap.add_argument("--prom-url", default="http://127.0.0.1:9090/api/v1/write")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_env(args.creds)
    if not cfg.get("ACCESS_TOKEN"):
        cfg = refresh_access_token(cfg)
    else:
        cfg = refresh_access_token(cfg)  # always refresh: access tokens last 1 day
    sysid = cfg["SYSTEM_ID"]
    series = []

    # --- daily lifetime ----------------------------------------------------
    d = api_get(cfg, f"/api/v4/systems/{sysid}/energy_lifetime")
    start_date = d.get("start_date")
    prod = d.get("production", [])
    day0 = dt.datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    cum_ts, daily_ts = [], []
    cum = 0
    for i, wh in enumerate(prod):
        day = day0 + dt.timedelta(days=i)
        ts = int((day + dt.timedelta(hours=12)).timestamp())
        wh = wh or 0
        cum += wh
        cum_ts.append((ts, float(cum)))
        daily_ts.append((ts, float(wh)))
    label = {"instance": "apollo", "job": "enphase", "source": "cloud"}
    series.append(TimeSeries("enphase_production_wh_lifetime", cum_ts, label))
    series.append(TimeSeries("enphase_energy_wh_daily", daily_ts, label))
    print(f"energy_lifetime: {len(prod)} days from {start_date}, total {cum/1000:.0f} kWh")

    # --- recent 15-min intervals (optional) --------------------------------
    if not args.daily_only and args.intervals_days > 0:
        now = int(time.time())
        chunk = 7 * 86400
        cursor = now - args.intervals_days * 86400
        iv_ts = []
        calls = 0
        while cursor < now:
            end = min(cursor + chunk, now)
            try:
                r = api_get(
                    cfg,
                    f"/api/v4/systems/{sysid}/telemetry/production_meter",
                    start_at=cursor,
                    end_at=end,
                )
                for iv in r.get("intervals", []):
                    iv_ts.append((iv["end_at"], float(iv.get("wh_del", 0))))
                calls += 1
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"intervals {cursor}-{end}: {e}\n")
            cursor = end
            time.sleep(7)  # respect 10/min
        print(f"production_meter: {len(iv_ts)} intervals in {calls} calls")
        if iv_ts:
            series.append(TimeSeries("enphase_energy_wh_15min", iv_ts, label))

    total = sum(len(s.samples) for s in series)
    print(f"writing {total} samples across {len(series)} series")
    if args.dry_run:
        for s in series:
            print(f"  {s.name}: {len(s.samples)} samples")
        return 0

    body = write_request(series)
    r = remote_write(args.prom_url, body)
    print(f"remote write -> {r.status_code}")
    r.raise_for_status()
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())

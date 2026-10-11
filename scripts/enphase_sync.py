#!/usr/bin/env python3
"""Daily incremental sync of Enphase cloud history into Prometheus.

Fetches energy_lifetime from the Enphase v4 API and remote-writes only the
RECENT days (default last 7) into the existing cloud series. Recent timestamps
fall within Prometheus's remote-write head window, so no TSDB block import is
needed for routine daily updates.

Series written (matching the original backfill's labels, so they merge):
  enphase_energy_wh_daily{source="cloud"}          -- per-day Wh
  enphase_production_wh_lifetime{source="cloud"}   -- cumulative Wh

Run daily via systemd timer / cron. Exits non-zero on failure so the timer
records it.
"""
import base64
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from promrw import TimeSeries, write_request, remote_write  # noqa: E402

CREDS = os.environ.get("ENPHASE_OAUTH_ENV", os.path.join(HERE, "oauth.env"))
API = "https://api.enphaseenergy.com"
PROM = os.environ.get("ENPHASE_PROM_URL", "http://127.0.0.1:9090/api/v1/write")
RECENT_DAYS = int(os.environ.get("ENPHASE_SYNC_DAYS", "7"))


def load_env(path):
    cfg = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def save_env(cfg, path):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for k in ("CLIENT_ID", "CLIENT_SECRET", "ACCESS_TOKEN", "REFRESH_TOKEN", "API_KEY", "SYSTEM_ID"):
            if cfg.get(k):
                f.write(f"{k}={cfg[k]}\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def refresh(cfg):
    basic = base64.b64encode(f"{cfg['CLIENT_ID']}:{cfg['CLIENT_SECRET']}".encode()).decode()
    q = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": cfg["REFRESH_TOKEN"]})
    req = urllib.request.Request(f"{API}/oauth/token?{q}", method="POST",
                                 headers={"Authorization": f"Basic {basic}"}, data=b"")
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)
    cfg["ACCESS_TOKEN"] = d["access_token"]
    if d.get("refresh_token"):
        cfg["REFRESH_TOKEN"] = d["refresh_token"]
    save_env(cfg, CREDS)
    return cfg


def main():
    cfg = refresh(load_env(CREDS))
    sysid = cfg["SYSTEM_ID"]
    url = f"{API}/api/v4/systems/{sysid}/energy_lifetime?{urllib.parse.urlencode({'key': cfg['API_KEY']})}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {cfg['ACCESS_TOKEN']}",
                                               "key": cfg["API_KEY"], "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)

    prod = data.get("production", [])
    day0 = dt.datetime.strptime(data["start_date"], "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)

    # Build the FULL cumulative series so the running total stays correct, but
    # only emit samples for the last RECENT_DAYS (remote-write window limits).
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=RECENT_DAYS)
    label = {"instance": "apollo", "job": "enphase", "source": "cloud"}
    daily, cum = [], []
    running = 0
    for i, wh in enumerate(prod):
        ts = int((day0 + dt.timedelta(days=i, hours=12)).timestamp())
        running += wh or 0
        if dt.datetime.fromtimestamp(ts, dt.timezone.utc) >= cutoff:
            daily.append((ts, float(wh or 0)))
            cum.append((ts, float(running)))

    if not daily:
        print("no recent days to sync")
        return 0

    series = [
        TimeSeries("enphase_energy_wh_daily", daily, label),
        TimeSeries("enphase_production_wh_lifetime", cum, label),
    ]
    body = write_request(series)
    resp = remote_write(PROM, body)
    last = dt.datetime.fromtimestamp(daily[-1][0], dt.timezone.utc).date()
    print(f"synced {len(daily)} days (through {last}); remote-write -> {resp.status_code}")
    resp.raise_for_status()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Fetch Enphase v4 history and emit OpenMetrics for promtool TSDB import.

Because Prometheus rejects remote-write samples older than its head window,
multi-year history is imported by generating TSDB blocks with
`promtool tsdb create-blocks-from openmetrics` instead.

Output: an OpenMetrics text file (metrics + timestamps in milliseconds).
"""
import base64
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CREDS = os.environ.get("ENPHASE_OAUTH_ENV", os.path.join(HERE, "oauth.env"))
API = "https://api.enphaseenergy.com"


def load_env(path):
    cfg = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def api(cfg, path, **params):
    params["key"] = cfg["API_KEY"]
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {cfg['ACCESS_TOKEN']}", "key": cfg["API_KEY"], "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def refresh(cfg):
    basic = base64.b64encode(f"{cfg['CLIENT_ID']}:{cfg['CLIENT_SECRET']}".encode()).decode()
    q = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": cfg["REFRESH_TOKEN"]})
    req = urllib.request.Request(
        f"{API}/oauth/token?{q}", method="POST", headers={"Authorization": f"Basic {basic}"}, data=b""
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)
    cfg["ACCESS_TOKEN"] = d["access_token"]
    if d.get("refresh_token"):
        cfg["REFRESH_TOKEN"] = d["refresh_token"]
    return cfg


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/enphase_history.om"
    interval_days = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    cfg = refresh(load_env(CREDS))
    sysid = cfg["SYSTEM_ID"]

    lines = []

    def emit(name, help_text, mtype, samples, labels=None):
        lstr = ""
        if labels:
            lstr = "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
        for ts, val in samples:
            # OpenMetrics timestamps are in SECONDS. promtool multiplies by
            # 1000 internally to store milliseconds -- passing ms here is what
            # produced the earlier "16-digit / year-50000" bad blocks.
            lines.append(f"{name}{lstr} {val} {int(ts)}")

    labels = {"instance": "apollo", "job": "enphase", "source": "cloud"}

    # daily lifetime
    d = api(cfg, f"/api/v4/systems/{sysid}/energy_lifetime")
    day0 = dt.datetime.strptime(d["start_date"], "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    cum, cum_ts, daily_ts = 0, [], []
    for i, wh in enumerate(d.get("production", [])):
        ts = int((day0 + dt.timedelta(days=i, hours=12)).timestamp())
        wh = wh or 0
        cum += wh
        cum_ts.append((ts, float(cum)))
        daily_ts.append((ts, float(wh)))
    emit("enphase_production_wh_lifetime", "Lifetime production energy Wh (cloud)", "counter", cum_ts, labels)
    emit("enphase_energy_wh_daily", "Daily production energy Wh (cloud)", "gauge", daily_ts, labels)
    print(f"daily: {len(daily_ts)} days, {cum/1000:.0f} kWh total")

    # 15-min intervals (optional)
    if interval_days > 0:
        now = int(time.time())
        chunk = 7 * 86400
        cursor = now - interval_days * 86400
        iv = []
        while cursor < now:
            end = min(cursor + chunk, now)
            try:
                r = api(cfg, f"/api/v4/systems/{sysid}/telemetry/production_meter", start_at=cursor, end_at=end)
                for x in r.get("intervals", []):
                    iv.append((x["end_at"], float(x.get("wh_del", 0))))
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"interval {cursor}: {e}\n")
            cursor = end
            time.sleep(7)
        if iv:
            emit("enphase_energy_wh_15min", "15-minute production energy Wh (cloud)", "gauge", iv, labels)
        print(f"intervals: {len(iv)} samples over {interval_days}d")

    lines.append("# EOF")
    tmp = out_path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, out_path)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()

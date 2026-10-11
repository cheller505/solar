#!/usr/bin/env python3
"""Enphase cloud history backfill -> Prometheus (remote write).

Pulls historical production from the Enlighten Systems API v2 and pushes it
into the local Prometheus via its remote-write receiver
(--web.enable-remote-write-receiver).

Data sources (Enlighten Systems API v2):
  /systems/<id>/energy_lifetime   daily Wh totals for the whole system life
  /systems/<id>/stats             5-minute wh/power intervals (last ~1y)
  /systems/<id>/consumption_lifetime  daily consumption Wh (if CTs present)

Auth: API key + user_id from developer.enphase.com.

Metrics written (same names the live exporter uses, so history and live data
sit in the same series):
  enphase_production_wh_lifetime   monotonic cumulative Wh (sum of daily)
  enphase_energy_wh_daily          per-day energy produced (sub-divided to
                                   5-min points when stats are available)
  enphase_production_watts_avg     average power per interval (from stats)

The remote-write payload is built by hand (protobuf + snappy) to avoid extra
runtime deps beyond `requests` and `snappy`.
"""
import argparse
import datetime as dt
import json
import os
import struct
import sys
import time
import urllib.parse

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from promrw import TimeSeries, write_request, remote_write  # noqa: E402

API = "https://api.enphaseenergy.com/api/v2"


def api_get(path, key, user_id, **params):
    params["key"] = key
    params["user_id"] = user_id
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    r = requests.get(url, timeout=60)
    if r.status_code == 204:
        return None
    r.raise_for_status()
    return r.json()


def to_epoch(date_str, tz_offset_seconds=0):
    d = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True, help="Enphase API key")
    ap.add_argument("--user-id", required=True, help="Enlighten user_id")
    ap.add_argument("--system-id", required=True, type=int)
    ap.add_argument(
        "--prom-url",
        default="http://127.0.0.1:9090/api/v1/write",
        help="Prometheus remote-write endpoint",
    )
    ap.add_argument("--stats-days", type=int, default=365, help="days of 5-min stats to fetch")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tz_off = None
    series = []

    # --- system meta -------------------------------------------------------
    summ = api_get(f"/systems/{args.system_id}/summary", args.key, args.user_id)
    print(f"system {args.system_id}: {summ.get('modules')} modules, "
          f"{summ.get('size_w')}W, lifetime {summ.get('energy_lifetime',0)/1e6:.2f} MWh")

    # --- daily lifetime ----------------------------------------------------
    life = api_get(f"/systems/{args.system_id}/energy_lifetime", args.key, args.user_id)
    prod = life.get("production", [])
    start = life.get("start_date")
    print(f"energy_lifetime: {len(prod)} days from {start}")

    # Walk days, accumulate a monotonic lifetime counter and emit daily values.
    day0 = dt.datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    cum = 0
    lifetime_ts = []
    daily_ts = []
    for i, wh in enumerate(prod):
        day = day0 + dt.timedelta(days=i)
        # daily energy lands at local noon-ish; use 12:00 UTC as a stable anchor
        ts = int((day + dt.timedelta(hours=12)).timestamp())
        wh = wh or 0
        cum += wh
        lifetime_ts.append((ts, float(cum)))
        daily_ts.append((ts, float(wh)))

    series.append(TimeSeries("enphase_production_wh_lifetime", 
                             list(lifetime_ts), {"instance": "apollo", "job": "enphase", "source": "cloud"}))
    series.append(TimeSeries("enphase_energy_wh_daily",
                             list(daily_ts), {"instance": "apollo", "job": "enphase", "source": "cloud"}))

    # --- 5-minute stats (last N days), fetched one day at a time -----------
    stats_ts_power = []
    now = int(time.time())
    stats_start = now - args.stats_days * 86400
    day_cursor = stats_start
    got = 0
    while day_cursor < now:
        end = min(day_cursor + 86400, now)
        try:
            d = api_get(
                f"/systems/{args.system_id}/stats",
                args.key, args.user_id,
                start_at=day_cursor, end_at=end,
            )
            for iv in (d or {}).get("intervals", []):
                te = iv.get("end_at")
                if te is None:
                    continue
                if "enwh" in iv:
                    stats_ts_power.append((te, float(iv["enwh"])))
                got += 1
        except requests.HTTPError as e:
            sys.stderr.write(f"stats {day_cursor}: {e}\n")
        day_cursor = end
    print(f"stats: {got} intervals over {args.stats_days}d")
    if stats_ts_power:
        series.append(TimeSeries("enphase_energy_wh_5min",
                                 stats_ts_power,
                                 {"instance": "apollo", "job": "enphase", "source": "cloud"}))

    # --- consumption (optional) -------------------------------------------
    try:
        cons = api_get(f"/systems/{args.system_id}/consumption_lifetime", args.key, args.user_id)
        if cons and cons.get("consumption"):
            cstart = dt.datetime.strptime(cons["start_date"], "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
            cts = []
            for i, wh in enumerate(cons["consumption"]):
                day = cstart + dt.timedelta(days=i)
                ts = int((day + dt.timedelta(hours=12)).timestamp())
                cts.append((ts, float(wh or 0)))
            series.append(TimeSeries("enphase_consumption_wh_daily", cts,
                                     {"instance": "apollo", "job": "enphase", "source": "cloud"}))
            print(f"consumption_lifetime: {len(cts)} days")
    except requests.HTTPError as e:
        print(f"consumption unavailable: {e}")

    # --- write -------------------------------------------------------------
    total = sum(len(s.samples) for s in series)
    print(f"writing {total} samples across {len(series)} series to {args.prom_url}")
    if args.dry_run:
        for s in series:
            print(f"  {s.name}: {len(s.samples)} samples "
                  f"[{s.samples[0][0] if s.samples else '-'} .. {s.samples[-1][0] if s.samples else '-'}]")
        return 0

    body = write_request(series)
    r = remote_write(args.prom_url, body)
    print(f"remote write -> {r.status_code}")
    r.raise_for_status()
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())

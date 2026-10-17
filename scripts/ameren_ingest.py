#!/usr/bin/env python3
"""Ingest Ameren (ACE) Advanced Metering CSV exports into Prometheus.

The export (downloaded from the Ameren account portal) contains 15-minute
interval rows: Usage (grid import kWh), Reverse Usage (solar export kWh).

Emitted metrics (labels: source="util", instance="apollo"):
  ameren_import_watts / ameren_export_watts    instantaneous avg power per 15-min interval
  ameren_import_wh_daily / ameren_export_wh_daily   daily totals (Wh), timestamped local midnight

Usage:
  python3 ameren_ingest.py openmetrics OUT.om file1.csv [file2.csv ...]   # full history (block import)
  python3 ameren_ingest.py remotewrite file.csv ...                       # recent updates (<30d old)

Block-import procedure (for the bulk history):
  promtool tsdb create-blocks-from openmetrics OUT.om blocks/
  # stop prometheus, copy blocks into /opt/appdata/prometheus/data (chown 65534), start
"""
import csv
import datetime
import os
import sys
import zoneinfo

TZ = zoneinfo.ZoneInfo("America/Chicago")
LABELS = '{source="util",instance="apollo"}'
DAILY_MAX_AGE_D = 1  # don't emit today's incomplete daily totals


def parse(path):
    """Yield (ts, import_kwh, export_kwh) for each 15-min interval."""
    with open(path, encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    hi = next(i for i, l in enumerate(lines) if l.startswith("Start Interval"))
    for line in lines[hi + 1:]:
        if not line.strip():
            continue
        p = next(csv.reader([line]))
        try:
            dt = datetime.datetime.strptime(p[0], "%m/%d/%Y %H:%M")
            u = float(p[2] or 0)
            r = float(p[5] or 0)
        except (ValueError, IndexError):
            continue
        yield dt.replace(tzinfo=TZ).timestamp(), u, r


def series(files):
    """Deduplicated (ts, imp_kwh, exp_kwh) intervals + daily totals."""
    seen = {}
    daily = {}
    for path in files:
        for ts, u, r in parse(path):
            if ts in seen:
                continue  # first file wins on cross-file overlap days
            seen[ts] = (u, r)
            d = datetime.datetime.fromtimestamp(ts, TZ).date()
            acc = daily.setdefault(d, [0.0, 0.0])
            acc[0] += u
            acc[1] += r
    return seen, daily


def emit(files, out):
    seen, daily = series(files)
    w = out.write
    now = datetime.datetime.now(TZ)
    w("# HELP ameren_import_watts Grid import power from Ameren meter CSV (15-min intervals)\n")
    w("# TYPE ameren_import_watts gauge\n")
    for ts in sorted(seen):
        w(f'ameren_import_watts{LABELS} {seen[ts][0] * 4000:.1f} {int(ts * 1000)}\n')
    w("# HELP ameren_export_watts Solar export power from Ameren meter CSV (15-min intervals)\n")
    w("# TYPE ameren_export_watts gauge\n")
    for ts in sorted(seen):
        w(f'ameren_export_watts{LABELS} {seen[ts][1] * 4000:.1f} {int(ts * 1000)}\n')
    w("# HELP ameren_import_wh_daily Daily grid import energy (Wh), Ameren meter CSV\n")
    w("# TYPE ameren_import_wh_daily gauge\n")
    w("# HELP ameren_export_wh_daily Daily solar export energy (Wh), Ameren meter CSV\n")
    w("# TYPE ameren_export_wh_daily gauge\n")
    for d in sorted(daily):
        # skip the (incomplete) current day for daily totals
        if (now.date() - d).days < DAILY_MAX_AGE_D:
            continue
        midnight = int(datetime.datetime.combine(d, datetime.time(0, 0), TZ).timestamp() * 1000)
        w(f'ameren_import_wh_daily{LABELS} {daily[d][0] * 1000:.0f} {midnight}\n')
        w(f'ameren_export_wh_daily{LABELS} {daily[d][1] * 1000:.0f} {midnight}\n')
    w("# EOF\n")


def remote_write(files, prom="http://127.0.0.1:9090/api/v1/write", window_days=25):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from promrw import TimeSeries, write_request, remote_write as rw
    seen, daily = series(files)
    now = datetime.datetime.now(TZ)
    ts_list = sorted(seen)
    min_ts = min(ts_list) if ts_list else 0
    cutoff = now.timestamp() - window_days * 86400
    labels = {"source": "util", "instance": "apollo"}
    imp, exp = [], []
    for ts in ts_list:
        if ts < cutoff:
            continue
        imp.append((ts, seen[ts][0] * 4000))
        exp.append((ts, seen[ts][1] * 4000))
    daily_i, daily_e = [], []
    for d, (u, r) in daily.items():
        if (now.date() - d).days < DAILY_MAX_AGE_D:
            continue
        midnight = datetime.datetime.combine(d, datetime.time(0, 0), TZ).timestamp()
        daily_i.append((midnight, u * 1000))
        daily_e.append((midnight, r * 1000))
    series_list = [
        TimeSeries("ameren_import_watts", imp, labels),
        TimeSeries("ameren_export_watts", exp, labels),
        TimeSeries("ameren_import_wh_daily", daily_i, labels),
        TimeSeries("ameren_export_wh_daily", daily_e, labels),
    ]
    resp = rw(prom, write_request(series_list))
    n = len(imp) + len(exp) + len(daily_i) + len(daily_e)
    print(f"remote-wrote {n} samples -> {resp.status_code}; oldest sent: "
          f"{datetime.datetime.fromtimestamp(max(min_ts, cutoff), TZ):%Y-%m-%d}")
    resp.raise_for_status()


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "openmetrics":
        with open(sys.argv[2], "w") as f:
            emit(sys.argv[3:], f)
        print(f"wrote {sys.argv[2]}")
    elif cmd == "remotewrite":
        args = sys.argv[2:]
        wd = 25
        if args and args[0].startswith("--window-days="):
            wd = int(args[0].split("=")[1])
            args = args[1:]
        remote_write(args, window_days=wd)
    else:
        sys.exit(__doc__)

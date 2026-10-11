#!/usr/bin/env python3
"""Enphase IQ Gateway (Envoy) Prometheus exporter.

Polls the local gateway's JSON endpoints with a JWT access token and serves
the results on /metrics for Prometheus to scrape. Talks to the gateway
directly over the LAN (no Enphase cloud), which means no rate limits and
sub-minute resolution.

Endpoints used (all need `Authorization: Bearer <token>`):
  /production.json                 -- production + consumption (eim) + storage
  /api/v1/production/inverters     -- per-inverter lastReportWatts, maxReportWatts
  /ivp/pdm/energy                  -- production/consumption totals (pcu/rgm/eim)

Metrics exposed (namespace `enphase_`):
  enphase_up{...}                         1 if the last poll of the gateway succeeded
  enphase_production_watts                current AC production (eim), W
  enphase_production_watts_inverters      sum of inverter-reported watts, W
  enphase_production_wh_lifetime          lifetime production energy, Wh
  enphase_consumption_watts               current total consumption (eim), W
  enphase_consumption_wh_lifetime         lifetime consumption energy, Wh
  enphase_net_consumption_watts           current net consumption (eim), W
  enphase_net_consumption_wh_lifetime     lifetime net consumption energy, Wh
  enphase_inverter_last_report_watts{serial}   per-inverter last reported W
  enphase_inverter_max_report_watts{serial}    per-inverter max reported W
  enphase_inverter_last_report_timestamp_seconds{serial}
  enphase_inverters_reporting              number of inverters seen in last poll
  enphase_storage_soc_percent              battery state of charge %, if present
  enphase_grid_status                     1 = importing, -1 = exporting, 0 = idle (net)

Config via env:
  ENPHASE_GATEWAY       gateway host/IP (default 192.168.50.194)
  ENPHASE_TOKEN_FILE    path to JWT token file (default /token/token.txt)
  ENPHASE_LISTEN_PORT   listen port (default 9093)
  ENPHASE_INSECURE      "1" to skip TLS verification (self-signed cert) -- default 1
"""
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

GATEWAY = os.environ.get("ENPHASE_GATEWAY", "192.168.50.194")
TOKEN_FILE = os.environ.get("ENPHASE_TOKEN_FILE", "/token/token.txt")
PORT = int(os.environ.get("ENPHASE_LISTEN_PORT", "9093"))
INSECURE = os.environ.get("ENPHASE_INSECURE", "1") == "1"
POLL_INTERVAL = int(os.environ.get("ENPHASE_POLL_INTERVAL", "20"))

_ssl_ctx = ssl.create_default_context()
if INSECURE:
    _ssl_ctx.check_hostname = False
    _ssl_ctx.verify_mode = ssl.CERT_NONE

_lock = threading.Lock()
_metrics_text = "# enphase exporter starting\n"
_last_success = 0


def read_token():
    with open(TOKEN_FILE) as f:
        return f.read().strip()


def fetch(path, token, timeout=10):
    url = f"https://{GATEWAY}{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as r:
        return json.load(r)


def fmt(v):
    try:
        return repr(float(v))
    except (TypeError, ValueError):
        return "NaN"


def build_metrics(token):
    lines = []

    # --- production + consumption -----------------------------------------
    prod = fetch("/production.json", token)
    prod_eim = next(
        (p for p in prod.get("production", []) if p.get("type") == "eim"), {}
    )
    prod_inv = next(
        (p for p in prod.get("production", []) if p.get("type") == "inverters"), {}
    )
    cons_total = next(
        (c for c in prod.get("consumption", []) if c.get("measurementType") == "total-consumption"),
        {},
    )
    cons_net = next(
        (c for c in prod.get("consumption", []) if c.get("measurementType") == "net-consumption"),
        {},
    )

    lines.append("# HELP enphase_production_watts Current solar production (EIM metered), watts")
    lines.append("# TYPE enphase_production_watts gauge")
    lines.append(f"enphase_production_watts {fmt(prod_eim.get('wNow'))}")

    lines.append("# HELP enphase_production_watts_inverters Sum of inverter-reported watts")
    lines.append("# TYPE enphase_production_watts_inverters gauge")
    lines.append(f"enphase_production_watts_inverters {fmt(prod_inv.get('wNow'))}")

    lines.append("# HELP enphase_production_wh_lifetime Lifetime production energy, watt-hours")
    lines.append("# TYPE enphase_production_wh_lifetime counter")
    lines.append(f"enphase_production_wh_lifetime {fmt(prod_eim.get('whLifetime'))}")

    lines.append("# HELP enphase_consumption_watts Current total consumption, watts")
    lines.append("# TYPE enphase_consumption_watts gauge")
    lines.append(f"enphase_consumption_watts {fmt(cons_total.get('wNow'))}")

    lines.append("# HELP enphase_consumption_wh_lifetime Lifetime consumption energy, watt-hours")
    lines.append("# TYPE enphase_consumption_wh_lifetime counter")
    lines.append(f"enphase_consumption_wh_lifetime {fmt(cons_total.get('whLifetime'))}")

    lines.append("# HELP enphase_net_consumption_watts Current net consumption (import>0, export<0), watts")
    lines.append("# TYPE enphase_net_consumption_watts gauge")
    lines.append(f"enphase_net_consumption_watts {fmt(cons_net.get('wNow'))}")

    lines.append("# HELP enphase_net_consumption_wh_lifetime Lifetime net consumption energy, watt-hours")
    lines.append("# TYPE enphase_net_consumption_wh_lifetime counter")
    lines.append(f"enphase_net_consumption_wh_lifetime {fmt(cons_net.get('whLifetime'))}")

    # --- storage (only if the system has a battery) ------------------------
    for st in prod.get("storage", []):
        if st.get("type") == "acb" and st.get("activeCount", 0) > 0:
            pct = st.get("percentFull")
            lines.append("# HELP enphase_storage_soc_percent Battery state of charge, percent")
            lines.append("# TYPE enphase_storage_soc_percent gauge")
            lines.append(f"enphase_storage_soc_percent {fmt(pct)}")

    # --- per-inverter -------------------------------------------------------
    try:
        invs = fetch("/api/v1/production/inverters", token)
    except Exception:
        invs = []
    lines.append("# HELP enphase_inverters_reporting Number of inverters reporting")
    lines.append("# TYPE enphase_inverters_reporting gauge")
    lines.append(f"enphase_inverters_reporting {len(invs)}")
    lines.append("# HELP enphase_inverter_last_report_watts Per-inverter last reported power, watts")
    lines.append("# TYPE enphase_inverter_last_report_watts gauge")
    for inv in invs:
        sn = inv.get("serialNumber", "unknown")
        lines.append(f'enphase_inverter_last_report_watts{{serial="{sn}"}} {fmt(inv.get("lastReportWatts"))}')
    lines.append("# HELP enphase_inverter_max_report_watts Per-inverter max reported power, watts")
    lines.append("# TYPE enphase_inverter_max_report_watts gauge")
    for inv in invs:
        sn = inv.get("serialNumber", "unknown")
        lines.append(f'enphase_inverter_max_report_watts{{serial="{sn}"}} {fmt(inv.get("maxReportWatts"))}')
    lines.append("# HELP enphase_inverter_last_report_timestamp_seconds Per-inverter last report time")
    lines.append("# TYPE enphase_inverter_last_report_timestamp_seconds gauge")
    for inv in invs:
        sn = inv.get("serialNumber", "unknown")
        lines.append(
            f'enphase_inverter_last_report_timestamp_seconds{{serial="{sn}"}} {fmt(inv.get("lastReportDate"))}'
        )

    return lines


def poller():
    global _metrics_text, _last_success
    while True:
        try:
            token = read_token()
            lines = build_metrics(token)
            lines.append("# HELP enphase_up Whether the last gateway poll succeeded")
            lines.append("# TYPE enphase_up gauge")
            lines.append("enphase_up 1")
            lines.append("# HELP enphase_scrape_timestamp_seconds Unix time of last successful poll")
            lines.append("# TYPE enphase_scrape_timestamp_seconds gauge")
            lines.append(f"enphase_scrape_timestamp_seconds {int(time.time())}")
            with _lock:
                _metrics_text = "\n".join(lines) + "\n"
            _last_success = int(time.time())
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"enphase poll error: {e}\n")
            sys.stderr.flush()
            with _lock:
                _metrics_text = (
                    "# HELP enphase_up Whether the last gateway poll succeeded\n"
                    "# TYPE enphase_up gauge\n"
                    "enphase_up 0\n"
                )
        time.sleep(POLL_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path in ("/metrics", "/"):
            with _lock:
                body = _metrics_text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # silence per-request logging
        pass


def main():
    t = threading.Thread(target=poller, daemon=True)
    t.start()
    # wait for first poll so /metrics isn't empty on startup
    time.sleep(min(POLL_INTERVAL, 3))
    srv = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"enphase-exporter listening on :{PORT}, gateway={GATEWAY}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()

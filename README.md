# Enphase Solar → Prometheus → Grafana

Solar production monitoring for an Enphase IQ Gateway (IQ Envoy): a local
exporter plus cloud-history backfill, feeding Prometheus and visualised in
Grafana.

Two data paths:

| Path | Source | Resolution | Auth |
|------|--------|-----------|------|
| **Live** | Local IQ Gateway over LAN | 20 s | Gateway JWT (local) |
| **History** | Enphase API v4 (cloud) | daily + 15-min | OAuth 2.0 |

## Contents

```
scripts/
  enphase_exporter.py            Live exporter: polls the gateway, serves /metrics
  enphase_sync.py                Daily cloud-history sync -> Prometheus (remote-write)
  enphase_v4_backfill.py         One-off cloud backfill via remote-write
  enphase_export_openmetrics.py  Dump cloud history to OpenMetrics (for block import)
  enphase_get_token.py           Mint a gateway JWT via the entrez flow
  refresh_enphase_token.py       Same, self-contained (reads refresh.env)
  promrw.py                      Minimal Prometheus remote-write encoder (protobuf+snappy)
  run_sync.sh                    Wrapper for the systemd timer
  ameren_ingest.py               Ingest Ameren interval-data CSVs (import/export)
dashboards/
  enphase-live.json              Grafana: live production & per-inverter
  enphase-history.json           Grafana: energy totals & full history
  home-energy.json               Grafana: Ameren consumption vs solar flows
systemd/
  enphase-sync.service/.timer    Daily cloud sync
provisioning/
  docker-compose.enphase.yml     Drop-in compose service
  prometheus-additions.yml       Scrape job + out-of-order write setting
```

## Quick start

### 1. Live exporter (local gateway)

```bash
# Mint a gateway JWT (needs your Enlighten login). Valid ~1 year.
python3 scripts/enphase_get_token.py \
  --user you@example.com --serial YOUR_GATEWAY_SERIAL \
  --out /opt/appdata/enphase/token.txt

# Run it (see provisioning/docker-compose.enphase.yml)
docker compose up -d enphase-exporter
```

The exporter serves Prometheus metrics on `:9093`:

- `enphase_up`, `enphase_production_watts`, `enphase_production_watts_inverters`
- `enphase_inverter_last_report_watts{serial}` (per panel), `enphase_inverters_reporting`
- `enphase_production_wh_lifetime`, `enphase_consumption_*` (if CTs installed)
- `enphase_storage_soc_percent` (if a battery is installed)

### 2. Prometheus

Add the scrape job and the out-of-order window from
`provisioning/prometheus-additions.yml` to your `prometheus.yml`. The
`storage.tsdb.out_of_order_time_window` is required for the daily sync
(remote-write of slightly-past samples) and needs a **restart**.

### 3. Cloud history (Enphase API v4)

Register at <https://developer-v4.enphase.com> (free **Watt** plan, 1000
hits/month), create an app, authorize your own system. Note the **API key**,
**client id/secret**, and complete the OAuth flow to get a **refresh token**.

Put creds in `/opt/appdata/enphase/oauth.env`:

```
CLIENT_ID=...
CLIENT_SECRET=...
ACCESS_TOKEN=...
REFRESH_TOKEN=...
API_KEY=...
SYSTEM_ID=0000000
```

Backfill the full history once (imported as TSDB blocks — remote-write rejects
multi-year-old samples):

```bash
cd /opt/appdata/enphase
python3 enphase_export_openmetrics.py history.om
promtool tsdb create-blocks-from openmetrics history.om blocks/
# stop prometheus, back it up, copy blocks in (owned by the prometheus uid), start
```

Then let the **daily sync** keep it current:

```bash
sudo cp systemd/enphase-sync.* /etc/systemd/system/
sudo systemctl enable --now enphase-sync.timer
```

### 4. Grafana

Drop `dashboards/*.json` into your Grafana provisioning directory, or import
them manually. Two dashboards on purpose:

- **Live** (default `now-3h`) — live metrics only exist from the moment the
  exporter starts, so they look empty on a long range.
- **History** (default `now-90d`) — cloud history is ~1 sample/day.

### 5. Grid import/export (Ameren) — optional

If your utility provides interval-data CSV exports (Ameren's portal does —
**Green Button CMD is third-party businesses only**), no API is needed:

```bash
python3 scripts/ameren_ingest.py remotewrite ACE_Electric_*.csv
```

`Usage` maps to `ameren_import_watts`/`_wh_daily`, `Reverse Usage` (export)
to `ameren_export_watts`/`_wh_daily`. Importing more than ~3 weeks of history
at once requires temporarily raising `storage.tsdb.out_of_order_time_window`
(restart), pushing with `--window-days=N`, then reverting. Home consumption
is derived as *import + solar production − export*. Note the `source` labels
differ between metrics — wrap each term in `sum()` in PromQL or the join
comes back empty.

## Gotchas

- **Consumption** requires consumption CTs, which must be installed/enabled by
  an installer. Without them `net-consumption` is 0 and `total-consumption`
  mirrors production. Check `/ivp/meters` on the gateway.
- **Gateway JWT** expires ~1 year; **v4 refresh token** ~1 month. Alerts are
  provided for both failing.
- **Cloud daily series** is one sample/day — always wrap in
  `max_over_time(...[1d])` in PromQL so coarse Grafana steps don't miss samples.
- Metric name `enphase_production_wh_lifetime` exists in **two** flavours: the
  live exporter (no `source` label, gateway meter) and the cloud backfill
  (`source="cloud"`, daily-summed). Filter with `{source="cloud"}` for history.

## Security

No credentials are stored in this repo. `.gitignore` excludes `token.txt`,
`*.env` (except `.env.example`), and generated `.om` files. Never commit real
tokens.

## License

Personal project — no license specified.

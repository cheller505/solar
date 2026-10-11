# Enphase Solar → Prometheus → Grafana

Metrics from the Enphase **IQ Gateway** (IQ Envoy) on the LAN, scraped into the
existing Apollo Prometheus and visualised in Grafana. No Enphase cloud API is
used — the exporter talks to the gateway directly, so there are no rate limits
and data is live (20 s resolution).

## Hardware / system

| Item | Value |
|------|-------|
| Gateway model | Enphase IQ Gateway (800-00663-r05), software D8.3.5433 |
| Gateway LAN IP | `192.168.50.194` (HTTPS, self-signed cert) |
| Gateway serial | `202325167285` |
| Enlighten system | **Christopher Heller** (site id `5465864`) |
| Inverters | 40 |

The gateway was found by sweeping the /24 for hosts on :443 and matching the
TLS cert (`O = Enphase Energy`). Its `/info` endpoint is unauthenticated;
everything else needs a JWT.

## Components

| Component | Location |
|-----------|----------|
| Exporter code | `/opt/appdata/enphase/enphase_exporter.py` |
| Live JWT token | `/opt/appdata/enphase/token.txt` (mode 600) |
| Credentials for refresh | `/opt/appdata/enphase/refresh.env` (mode 600) |
| Refresh script | `/opt/apollo-rebuild/stack/scripts/refresh_enphase_token.py` |
| Token-mint script (manual) | `/opt/apollo-rebuild/stack/scripts/enphase_get_token.py` |
| Compose service | `enphase-exporter` in `/opt/apollo-rebuild/stack/docker-compose.yml` |
| Prometheus job | `enphase` in `prometheus.yml` (in `/opt/appdata/prometheus/`) |
| Alerts | `enphase` group in `/opt/appdata/prometheus/alert_rules.yml` |
| Grafana dashboard | `Enphase Solar — Live` (uid `enphase-live`) and `Enphase Solar — Energy & History` (uid `enphase-history`) |

## Metrics exposed

Namespace `enphase_`:

- `enphase_up` — 1 if the last gateway poll succeeded
- `enphase_production_watts` — live AC production (metered, eim)
- `enphase_production_watts_inverters` — sum of inverter-reported watts
- `enphase_production_wh_lifetime` — lifetime production energy
- `enphase_consumption_watts` / `enphase_consumption_wh_lifetime`
- `enphase_net_consumption_watts` / `enphase_net_consumption_wh_lifetime`
- `enphase_inverter_last_report_watts{serial}` — per-inverter current output
- `enphase_inverter_max_report_watts{serial}` — per-inverter peak
- `enphase_inverter_last_report_timestamp_seconds{serial}`
- `enphase_inverters_reporting` — count of reporting inverters (expect 40)
- `enphase_storage_soc_percent` — battery SoC (only if a battery is installed)

### Note on consumption metering

This system's `total-consumption` reading mirrors production and
`net-consumption` is ~0, which means **no consumption CTs are installed** (or
they're misconfigured). The consumption panels will therefore not show a real
house load until CTs are added. Production and per-inverter data are accurate.

## Token expiry — IMPORTANT

The gateway JWT is valid for roughly **one year**. When it expires, the
exporter's poll starts failing and `enphase_up` goes to 0 (the
`EnphaseGatewayDown` alert fires). To fix:

```bash
sudo python3 /opt/appdata/enphase/refresh_enphase_token.py
```

That logs into Enlighten, mints a new token, and writes it to `token.txt`. The
exporter re-reads the token on every poll, so **no restart is needed**. The
token swap is atomic (temp file + rename), so a poll never sees a partial file.

Set a reminder to run this ~11 months from the last refresh. Optionally add it
to cron with a systemd timer / `@monthly` entry that only re-mints if the
existing token's `exp` claim is near — but a simple annual reminder is enough.

## Historic data (imported)

**The local IQ Gateway has no usable history** — it only serves instantaneous
values and `today`/`7-day`/`lifetime` rollups; `start_date` params are ignored.
History comes from the **Enphase API v4** (cloud).

### Enphase API v4

Note: the **old v2 portal (`developer.enphase.com`) has signup disabled**. Use
**v4**: <https://developer-v4.enphase.com>. The free **Watt** plan (1000
hits/month, 10/min) is enough and includes site-level production monitoring
and `energy_lifetime`.

Credentials live in `/opt/appdata/enphase/oauth.env` (mode 600):

| Key | Notes |
|-----|-------|
| CLIENT_ID / CLIENT_SECRET | from the v4 app page |
| ACCESS_TOKEN | OAuth access token — **expires daily** |
| REFRESH_TOKEN | OAuth refresh token — **expires ~monthly** |
| API_KEY | application API key (sent in a header named `key`) |
| SYSTEM_ID | 5465864 |

Auth = `Authorization: Bearer <access_token>` header **plus** API key in a `key`
header. The export script refreshes the access token automatically.

If the **refresh token** expires (~monthly), re-authorize and re-run:
`https://api.enphaseenergy.com/oauth/authorize?response_type=code&client_id=YOUR_CLIENT_ID&redirect_uri=https://api.enphaseenergy.com/oauth/redirect_uri`

### What was imported

`energy_lifetime` gave **daily Wh for the whole system life** — 740 days
(2024-10-01 → present), ~41,731 kWh lifetime. Prometheus **rejects remote-write
samples older than its head window** ("out of bounds"), so multi-year history
was imported as **TSDB blocks** via `promtool tsdb create-blocks-from
openmetrics` (not remote write).

Cloud series (tagged `source="cloud"` to keep them apart from live data):

- `enphase_energy_wh_daily` — energy per day
- `enphase_production_wh_lifetime` — cumulative lifetime Wh (cloud)

> The *live* exporter also emits `enphase_production_wh_lifetime` **without**
> a `source` label, read from the gateway's meter (~31.4 MWh) — which differs
> from Enphase's daily-summed cloud value (~41.7 MWh). Always filter cloud
> history with `{source="cloud"}`.

Dashboard history panels use `max_over_time(...{source="cloud"}[3d])` so the
sparse one-sample-per-day data survives coarse Grafana steps.

### Re-running the import

```bash
cd /opt/appdata/enphase
# 1. export OpenMetrics (fetches a fresh token automatically)
sudo docker run --rm --network host -v $PWD:/w -w /w python:3.12-slim \
  python3 enphase_export_openmetrics.py /w/enphase_history.om
# 2. build blocks  (OpenMetrics timestamps are SECONDS — not milliseconds!)
sudo docker run --rm --user root -v $PWD:/w -w /w --entrypoint promtool \
  prom/prometheus:v3.13.1 tsdb create-blocks-from openmetrics /w/enphase_history.om /w/blocks
# 3. STOP prometheus, BACK IT UP, copy blocks in (owned by 65534), start
```

### Keeping cloud history current (daily sync)

The cloud series would go stale after the one-off backfill, so a **daily sync**
keeps it current:

- Script: `/opt/appdata/enphase/enphase_sync.py` (+ wrapper `run_sync.sh`)
- Timer: `enphase-sync.timer` (systemd, daily ~06:30) → `enphase-sync.service`
- What it does: fetches `energy_lifetime`, remote-writes the last 7 days into
  `enphase_energy_wh_daily`/`enphase_production_wh_lifetime` (`source="cloud"`).

This relies on Prometheus accepting recent out-of-order writes, enabled via
`storage.tsdb.out_of_order_time_window: 30d` in `prometheus.yml`. Without it the
remote-write receiver rejects the (slightly past) daily samples with
`out of bounds`. **That setting needs a Prometheus restart, not just a reload.**

Check/trigger:
```bash
systemctl list-timers enphase-sync.timer
sudo systemctl start enphase-sync.service   # run now
journalctl -u enphase-sync.service -n 20
```

Alert `EnphaseCloudSyncStale` fires if no new daily cloud sample lands for
>2 days (e.g. the v4 refresh token expired).

### Retention

Retention is **5y** (`--storage.tsdb.retention.time=5y`), raised from 180d so
the imported history isn't garbage-collected. Disk is plentiful (root fs 1.6 TB
free).

## ⚠️ Incident: TSDB history loss (2026-10-11)

While importing history, a cleanup command
(`rm -rf /opt/appdata/prometheus/data/01M4NP*`) meant to remove only the
badly-imported blocks **also matched real blocks** — ULIDs are time-ordered and
Prometheus's own fresh blocks shared the `01M4NP` prefix. The whole TSDB
(~5.5 GB, back to 2026-07-29) was wiped, with no backup (Prometheus is not in
the restic job, which covers only `/home/cheller` and NAS paths).

extundelete / ext4magic / raw scans found nothing — ext4 extent-based files are
not recoverable that way. **Those metrics are gone permanently.** Live
collection resumed immediately; only the Enphase history was re-imported.

Safeguards added since:
- Prometheus TSDB added to `restic-backup.sh` (weekly, repo `prometheus-tsdb`).
- Import uses explicit block paths — never globs on the live data dir.
- Retention 180d → 5y.
- OpenMetrics timestamps fixed to seconds; blocks verified (sane minTime/maxTime)
  before starting Prometheus.

## Recovery (2026-10-11) — what was recovered and how

After the wipe, an exhaustive search (entire `/`, docker/containerd layers,
`/ydrive`, `/xdrive`, `/wdrive`, and remote hosts **warspite, moe, birdie,
walnut, argus** over SSH incl. their restic repos) found THREE usable copies:

1. **NAS backup** — `/mnt/nas/ydrive/backups/apollo-prewipe-2026-07-12/var-lib/prometheus.tar`
   (5.9 GB, 18 blocks) covering **2026-01-19 → 2026-07-13**, label `instance="apollo"`.
2. **argus standby** — `/opt/appdata/prometheus/data` (5.5 GB, 17 blocks). Argus
   runs a **standby mirror of apollo's whole stack** (`/opt/apollo-standby`),
   synced by `apollo-standby-sync.sh`, so its Prometheus holds apollo's metrics
   for **2026-07-29 → 2026-10-11**. (Same block ULIDs as apollo's original TSDB —
   it was replicating apollo directly.)
3. **Starlink TSDB** — `/mnt/nas/ydrive/software/starlink/data/prometheus`
   (23 blocks, **2024-11-02 → 2024-11-17**).

### Resulting coverage (apollo node metrics)

| Period | Source | Status |
|--------|--------|--------|
| 2026-01-19 → 2026-07-13 | NAS prewipe backup | recovered |
| **2026-07-14 → 2026-07-28 (15 days)** | — | **permanently lost** |
| 2026-07-29 → 2026-08-13 | argus (label `node-exporter:9100`) | recovered |
| 2026-08-13 → 2026-10-11 | argus (label `apollo`) | recovered |
| 2026-10-11 → present | live | collecting |
| 2024-11-02 → 2024-11-17 | Starlink only | recovered |

So the incident lost **~15 days** of general infra metrics (Jul 14–28), not the
90 days first feared. Note: for the argus-sourced Jul 29–Aug 13 window the apollo
series are labelled `instance="node-exporter:9100"` (argus scraped the container
name) rather than `instance="apollo"`; query with `instance=~"apollo|node-exporter:9100"`
to see that stretch seamlessly.

Recovered metric families: node_exporter, blackbox/ping, cadvisor, target `up`,
ZFS, SMART, speedtest, Starlink — 1,439 metric names, TSDB ~11 GB.

Safety backups kept in `/opt/appdata/`: `prometheus-tsdb-before-import/merge/
starlink/argus-*.tar.gz` (4 files, ~6.3 GB total).

### Other hosts checked

- **warspite** — no apollo Prometheus; has a 2024 Starlink TSDB (recovered) +
  app files. Its restic repos hold photos/docs only.
- **moe** — no Prometheus (only node-exporter).
- **birdie** — its OWN small Prometheus (Raspberry Pi node only, Sep 26→Oct 11);
  not apollo data.
- **walnut** — `/var/lib/prometheus` is an empty node-exporter textfile dir;
  a restic repo on `/mnt/backup` (password unknown, not apollo data).
- **argus** — the standby mirror (main recovery source, see above).

### Recovering from a backup again (procedure)

1. Extract the tar / copy `01*` block dirs; confirm each has `meta.json`.
2. `promtool tsdb list <dir>` to verify and read min/max times.
3. Load into a THROWAWAY Prometheus first to confirm series before touching live.
4. Stop prometheus, `tar czf` the current data dir, copy blocks in owned by
   65534:65534, start prometheus.
5. Never delete blocks on the live TSDB with a glob.

## Verifying it works

```bash
# exporter directly
curl -s http://127.0.0.1:9093/metrics | grep '^enphase_'

# prometheus target health
curl -s 'http://127.0.0.1:9090/api/v1/targets' | grep -o '"job":"enphase"[^}]*'

# query a value
curl -s 'http://127.0.0.1:9090/api/v1/query?query=enphase_production_watts'
```

Grafana dashboard: <http://192.168.50.2:3000/d/enphase-solar-apollo>

## Dashboards

Two dashboards (split because the live and historical metrics need very
different time ranges/step sizes):

- **Enphase Solar — Live** (uid `enphase-live`), default range `now-3h`:
  production now, inverter sum, reporting count, gateway status, today's kWh,
  real-time production graph, per-inverter output (timeseries + bar gauges).
- **Enphase Solar — Energy & History** (uid `enphase-history`), default range
  `now-90d`: lifetime, last-day, rolling 30d/365d totals, daily-production bars
  for the full ~2y of cloud history, cumulative lifetime curve.

> Why split: the live `enphase_*` metrics only exist from today, so on a 90-day
> view they collapse to ~1 point ("no data"). Cloud-history metrics only have
> 1 sample/day, so on a 3-hour view they'd be flat. Per-panel time overrides
> would work but two dashboards are simpler and clearer.

> Query note: the cloud daily series is one sample per day, so wrap it in
> `max_over_time(...[1d])` (or `[3d]` for "latest") before doing anything else;
> otherwise coarse Grafana steps miss the samples. Period totals use
> `sum(sum_over_time((max_over_time(...[1d]) / 1000)[30d:1d]))`.

## Changing the exporter

The exporter script is bind-mounted, so after editing
`/opt/appdata/enphase/enphase_exporter.py` just restart the container:

```bash
cd /opt/apollo-rebuild/stack && sudo docker compose restart enphase-exporter
```

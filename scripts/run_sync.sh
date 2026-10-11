#!/bin/bash
# Daily Enphase cloud-history sync. Runs the sync script in a throwaway
# python container (needs requests + python-snappy). Invoked by
# enphase-sync.timer. Logs to the journal/syslog.
set -uo pipefail
exec docker run --rm --network host \
  -v /opt/appdata/enphase:/w -w /w python:3.12-slim \
  sh -c "pip install -q requests python-snappy >/dev/null 2>&1 && python3 enphase_sync.py"

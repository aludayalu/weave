#!/usr/bin/env bash
# =====================================================================
# meridian-erp tribal-knowledge dataset — full pipeline, start to finish
#
#   bronze  -> silver -> detection -> gold -> validation -> Lakebase
#
# Runs anywhere: laptop or Databricks notebook. Set SYNTH_DATA_ROOT if the
# synth-data folder is not next to this scripts/ directory.
# =====================================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
export PYTHONPATH="$HERE/..:$HERE:${PYTHONPATH:-}"
PY="${PYTHON:-python3}"

banner() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

banner "0/6  where am I?"
$PY -c "from _paths import SYN, REPO; print('dataset :', SYN); print('repo    :', REPO)"

banner "1/6  BRONZE — generate the three raw streams + attached documents"
$PY gen_streams.py
$PY gen_discord_jira.py
$PY gen_github.py
# then dirty them: a real ingest is never clean, and silver needs something to fix
$PY inject_bronze_noise.py >/dev/null && echo "bronze noise injected"

banner "2/6  SILVER — clean, type, dedupe, link, quarantine"
$PY silver_clean.py | tail -4

banner "3/6  DETECTION — the model reads silver and picks task timeframes"
$PY gold_detect.py run
$PY gold_detect.py score | head -9

banner "4/6  GOLD — carve each timeframe into per-task slices"
$PY dissect_to_gold.py

banner "5/6  VALIDATE — raw schemas, then the whole chain"
$PY validate_streams.py | tail -6
$PY validate_pipeline.py

banner "6/6  LAKEBASE — load (skipped unless credentials are configured)"
if [ -f "${SYNTH_DATA_ROOT:-}/lakebase/.env" ] || [ -f ../opencode/synth-data/lakebase/.env ]; then
    $PY ../lakebase/check_connection.py || echo "(not connected — see scripts/lakebase/README.md)"
else
    echo "no .env yet — see scripts/lakebase/README.md for the two auth shapes"
fi

banner "done"
$PY - <<'PYEOF'
import json, pathlib
from _paths import GOLD
idx = json.loads((GOLD / "_index.json").read_text())["tasks"]
print(f"{len(idx)} gold task slices in {GOLD}")
print(f"  {sum(t['discord_messages'] for t in idx)} discord messages")
print(f"  {sum(t['commits'] for t in idx)} commits, {sum(t['patch_bytes'] for t in idx)} patch bytes")
print(f"  {sum(t['documents'] for t in idx)} documents")
PYEOF

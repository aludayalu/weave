#!/usr/bin/env bash
# End-to-end build: bronze -> silver -> detection -> gold -> validation.
set -e
cd "$(dirname "$0")"
echo "== 1. bronze: raw streams + documents (deliberately messy)"
python3 tools/gen_streams.py
python3 tools/gen_discord_jira.py
python3 tools/gen_github.py
python3 tools/inject_bronze_noise.py > /dev/null
echo "== 2. silver: clean, type, dedupe, link, quarantine"
python3 tools/silver_clean.py | tail -3
echo "== 3. detection loop: model picks task timeframes from silver"
python3 tools/gold_detect.py run
python3 tools/gold_detect.py score | head -9
echo "== 4. gold: carve each timeframe into per-task slices"
python3 tools/dissect_to_gold.py
echo "== 5. validate"
python3 tools/validate_pipeline.py

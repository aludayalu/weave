"""Locate the dataset root, wherever this is running from.

Resolution order:
  1. $SYNTH_DATA_ROOT
  2. ./synth-data            (workspace: scripts/ and synth-data/ side by side)
  3. ../synth-data
  4. the original local checkout

Every pipeline script imports this, so the same file runs on a laptop and in a
Databricks notebook without edits.
"""
import os
from pathlib import Path

LOCAL_DEFAULT = Path("/Users/aludayalu/weave/opencode/synth-data")


def synth_root() -> Path:
    env = os.environ.get("SYNTH_DATA_ROOT")
    if env and Path(env).is_dir():
        return Path(env)
    here = Path(__file__).resolve().parent
    for cand in (here / "synth-data", here.parent / "synth-data",
                 here.parent.parent / "synth-data"):
        if cand.is_dir():
            return cand
    if LOCAL_DEFAULT.is_dir():
        return LOCAL_DEFAULT
    raise SystemExit(
        "Could not find the synth-data folder.\n"
        "Set SYNTH_DATA_ROOT=/path/to/synth-data and re-run.")


def scripts_root() -> Path:
    return Path(__file__).resolve().parent


SYN = synth_root()
LAKEBASE = SYN / "lakebase"
RAW = SYN / "raw"
SILVER = SYN / "silver"
DETECTED = SYN / "detected"
GOLD = SYN / "gold"
REPO = SYN.parent / "meridian"

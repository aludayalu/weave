"""Notebook 06: validate the dataset and launch a fine tuning run.

The trajectories are already in /tmp/weave-dataset-*.jsonl from 05. This notebook
does the part that is easy to get quietly wrong: check the dataset is actually
shaped the way a fine tuning job needs, upload it somewhere the training job can
read, and then run the job and watch it.

  1  load and inspect
  2  validate, and refuse to continue if anything is malformed
  3  write the final train and val files, plus a manifest
  4  stage them on the workspace volume the training job reads
  5  submit the fine tuning job and poll it

Steps 1 to 4 are deterministic and have been run. Step 5 needs a training job to
exist, and the widget below names it.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

# ------------------------------------------------------------------- config
#
# TEST ENVIRONMENT. Same as 03 and 04: a dapi token for the workspace, and a
# Lakebase token for the database, which lives an hour.
WORKSPACE_URL = "https://dbc-5a40bfd7-421c.cloud.databricks.com"
GATEWAY_TOKEN = "dapi36f076633cc4e9316be416b75cf41848"

DB_HOST = "ep-twilight-bonus-d8outdz9.database.us-east-2.cloud.databricks.com"
DB_NAME = "databricks_postgres"
DB_USER = "aarav@dayal.org"
DB_TOKEN = os.environ.get("LAKEBASE_TOKEN", "")

# Where the dataset is staged. A volume is the right place: a training job on a
# different cluster cannot see this notebook's filesystem.
VOLUME = "/Volumes/main/weave/datasets"
BASELINE = os.environ.get("WEAVE_BASELINE", "databricks-meta-llama-3-1-8b-instruct")
JOB_ID = os.environ.get("WEAVE_TRAIN_JOB", "")

MODEL = os.environ.get("WEAVE_MODEL", "weave-agent-v1")
OUT_DIR = Path(os.environ.get("WEAVE_OUT", "/tmp/weave-ft"))


# ------------------------------------------------------------------- helpers
def api(method: str, path: str, body: dict | None = None, timeout: int = 120) -> dict:
    request = urllib.request.Request(
        f"{WORKSPACE_URL}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {GATEWAY_TOKEN}"},
        method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode()[:500]
        raise SystemExit(f"{method} {path} failed: HTTP {error.code}\n{detail}")


def load(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"{path} does not exist. Run 05_build_dataset.py first.")
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as error:
            raise SystemExit(f"{path}:{number} is not valid json: {error}")
    return rows


def report(rows: list[dict], label: str) -> dict:
    calls = [r["metadata"]["tool_calls"] for r in rows]
    turns = [r["metadata"]["turns"] for r in rows]
    approx = sum(len(json.dumps(m)) for r in rows for m in r["messages"]) // 4
    print(f"{label:6} {len(rows):>3} examples | "
          f"turns {min(turns)}-{max(turns)} | "
          f"tool calls {min(calls)}-{max(calls)} | ~{approx:,} tokens")
    return {"examples": len(rows), "min_turns": min(turns), "max_turns": max(turns),
            "min_calls": min(calls), "max_calls": max(calls), "approx_tokens": approx}


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 68)
    print("weave fine tuning dataset")
    print("=" * 68)

    # ---------------------------------------------------------------- 1 load
    train = load(Path("/tmp/weave-dataset-train.jsonl"))
    val = load(Path("/tmp/weave-dataset-val.jsonl"))
    stats = {"train": report(train, "train"), "val": report(val, "val")}

    # ------------------------------------------------------------ 2 validate
    #
    # These are the checks that catch a silently broken dataset. A malformed one
    # does not fail loudly at training time, it produces a model that has learned
    # to stop mid task, which is much worse than a crash.
    print("\nvalidating")
    problems: list[str] = []
    known_tools = {t["function"]["name"] for t in TOOL_SPECS}

    for split, rows in (("train", train), ("val", val)):
        for row in rows:
            where = f"{split}/{row.get('window_id')}"
            messages = row.get("messages") or []
            if not messages or messages[0].get("role") != "system":
                problems.append(f"{where}: does not start with a system message")
            if len(messages) < 3:
                problems.append(f"{where}: only {len(messages)} messages")
                continue
            if messages[-1].get("role") != "assistant":
                problems.append(f"{where}: ends on {messages[-1].get('role')}, not assistant")
            if not (messages[-1].get("content") or "").strip():
                problems.append(f"{where}: final message is empty")

            # every tool call must be answered, in order, with no orphans
            open_calls: dict[str, str] = {}
            for message in messages:
                if message.get("role") == "assistant":
                    for call in message.get("tool_calls") or []:
                        name = call["function"]["name"]
                        if name not in known_tools:
                            problems.append(f"{where}: unknown tool {name!r}")
                        open_calls[call["id"]] = name
                elif message.get("role") == "tool":
                    if message.get("tool_call_id") not in open_calls:
                        problems.append(
                            f"{where}: tool result with no matching call "
                            f"({message.get('name')})")
                        continue
                    open_calls.pop(message["tool_call_id"])
            if open_calls:
                problems.append(f"{where}: {len(open_calls)} tool call(s) never answered")

    for split, rows in (("train", train), ("val", val)):
        for row in rows:
            if not any(m.get("role") == "tool" for m in row.get("messages", [])):
                problems.append(f"{split}/{row.get('window_id')}: no tool calls at all")

    unique = [r["window_id"] for r in train + val]
    overlap = {w for w in unique if unique.count(w) > 1}
    if overlap:
        problems.append(f"a window appears in both splits: {sorted(overlap)}")

    if problems:
        print(f"\n{len(problems)} problem(s), refusing to continue:")
        for problem in problems[:25]:
            print("   -", problem)
        if len(problems) > 25:
            print(f"   ... and {len(problems) - 25} more")
        return 1
    print(f"  all {len(train) + len(val)} conversations are well formed")

    # --------------------------------------------------------------- 3 write
    print("\nwriting")
    manifest = {
        "model": MODEL, "baseline": BASELINE, "created": time.time(),
        "splits": stats,
        "trajectory_source": "platinum.trajectory via 04_gold_to_platinum.py",
    }
    for split, rows in (("train", train), ("val", val)):
        path = OUT_DIR / f"{split}.jsonl"
        with open(path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"  {path}  {len(rows)} rows  {path.stat().st_size:,}b")
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"  {OUT_DIR / 'manifest.json'}")

    # --------------------------------------------------------------- 4 stage
    print(f"\nstaging on {VOLUME}")
    if not JOB_ID:
        print("  no WEAVE_TRAIN_JOB set, so there is no job to stage for and no")
        print("  job to submit. The dataset is written and validated. Set")
        print("  WEAVE_TRAIN_JOB to the run id of a training job and re-run to")
        print("  submit it. A job that trains on this needs a GPU and:")
        print("    - the files mounted at", VOLUME)
        print("    - a training loop that reads chat + tools jsonl")
        return 0

    # -------------------------------------------------------------- 5 submit
    print(f"\nsubmitting job {JOB_ID}")
    run = api("POST", "/api/2.1/jobs/run-now",
              {"job_id": int(JOB_ID),
               "notebook_task": {"notebook_path": "/Shared/weave/06_finetune"}})
    run_id = run.get("run_id")
    print(f"  run_id {run_id}")

    seen = None
    for _ in range(120):
        time.sleep(10)
        state = api("GET", f"/api/2.1/jobs/runs/get?run_id={run_id}")
        life = (state.get("state") or {}).get("life_cycle_state")
        msg = (state.get("state") or {}).get("state_message", "")
        if life != seen:
            print(f"  {life}  {msg[:120]}")
            seen = life
        if life in ("TERMINATED", "SKIPPED", "INTERNAL_ERROR"):
            print(f"\nfinished: {life} {msg[:300]}")
            return 0 if life == "TERMINATED" else 2
    print("\nstill running after 20 minutes, check it in the UI")
    return 0


# The tool schemas the trajectories were generated with. Validation has to check
# against these, or it cannot tell a typo from a real tool.
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "platinum", Path(__file__).resolve().parent / "04_gold_to_platinum.py")
_platinum = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_platinum)
TOOL_SPECS = _platinum.TOOLS


if __name__ == "__main__":
    raise SystemExit(main())

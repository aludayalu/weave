"""Notebook 05: build the fine tuning dataset from the platinum trajectories.

Reads platinum.run and platinum.trajectory, which notebook 04 wrote, and turns
each window into one training conversation: the system prompt, the task, and the
whole ordered exchange including every tool call and every tool result.

The unit of training is the whole trajectory, not the individual turn. A turn on
its own has no meaning, because the interesting part is the model noticing that
a tool result contradicts what it expected and changing its mind.

Output is jsonl in the chat completions shape, with tools attached, which is what
an OpenAI style fine tuning job takes and what the chat harness already speaks.

  python3 05_build_dataset.py --out /tmp/dataset.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# %% imports

import psycopg
from psycopg.rows import dict_row

# Same shape as 04. One list so the prompt a model is trained on is byte for
# byte the prompt it was asked to answer under.
import importlib.util

_here = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("platinum", _here / "04_gold_to_platinum.py")
platinum = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(platinum)

MASTER_SYSTEM_PROMPT = platinum.MASTER_SYSTEM_PROMPT
TASK_INSTRUCTION = platinum.TASK_INSTRUCTION
TOOLS = platinum.TOOLS
DB_HOST, DB_NAME, DB_USER = platinum.DB_HOST, platinum.DB_NAME, platinum.DB_USER

# Tool names the model emitted that are not real tools. They came from the model
# writing a shell command into the name field, and they are dropped rather than
# kept, because training on them teaches the harness to call a tool named
# `cat some/file`.
KNOWN_TOOLS = {t["function"]["name"] for t in TOOLS}


# %% connect

def connect():
    token = os.environ.get("LAKEBASE_TOKEN", "")
    if not token:
        raise SystemExit("set LAKEBASE_TOKEN; it lives an hour, so mint a fresh one")
    return psycopg.connect(host=DB_HOST, port=5432, dbname=DB_NAME, user=DB_USER,
                           password=token, sslmode="require", connect_timeout=30,
                           row_factory=dict_row)


# %% one window into one conversation

def build_example(run: dict, steps: list[dict], windows: dict) -> dict | None:
    """One window becomes one conversation in OpenAI chat format."""
    messages: list[dict] = [
        {"role": "system", "content": MASTER_SYSTEM_PROMPT},
        {"role": "user", "content": TASK_INSTRUCTION.format(
            start=run["start_ts"], end=run["end_ts"])
         + f"\nThe ticket is {run.get('task_id') or run['window_id']}: "
           f"{run.get('title') or 'untitled'}.\n"},
    ]

    kept = dropped = 0
    # assistant steps are stored one row per tool call, sharing a turn, so group
    # them back together or the conversation reads as several separate replies
    by_turn: dict[int, list[dict]] = {}
    for step in steps:
        by_turn.setdefault(step["turn"], []).append(step)

    for turn in sorted(by_turn):
        rows = by_turn[turn]
        prose = next((r["content"] for r in rows if r["content"]), "")
        calls = []
        results = []
        for row in rows:
            name = row["tool_name"]
            if name is None:
                continue
            if name not in KNOWN_TOOLS:
                dropped += 1
                continue
            call_id = f"call_{uuid.uuid4().hex[:16]}"
            calls.append({"id": call_id, "type": "function", "function": {
                "name": name, "arguments": json.dumps(row["tool_args"] or {})}})
            # tool_call_id, not id. A tool result is matched to its call by that
            # key, and using id leaves every result orphaned, which the validator
            # in 06 then reports as 696 problems.
            results.append({"tool_call_id": call_id, "role": "tool", "name": name,
                            "content": (row["tool_result"] or "")[:12000]})
            kept += 1

        if not calls:
            if prose:
                messages.append({"role": "assistant", "content": prose})
            continue
        message = {"role": "assistant", "content": prose or None, "tool_calls": calls}
        messages.append(message)
        messages.extend(results)

    if len(messages) < 3:
        return None
    if not any(m.get("role") == "tool" for m in messages):
        return None
    # The closing handoff is stored on the run, not as a trajectory row, because
    # it carries no tool calls and rows are written per call. Without appending
    # it here every conversation ended on a tool result, and a conversation that
    # ends on a tool result teaches the model to stop mid task with no reply.
    final = (run.get("final") or "").strip()
    if final:
        messages.append({"role": "assistant", "content": final})

    if len(messages) < 3:
        return None
    if not any(m.get("role") == "tool" for m in messages):
        return None
    if messages[-1]["role"] != "assistant" or not (messages[-1].get("content") or "").strip():
        return None

    return {
        "id": f"traj_{run['window_id']}",
        "window_id": run["window_id"],
        "task_id": run.get("task_id"),
        "title": run.get("title"),
        "messages": messages,
        "metadata": {
            "turns": run["turns"], "tool_calls": kept, "dropped_tool_names": dropped,
            "tokens_in": run["tokens_in"], "tokens_out": run["tokens_out"],
            "has_final": bool(run.get("final")),
        },
    }, kept, dropped


# %% run

# %% run

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="/tmp/weave-dataset.jsonl")
    parser.add_argument("--val-fraction", type=float, default=0.15)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--stats-only", action="store_true")
    args = parser.parse_args()

    # %% load the trajectories
    with connect() as conn, conn.cursor() as cur:
        # platinum.run has no start_ts, the window does. Join so the ordering
        # and the timestamps both come from gold.
        cur.execute(
            "SELECT r.*, g.start_ts, g.end_ts FROM platinum.run r "
            "JOIN gold.task_window g USING (window_id) "
            "WHERE r.status = 'succeeded' ORDER BY g.start_ts")
        runs = cur.fetchall()

    print(f"runs: {len(runs)} succeeded trajectories joined to gold")

    # The table accumulated repeats from earlier test runs, so 32 windows had
    # produced 52 succeeded rows and the dataset had 31 examples over 24
    # windows. Keep the newest run per window, otherwise the same task is
    # trained on three times with three different endings.
    newest: dict[str, dict] = {}
    for run in runs:
        current = newest.get(run["window_id"])
        if current is None or (run.get("finished_at") or 0) >= (current.get("finished_at") or 0):
            newest[run["window_id"]] = run
    if len(newest) != len(runs):
        print(f"deduped {len(runs)} runs to {len(newest)} windows, "
              f"keeping the most recent each")
    runs = list(newest.values())
    runs.sort(key=lambda r: r["start_ts"])

    # build and report
    examples, kept_total, dropped_total = [], 0, 0
    for run in runs:
        if not run.get("final"):
            continue
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM platinum.trajectory WHERE window_id = %s "
                        "ORDER BY turn, step", (run["window_id"],))
            steps = cur.fetchall()
        built = build_example(run, steps, {})
        if not built:
            continue
        example, kept, dropped = built
        examples.append(example)
        kept_total += kept
        dropped_total += dropped

    if not examples:
        print("no usable trajectories")
        return 1

    # stats
    turns = [e["metadata"]["turns"] for e in examples]
    calls = [e["metadata"]["tool_calls"] for e in examples]
    finals = sum(1 for e in examples if e["metadata"]["has_final"])
    print(f"\nexamples      {len(examples)}")
    print(f"with handoff  {finals}")
    print(f"turns         min {min(turns)} median {sorted(turns)[len(turns)//2]} "
          f"max {max(turns)}")
    print(f"tool calls    min {min(calls)} median {sorted(calls)[len(calls)//2]} "
          f"max {max(calls)}")
    print(f"tool calls kept    {kept_total:,}   dropped as invalid {dropped_total:,}")

    roles: dict[str, int] = {}
    for example in examples:
        for message in example["messages"]:
            roles[message["role"]] = roles.get(message["role"], 0) + 1
    print(f"messages      {roles}")
    approx = sum(len(json.dumps(m)) for e in examples for m in e["messages"]) // 4
    print(f"approx tokens {approx:,}")

    if args.stats_only:
        return 0

    # split and write
    random.Random(args.seed).shuffle(examples)
    split = max(1, int(len(examples) * args.val_fraction))
    val, train = examples[:split], examples[split:]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    for name, rows in (("train", train), ("val", val)):
        path = out.with_name(out.stem + f"-{name}.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"wrote {path}  {len(rows)} rows")

    with open(out, "w", encoding="utf-8") as handle:
        for row in examples:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {out}  {len(examples)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

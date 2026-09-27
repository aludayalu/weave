#!/usr/bin/env python3
"""TASK DETECTION (pre-gold): the model ingests silver and chooses timeframes.

The detection MODEL ingests silver and decides task timeframes. Its output IS
gold. This script is the harness around that call:

  * `run`    - local deterministic stand-in for the model, writing the exact
               gold payload shape, so the dissector and the scorer are runnable
               end to end without a model endpoint. Swap the detector body for a
               Databricks Model Gateway call (see --endpoint) and the rest of the
               pipeline is unchanged.
  * `score`  - compares a gold file (yours or the model's) against
               gold/reference_windows.json and reports boundary error.

Usage:
  python3 gold_detect.py run
  python3 gold_detect.py run --endpoint http://...   # real model call
  python3 gold_detect.py score gold/task_windows.json
"""

import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import SYN, REPO  # noqa: E402
import json, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SIL, GOLD, DET = SYN / "silver", SYN / "gold", SYN / "detected"
GOLD.mkdir(exist_ok=True); DET.mkdir(exist_ok=True)


def load_jsonl(p):
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def epoch(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def to_utc(e):
    return datetime.fromtimestamp(e, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- detector
def detect():
    """Stand-in for the detection model: consumes SILVER only and emits the
    timeframe payload. A real run replaces this body with a Model Gateway call
    using specs/task_detection_prompt.md; the output shape is identical."""
    msgs = load_jsonl(SIL / "discord_messages.jsonl")
    events = load_jsonl(SIL / "jira_changelog_events.jsonl")
    prs = load_jsonl(SIL / "github_pull_requests.jsonl")

    merged = {p["pr_number"]: p for p in prs if p["merged"]}
    closed = {e["issue_key"]: e["ts_utc"] for e in events
              if e["field"] == "status" and e["to_value"] == "Done"}

    # every silver message that references a ticket, grouped by ticket
    by_ticket = {}
    for m in msgs:
        for t in m["mentions_ticket"]:
            by_ticket.setdefault(t, []).append(m)

    tasks = []
    for key, group in sorted(by_ticket.items()):
        if key not in closed:
            continue                                  # not finished -> not a task
        group.sort(key=lambda r: r["ts_epoch"])
        prnums = sorted({p for m in group for p in m["mentions_pr"] if p in merged})
        if not prnums:
            continue
        # the PR that closed THIS ticket: the mentioned PR whose merge lands
        # closest to the ticket's own Done transition (not merely the latest,
        # since one thread can mention several PRs)
        done = epoch(closed[key])
        num = min(prnums, key=lambda n: abs(epoch(merged[n]["merged_ts_utc"]) - done))
        pr = merged[num]
        start = group[0]["ts_utc"]
        end = max(closed[key], pr["merged_ts_utc"])
        if epoch(end) < epoch(start):
            continue
        window = [m for m in group if epoch(start) <= m["ts_epoch"] <= epoch(end)]
        tasks.append({
            "task_id": key.lower(),
            "title": window[0]["content"][:90],
            "start_ts_utc": start,
            "end_ts_utc": end,
            "confidence": 0.95 if abs(epoch(closed[key]) - epoch(pr["merged_ts_utc"])) < 3600 else 0.7,
            "summary": f"{key} raised in #{window[0]['channel']}, shipped in PR #{num}",
            "participants": sorted({m["author_name"] for m in window}),
            "evidence": {
                "discord": {"channel": window[0]["channel"],
                            "start_message_id": window[0]["message_id"],
                            "end_message_id": window[-1]["message_id"],
                            "supporting_message_ids": [m["message_id"] for m in window]},
                "jira": {"issue_keys": [key], "closing_event_ts_utc": closed[key]},
                "github": {"merge_shas": [pr["merge_sha"]], "pr_numbers": [num],
                            "branch": pr["branch"]},
                "documents": [],
            },
        })
    tasks.sort(key=lambda t: t["start_ts_utc"])
    return {"produced_by": "local-standin-detector", "task_count": len(tasks), "tasks": tasks}


# ---------------------------------------------------------------- scorer
def score(path):
    ref = json.loads((GOLD / "reference_windows.json").read_text())["tasks"]
    hyp = json.loads(Path(path).read_text())["tasks"]
    rid = {t["jira"]: t for t in ref}
    matched, exact, tot = 0, 0, 0
    errs = []
    for h in hyp:
        tot += 1
        tk = (h.get("evidence", {}).get("jira", {}).get("issue_keys") or [None])[0]
        if tk not in rid:
            errs.append(f"hallucinated task {tk}")
            continue
        matched += 1
        r = rid[tk]
        ds = abs(epoch(h["start_ts_utc"]) - epoch(r["window"]["start"])) / 60
        de = abs(epoch(h["end_ts_utc"]) - epoch(r["window"]["end"])) / 60
        if ds <= 5 and de <= 5:
            exact += 1
        else:
            errs.append(f"{tk}: start {ds:.1f}m, end {de:.1f}m off")
    precision = matched / tot if tot else 0
    recall = matched / len(ref) if ref else 0
    print(json.dumps({"hypotheses": tot, "reference": len(ref), "matched": matched,
                      "boundaries_exact_within_5min": exact,
                      "precision": round(precision, 3), "recall": round(recall, 3),
                      "f1": round(2 * precision * recall / (precision + recall), 3) if precision + recall else 0},
                     indent=2))
    for e in errs[:15]:
        print("  -", e)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        out = detect()
        (DET / "task_windows.json").write_text(json.dumps(out, indent=2))
        print(json.dumps({"written": "detected/task_windows.json", "tasks": out["task_count"]}))
    elif cmd == "score":
        score(sys.argv[2] if len(sys.argv) > 2 else str(DET / "task_windows.json"))

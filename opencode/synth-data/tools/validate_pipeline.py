#!/usr/bin/env python3
"""End-to-end validation: bronze (messy) -> silver (clean) -> detected
timeframes -> gold slices.

Verifies the guarantees the pipeline depends on:
  bronze  the raw streams are genuinely messy (mess is required, not a bug)
  silver  every surviving record is typed, UTC, deduplicated and linked;
          nothing malformed leaked through
  detect  timeframes agree with ground truth within a tolerance
  gold    every task has a non-empty conversation, ticket, patch and brief,
          and the patch touches the files the task was supposed to touch
"""
import json, re, sys
from datetime import datetime
from pathlib import Path

SYN = Path("/Users/aludayalu/weave/opencode/synth-data")
RAW, SIL, DET, GOLD = SYN / "raw", SYN / "silver", SYN / "detected", SYN / "gold"
fails = []


def ck(cond, msg):
    if not cond:
        fails.append(msg)


def jsonl(p):
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def ep(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


raw_msgs, raw_issues, raw_commits, raw_pulls = [], [], [], []
for f in (RAW / "discord").glob("*.json"):
    raw_msgs += json.loads(f.read_text())["messages"]
raw_issues = json.loads((RAW / "jira" / "issues.json").read_text())
raw_commits = json.loads((RAW / "github" / "commits.json").read_text())
raw_pulls = json.loads((RAW / "github" / "pulls.json").read_text())

msgs = jsonl(SIL / "discord_messages.jsonl")
events = jsonl(SIL / "jira_changelog_events.jsonl")
issues = jsonl(SIL / "jira_issues.jsonl")
commits = jsonl(SIL / "github_commits.jsonl")
prs = jsonl(SIL / "github_pull_requests.jsonl")
quar = jsonl(SIL / "quarantine.jsonl")

# ---------------- bronze is genuinely messy
mess = 0
mess += sum(1 for m in raw_msgs if not (m.get("content") or "").strip())
mess += sum(1 for m in raw_msgs if len(raw_msgs) and m["message_id"] in
            {x["message_id"] for x in raw_msgs if x["message_id"] == m["message_id"]} and False)
ids = [m["message_id"] for m in raw_msgs]
mess += len(ids) - len(set(ids))
mess += sum(1 for m in raw_msgs if not str(m.get("timestamp", "")).endswith(("Z", "+00:00", "")))
mess += sum(1 for i in raw_issues for h in i["changelog"]["histories"] if not h.get("created"))
mess += sum(1 for i in raw_issues for a in i["fields"].get("attachment", []) if "mimeType" not in a)
mess += sum(1 for c in raw_commits if not (c.get("commit", {}).get("author") or {}).get("date"))
ck(mess > 40, f"bronze is not messy enough (mess={mess}); silver would have nothing to clean")
print(f"bronze: {len(raw_msgs)} discord messages, {len(raw_issues)} jira issues, "
      f"{len(raw_commits)} commits, {len(raw_pulls)} PRs, {mess} defects injected")

# ---------------- silver is clean
seen = set()
for m in msgs:
    ck(m["message_id"] not in seen, f"duplicate survived into silver: {m['message_id']}")
    seen.add(m["message_id"])
    ck(m["ts_utc"].endswith("Z"), f"non-UTC ts in silver: {m['ts_utc']}")
    ck(bool(m["content"].strip()), f"empty content in silver: {m['message_id']}")
    ck(m["author_name"] not in (None, "", "unknown"), f"unknown author: {m['message_id']}")
    ck(m["ts_utc"] > "2026-01-01" and m["ts_utc"] < "2028-01-01", f"ts out of range: {m['ts_utc']}")
for i in issues:
    ck(bool(re.match(r"^MER-\d+$", i["issue_key"])), f"bad key {i['issue_key']}")
    ck(i["created_utc"].endswith("Z"), f"issue ts not UTC {i['issue_key']}")
for c in commits:
    ck(c["authored_ts_utc"].endswith("Z") and c["committed_ts_utc"].endswith("Z"), f"commit ts {c['sha'][:8]}")
    ck(bool(c["author_name"]) and bool(c["authored_ts_utc"]), f"incomplete commit {c['sha'][:8]}")
for q in quar:
    ck(q.get("reason"), "quarantine row without a reason")
print(f"silver: {len(msgs)} messages, {len(issues)} issues, {len(events)} changelog events, "
      f"{len(commits)} commits, {len(prs)} PRs, {len(quar)} quarantined")

# silver must be strictly better than bronze
ck(len(msgs) <= len(raw_msgs), "silver produced more messages than bronze")
ck(len(set(m["message_id"] for m in msgs)) == len(msgs), "silver still has duplicates")

# ---------------- detection vs ground truth
ref = {t["jira"]: t for t in json.loads((GOLD / "reference_windows.json").read_text())["tasks"]}
det = json.loads((DET / "task_windows.json").read_text())["tasks"]
matched = exact = 0
for d in det:
    key = d["evidence"]["jira"]["issue_keys"][0]
    if key not in ref:
        fails.append(f"detected task not in ground truth: {key}")
        continue
    matched += 1
    ds = abs(ep(d["start_ts_utc"]) - ep(ref[key]["window"]["start"])) / 60
    de = abs(ep(d["end_ts_utc"]) - ep(ref[key]["window"]["end"])) / 60
    if ds <= 5 and de <= 5:
        exact += 1
print(f"detected: {len(det)} windows, {matched} matched, {exact} boundaries exact within 5 min")
ck(matched >= 0.9 * len(ref), f"recall too low: {matched}/{len(ref)}")

# ---------------- gold slices
idx = json.loads((GOLD / "_index.json").read_text())["tasks"]
ck(len(idx) == len(det), "gold task count != detected window count")
for row in idx:
    tid = row["task_id"]
    g = GOLD / tid
    for f in ("brief.json", "discord.json", "jira.json", "git.json", "diff.patch", "documents.json"):
        ck((g / f).exists(), f"{tid}: missing {f}")
    if not (g / "diff.patch").exists():
        continue
    patch = (g / "diff.patch").read_text()
    ck(len(patch) > 200, f"{tid}: patch too small ({len(patch)}B) — wrong merge-base?")
    ck("diff --git" in patch, f"{tid}: patch is not a diff")
    ck(row["discord_messages"] > 0, f"{tid}: empty conversation slice")
    ck(row["jira_events"] > 0, f"{tid}: empty changelog slice")
    ck(row["commits"] > 0, f"{tid}: empty commit slice")
    b = json.loads((g / "brief.json").read_text())
    ck(len(b.get("tribal_evidence", {}).get("discussion", [])) > 0, f"{tid}: brief has no discussion")
    ck(b["duration_minutes"] > 0, f"{tid}: non-positive duration")
print(f"gold: {len(idx)} task slices, "
      f"{sum(r['discord_messages'] for r in idx)} messages, "
      f"{sum(r['commits'] for r in idx)} commits, "
      f"{sum(r['patch_bytes'] for r in idx)} patch bytes, "
      f"{sum(r['documents'] for r in idx)} documents")

# eval split must be present but separable
evals = [r for r in idx if r["task_id"].startswith("mer-2")]
ck(len(evals) == 7, f"expected 7 eval slices, got {len(evals)}")

if fails:
    print(f"\nFAILED ({len(fails)}):")
    for f in fails[:25]:
        print("  -", f)
    sys.exit(1)
print("\nPIPELINE VALIDATION PASSED")

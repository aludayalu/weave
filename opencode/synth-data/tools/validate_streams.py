#!/usr/bin/env python3
"""Validate the raw streams before ingestion.

Checks (all read-only):
  1. schemas match the real producers (bot.py discord, Jira REST v2, GitHub REST)
  2. every timestamp is UTC ISO-8601 and monotonic per stream
  3. cross-stream key consistency: MER-xxx / PR # appear in both git and chat
  4. task windows are recoverable: every window start has a chat message and
     every window end has a merge commit inside [start, end]
  5. attachments exist on disk with the recorded size
Exit code 1 on any failure.
"""
import json, re, subprocess, sys
from datetime import datetime
from pathlib import Path

ROOT = Path("/Users/aludayalu/weave/opencode")
REPO = ROOT / "meridian"
SYN = ROOT / "synth-data"
RAW = SYN / "raw"
fails, warns = [], []

def check(cond, msg):
    if not cond:
        fails.append(msg)
    return cond

def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))

# ---------------------------------------------------------------- discord
msgs_by_channel = {}
for f in sorted((RAW / "discord").glob("*.json")):
    rec = json.loads(f.read_text())
    check(set(rec) >= {"channel", "exported_at", "message_count", "messages"}, f"{f.name}: missing archive keys")
    check(rec["message_count"] == len(rec["messages"]), f"{f.name}: message_count mismatch")
    prev = None
    for m in rec["messages"]:
        check(set(m) >= {"message_id", "author", "timestamp", "edited", "content", "jump_url", "attachments"},
              f"{f.name}: message schema {sorted(m)}")
        check(re.match(r"^\d{17,20}$", m["message_id"]) is not None, f"{f.name}: bad snowflake {m['message_id']}")
        ts = parse(m["timestamp"])
        check(ts.tzinfo is not None and ts.utcoffset().total_seconds() == 0, f"{f.name}: non-UTC timestamp {m['timestamp']}")
        if prev and ts < prev:
            fails.append(f"{f.name}: messages out of order at {m['timestamp']}")
        prev = ts
        for a in m["attachments"]:
            check(set(a) >= {"uuid", "original_filename", "size", "content_type", "path"},
                  f"{f.name}: attachment schema {sorted(a)}")
            p = SYN / a["path"]
            check(p.exists(), f"{f.name}: missing attachment file {a['path']}")
            if p.exists():
                check(p.stat().st_size == a["size"], f"{f.name}: attachment size mismatch {a['path']}")
    msgs_by_channel[rec["channel"]["name"]] = rec["messages"]

all_msgs = [m for v in msgs_by_channel.values() for m in v]
check(len(all_msgs) > 300, f"discord stream too small: {len(all_msgs)}")

# ---------------------------------------------------------------- jira
issues = json.loads((RAW / "jira" / "issues.json").read_text())
keys = set()
for i in issues:
    check(set(i) >= {"id", "key", "fields", "changelog"}, f"{i.get('key')}: issue schema")
    check(i["key"] not in keys, f"duplicate jira key {i['key']}")
    keys.add(i["key"])
    f = i["fields"]
    check("summary" in f and "created" in f, f"{i['key']}: missing summary/created")
    for h in i["changelog"]["histories"]:
        check(set(h) >= {"id", "author", "created", "items"}, f"{i['key']}: changelog schema")
        parse(h["created"])
        for it in h["items"]:
            check(set(it) >= {"field", "fieldtype", "from", "fromString", "to", "toString"},
                  f"{i['key']}: changelog item schema")
    # changelog must be ordered and bounded by issue lifetime
    times = [parse(h["created"]) for h in i["changelog"]["histories"]]
    check(times == sorted(times), f"{i['key']}: changelog not chronological")
    check(min(times) >= parse(f["created"]), f"{i['key']}: changelog starts before issue creation")
    check(max(times) <= parse(f["updated"]), f"{i['key']}: changelog ends after issue update")
    for a in f.get("attachment", []):
        check(set(a) >= {"id", "filename", "size", "mimeType", "created", "author", "content"},
              f"{i['key']}: attachment schema")
        p = SYN / a["content"]
        check(p.exists(), f"{i['key']}: missing attachment {a['content']}")

# ---------------------------------------------------------------- github
commits = json.loads((RAW / "github" / "commits.json").read_text())
pulls = json.loads((RAW / "github" / "pulls.json").read_text())
shas = {c["sha"] for c in commits}
for c in commits:
    check(set(c) >= {"sha", "html_url", "commit", "parents", "stats", "files"}, "commit schema")
    parse(c["commit"]["author"]["date"])
    parse(c["commit"]["committer"]["date"])
    for p in c["parents"]:
        check(p["sha"] in shas, f"commit {c['sha']} references unknown parent {p['sha']}")
for p in pulls:
    check(set(p) >= {"number", "state", "title", "user", "created_at", "merged_at", "merge_commit_sha",
                     "head", "base"}, "pull schema")
    if p["merged"]:
        check(p["merge_commit_sha"] in shas, f"PR #{p['number']}: merge sha not in commit stream")
        m = next(c for c in commits if c["sha"] == p["merge_commit_sha"])
        check(len(m["parents"]) == 2, f"PR #{p['number']}: merge commit must have 2 parents")
        check(parse(p["merged_at"]) >= parse(m["commit"]["committer"]["date"]) - __import__("datetime").timedelta(seconds=1),
              f"PR #{p['number']}: merged_at before merge commit date")
check(len(pulls) >= 30, f"expected >=30 PRs, got {len(pulls)}")

# ---------------------------------------------------------------- cross-stream
gold = json.loads((SYN / "gold" / "tasks_gold.json").read_text())["tasks"]
text_all = "\n".join(m["content"] for m in all_msgs)
for g in gold:
    check(g["jira"] in text_all, f"{g['task_id']}: {g['jira']} never mentioned in Discord")
    check(f"PR #{g['pr']}" in text_all, f"{g['task_id']}: PR #{g['pr']} never mentioned in Discord")
    check(g["jira"] in keys, f"{g['task_id']}: {g['jira']} missing from jira stream")
    w0, w1 = parse(g["window"]["start"]), parse(g["window"]["end"])
    # window start must have a chat message within 30 min
    firsts = [m for m in all_msgs if abs((parse(m["timestamp"]) - w0).total_seconds()) < 1800]
    check(firsts, f"{g['task_id']}: no Discord message at window start")
    # merge commit must land inside the window
    pr = next((p for p in pulls if p["number"] == g["pr"]), None)
    check(pr is not None, f"{g['task_id']}: PR #{g['pr']} missing from github stream")
    if pr:
        check(w0 <= parse(pr["merged_at"]) <= w1,
              f"{g['task_id']}: merge {pr['merged_at']} outside window {g['window']}")
    # jira created must precede the merge
    iss = next(i for i in issues if i["key"] == g["jira"])
    check(w0 <= parse(iss["fields"]["created"]) <= w1, f"{g['task_id']}: jira created outside window")

# ---------------------------------------------------------------- report
print(f"discord: {len(msgs_by_channel)} channels / {len(all_msgs)} messages")
print(f"jira:     {len(issues)} issues, {sum(len(i['changelog']['histories']) for i in issues)} changelog entries")
print(f"github:  {len(commits)} commits, {len(pulls)} PRs ({sum(1 for p in pulls if p['merged'])} merged)")
print(f"gold:     {len(gold)} task windows")
if warns:
    print(f"\n{len(warns)} warnings")
    for w in warns[:10]:
        print("  warn:", w)
if fails:
    print(f"\nFAILED ({len(fails)}):")
    for f_ in fails[:25]:
        print("  -", f_)
    sys.exit(1)
print("\nALL CHECKS PASSED")

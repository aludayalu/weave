#!/usr/bin/env python3
"""Inject bronze-realistic mess into the raw streams.

A real 24h ingest of Discord/Jira/GitHub is never clean. Silver exists to fix
what this script simulates:

  * malformed messages: empty content, control characters, HTML leftovers,
    partially-serialized JSON fragments pasted into chat
  * bad attachments: missing size, wrong content type, zero-byte files,
    records pointing at files that were never archived
  * duplicates: Discord webhook retries and Jira re-saves produce exact dupes
  * timestamp drift: local offsets, naive datetimes, unix seconds, one bad future
  * tombstoned / edited markers and deleted-message ghosts
  * unrelated tickets and abandoned PRs that are not tasks at all
Idempotent: always regenerates from the clean generator output first.
"""
import json, random, shutil, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SYN = Path("/Users/aludayalu/weave/opencode/synth-data")
RAW = SYN / "raw"
random.seed(23)

JUNK_MESSAGES = [
    "",
    "   ",
    "<@301002> can you?",
    "```\n{\"partial\": tru",
    "\x00\x07 garbage from the export",
    "ok\r\n\r\n-- \nsent from phone",
    "&amp;&lt;legacy&gt;",
    "https://discord.com/channels/9901/1101/0 (deleted message placeholder)",
]
BAD_TYPES = ["", "application/octet-stream", "text/html; charset=utf-8", None]

def regenerate():
    for tool in ("gen_streams.py", "gen_discord_jira.py", "gen_github.py"):
        subprocess.run([sys.executable, f"tools/{tool}"], cwd=SYN, check=True,
                       capture_output=True)

def corrupt_attachment(msg):
    """Break some attachment records: bad size, bad type, missing file, zero byte."""
    a = msg.get("attachments") or []
    if not a:
        return
    if random.random() < 0.35:
        which = random.choice(a)
        mode = random.choice(["size", "type", "missing", "zero"])
        if mode == "size":
            which["size"] = which.get("size", 0) + random.randint(1, 4000)
        elif mode == "type":
            which["content_type"] = random.choice(BAD_TYPES)
        elif mode == "missing":
            which["path"] = "raw/attachments/jira/logs/" + Path(which["path"]).name
        elif mode == "zero":
            which["size"] = 0

def shift_timestamp(m):
    """Some exports keep offsets, some unix seconds, a few are naive/未来."""
    r = random.random()
    dt = datetime.fromisoformat(m["timestamp"])
    if r < 0.05:
        m["timestamp"] = str(int(dt.timestamp()))                       # unix seconds
    elif r < 0.09:
        m["timestamp"] = dt.astimezone(timezone(timedelta(hours=-6))).isoformat()  # offset
    elif r < 0.11:
        m["timestamp"] = dt.replace(tzinfo=None).isoformat()            # naive
    elif r < 0.115:
        m["timestamp"] = (dt + timedelta(days=400)).isoformat()         # clock skew

def main():
    regenerate()
    stats = {}

    # ---------------- discord
    for f in sorted((RAW / "discord").glob("*.json")):
        rec = json.loads(f.read_text())
        msgs = rec["messages"]
        orig = len(msgs)

        # duplicate webhook retries
        dups = random.sample(msgs, k=max(1, len(msgs) // 25))
        for d in dups:
            copy = dict(d)
            copy["attachments"] = [dict(a) for a in d["attachments"]]
            msgs.append(copy)

        # malformed content
        for m in random.sample(msgs, k=max(1, len(msgs) // 12)):
            m["content"] = random.choice(JUNK_MESSAGES)

        # edited + tombstone markers
        for m in random.sample(msgs, k=max(1, len(msgs) // 15)):
            if random.random() < 0.5:
                m["edited"] = m["timestamp"]
            else:
                m["author"]["name"] = "Deleted User"

        for m in msgs:
            shift_timestamp(m)
            corrupt_attachment(m)

        # a couple of ghost messages from deleted authors with no author id
        for _ in range(2):
            base = random.choice(msgs)
            msgs.append({
                "message_id": str(random.randint(10**17, 10**18)),
                "author": {"id": None, "name": None},
                "timestamp": base["timestamp"],
                "edited": None,
                "content": "",
                "jump_url": None,
                "attachments": [],
            })

        msgs.sort(key=lambda m: (m["timestamp"] or ""))
        rec["message_count"] = len(msgs)          # exporter's own count is wrong too
        f.write_text(json.dumps(rec, indent=2, ensure_ascii=False))
        stats[f.name] = {"before": orig, "after": len(msgs)}

    # ---------------- jira
    issues = json.loads((RAW / "jira" / "issues.json").read_text())
    for iss in random.sample(issues, k=6):
        # a re-save duplicate with a later updated timestamp
        dup = json.loads(json.dumps(iss))
        dup["key"] = iss["key"]
        dup["fields"]["updated"] = (datetime.fromisoformat(iss["fields"]["updated"])
                                    + timedelta(minutes=3)).isoformat()
        issues.append(dup)
    for iss in issues:
        if random.random() < 0.25:
            iss["changelog"]["histories"] = iss["changelog"]["histories"] + [
                {"id": "0", "author": {}, "created": "", "items": []}]  # partial write
        for h in iss["changelog"]["histories"]:
            if random.random() < 0.12:
                h["created"] = h["created"].replace("+00:00", "")           # naive
        for a in iss["fields"].get("attachment", []):
            if random.random() < 0.3:
                a["size"] = a.get("size", 0) + random.randint(1, 900)
            if random.random() < 0.2:
                a.pop("mimeType", None)
    # abandoned / non-task tickets
    issues.append({
        "id": "29999", "key": "MER-998",
        "fields": {"summary": "Ops: rotate read replica credentials",
                   "description": "unrelated maintenance ticket",
                   "issuetype": {"name": "Task"}, "status": {"name": "Done"},
                   "priority": {"name": "Low"},
                   "reporter": {"displayName": "Dan Levin"}, "assignee": None,
                   "created": "2026-09-15T13:00:00+00:00",
                   "updated": "2026-09-15T13:20:00+00:00",
                   "labels": ["chore"], "attachment": []},
        "changelog": {"startAt": 0, "maxResults": 2, "total": 2, "histories": [
            {"id": "39901", "author": {"displayName": "Dan Levin"},
             "created": "2026-09-15T13:00:00+00:00",
             "items": [{"field": "status", "fieldtype": "jira", "from": "10000",
                        "fromString": "To Do", "to": "10003", "toString": "Done"}]},
            {"id": "39902", "author": {"displayName": "Dan Levin"},
             "created": "2026-09-15T13:20:00+00:00",
             "items": [{"field": "resolution", "fieldtype": "jira", "from": None,
                        "fromString": None, "to": "10000", "toString": "Fixed"}]}]}})
    (RAW / "jira" / "issues.json").write_text(json.dumps(issues, indent=2, ensure_ascii=False))

    # ---------------- github
    pulls = json.loads((RAW / "github" / "pulls.json").read_text())
    commits = json.loads((RAW / "github" / "commits.json").read_text())
    # an abandoned PR that was never merged and is not a task
    pulls.append({
        "number": 199, "state": "open", "title": "chore: bump go toolchain to 1.28",
        "user": {"login": "dan-levin"}, "created_at": "2026-10-09T12:00:00+00:00",
        "updated_at": "2026-10-09T12:00:00+00:00", "merged_at": None, "closed_at": None,
        "merge_commit_sha": None, "merged": False,
        "head": {"ref": "chore/toolchain", "sha": "deadbeef" * 5, "repo": {"full_name": "meridian/meridian-erp"}},
        "base": {"ref": "main", "repo": {"full_name": "meridian/meridian-erp"}},
        "html_url": "https://github.com/meridian/meridian-erp/pull/199",
        "body": "", "commits": 1, "changed_files": 1})
    # a commit whose author metadata is missing (partial CI export)
    broken = json.loads(json.dumps(commits[5]))
    broken["sha"] = "0" * 40
    broken["commit"]["author"] = {}
    broken["parents"] = [{"sha": "f" * 40}]
    commits.append(broken)
    (RAW / "github" / "pulls.json").write_text(json.dumps(pulls, indent=2))
    (RAW / "github" / "commits.json").write_text(json.dumps(commits, indent=2))

    stats["jira_issues"] = len(issues)
    stats["github_pulls"] = len(pulls)
    stats["github_commits"] = len(commits)
    print(json.dumps(stats, indent=2))

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Discord + Jira stream generation (imports document index from gen_streams)."""

import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import SYN, REPO  # noqa: E402
import json, re, subprocess, textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAW = SYN / "raw"
DISCORD_EPOCH = 1420070400000  # ms
random_seed = 11

PERSONAS = {
    "priya":  ("Priya Sharma",  "priya-sharma",  "priya@meridian-erp.dev"),
    "marcus": ("Marcus Lee",    "marcus-lee",    "marcus@meridian-erp.dev"),
    "sofia":  ("Sofia Almeida", "sofia-almeida", "sofia@meridian-erp.dev"),
    "dan":    ("Dan Levin",     "dan-levin",     "dan@meridian-erp.dev"),
}
AUTHOR_IDS = {"priya": "301001", "marcus": "301002", "sofia": "301003", "dan": "301004"}
CHANNELS = {
    "eng-billing":   ("billing",      "1101"),
    "eng-platform":  ("eng-platform", "1102"),
    "eng-frontend":  ("eng-frontend", "1103"),
    "incidents":     ("incidents",    "1104"),
    "deploys":       ("deploys",      "1105"),
    "general":       ("general",      "1106"),
}
GUILD_ID = "9901"
GUILD = "meridian-systems"

def git(*a):
    return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()

def iso(dt): return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

def snowflake(dt, n):
    """Discord snowflake: (unix_ms << 22) | sequence — 18-19 digits."""
    ms = int(dt.timestamp() * 1000) - DISCORD_EPOCH
    return str((ms << 22) | (n % 4194304))

def att_dir(stream, name):
    kind = "reports" if name.endswith(".pdf") else "logs" if name.endswith(".log") else "images"
    return SYN / "raw" / "attachments" / stream / kind / "%s" 

def att_path(stream, tid, name):
    kind = "reports" if name.endswith(".pdf") else "logs" if name.endswith(".log") else "images"
    return SYN / "raw" / "attachments" / stream / kind / name

def attach(tid, kind, name):
    p = SYN / "raw" / "attachments" / kind / tid / name
    return p

def attachments_for(tid, docs_index, stream):
    out = []
    for name in docs_index.get(tid, []):
        for st in ("discord", "jira"):
            p = att_path(st, tid, name)
            if p.exists():
                ctype = "application/pdf" if name.endswith(".pdf") else ("image/png" if name.endswith(".png") else "text/plain")
                item = {"name": name, "size": p.stat().st_size,
                        "content_type": ctype, "path": str(p.relative_to(SYN))}
                if stream == "discord":
                    item = {"uuid": f"{name.split('-')[0]}-{abs(hash(name)) % 10**8}",
                            "original_filename": name, "size": item["size"],
                            "content_type": item["content_type"], "path": item["path"]}
                out.append(item)
                break
    return out

OPENERS = [
    "picking this up now",
    "took first pass at this, thoughts below",
    "reproduced it locally, it's real",
    "ok this is the third report on this, starting now",
]
MIDDLES = [
    "the interesting part is that the obvious fix passes locally and still ships broken",
    "grepped the whole repo and the constraint isn't where I'd expect",
    "checked the pinned messages in this channel, the convention explains it",
    "the generated artifacts are the source of truth, not the file you're editing",
    "cross-team contract involved here, so I opened a ticket before touching anything",
    "reverted my first attempt, it broke the partner integration",
]
CLOSERS = [
    "done, PR is up and CI is green",
    "shipped — flag is live in staging, will confirm after the nightly job",
    "fix is merged, thanks for the quick turnaround",
    "closing this out, follow-up filed for the remaining edge case",
]

def main():
    catalog = json.loads((SYN / "task_catalog.json").read_text())
    tasks = {t["id"]: t for t in catalog["tasks"]}
    docs_index = json.loads((SYN / "gold" / "documents_index.json").read_text())

    merges = {}
    for line in git("log", "--merges", "--format=%H|%cI|%s", "main").splitlines():
        sha, date, subject = line.split("|", 2)
        m = re.match(r"Merge pull request #(\d+) from (\S+)", subject)
        if m:
            merges[int(m.group(1))] = {"sha": sha, "date": date, "branch": m.group(2).replace("meridian/", "")}

    import random
    random.seed(random_seed)

    discord = {}   # channel -> [messages]
    jira = {}      # key -> issue
    gold = []

    for tid, t in tasks.items():
        pr = merges.get(t["pr"])
        if pr is None:
            continue
        merge_dt = datetime.fromisoformat(pr["date"])
        start = merge_dt - timedelta(days=2, hours=6, minutes=12)
        red_dt = start + timedelta(days=1, hours=1, minutes=40)
        impl_dt = start + timedelta(days=1, hours=6, minutes=5)
        end = merge_dt + timedelta(minutes=18)

        owner = t["channel"]
        lead = random.choice(list(AUTHOR_IDS))
        others = [p for p in AUTHOR_IDS if p != lead]

        msgs = []
        def add(when, handle, content, atts=None, thread=None):
            n = len(msgs)
            m = {
                "message_id": snowflake(when, n),
                "author": {"id": AUTHOR_IDS[handle], "name": PERSONAS[handle][0]},
                "timestamp": iso(when),
                "edited": None,
                "content": content,
                "jump_url": f"https://discord.com/channels/{GUILD_ID}/{CHANNELS[owner][1]}/{snowflake(when, n)}",
                "attachments": atts or [],
            }
            if thread:
                m["thread"] = thread
            msgs.append(m)

        # --- opener
        add(start, lead, f"@{others[0]} {t['problem']} Opening {t['jira']} to track it. {random.choice(OPENERS)}.")
        add(start + timedelta(minutes=9), others[0],
            f"ack. looking now. filing evidence on the ticket ({t['docs'][0]}).")
        atts = attachments_for(tid, docs_index, "discord")
        add(start + timedelta(hours=1, minutes=30), others[1],
            f"fyi {t['docs'][0]} is attached to {t['jira']} if you want the raw capture.", atts)
        # --- red tests land
        add(red_dt, lead, f"spec-first per team rule — tests for {t['jira']} are red on main now, pushing as I go.")
        add(impl_dt, others[0], random.choice(MIDDLES))
        add(impl_dt + timedelta(minutes=50), others[1], random.choice(MIDDLES))
        add(impl_dt + timedelta(hours=1, minutes=20), lead,
            f"PR #{t['pr']} is up: `{pr['branch']}`. review when you can.")
        # --- review chatter
        add(impl_dt + timedelta(hours=2, minutes=5), others[0],
            f"reviewed #{t['pr']} — left two comments, nothing structural.")
        add(impl_dt + timedelta(hours=2, minutes=40), lead, "addressed both, pushing.")
        # --- completion anchors
        add(end - timedelta(minutes=22), others[1], f"deploy is green, {t['jira']} is ready to close.")
        add(end, lead, f"{t['jira']} closed — PR #{t['pr']} merged. {random.choice(CLOSERS)}")

        # noise chatter around the window
        noise = [
            "standup in 5, billing standup moved to 10:30",
            "anyone seen the staging pg bump? unrelated",
            "rotating the read replica creds tonight, no action needed",
            "the coffee machine is fixed (somehow)",
            "reminder: freeze starts friday for the payments repo",
        ]
        for i in range(3):
            when = start + timedelta(hours=3 + i * 5)
            add(when, random.choice(list(AUTHOR_IDS)), random.choice(noise))

        msgs.sort(key=lambda m: m["timestamp"])
        discord.setdefault(owner, []).extend(msgs)

        jira[t["jira"]] = {
            "id": str(20000 + t["pr"]),
            "key": t["jira"],
            "fields": {
                "summary": t["title"],
                "description": t["problem"] + "\n\nTribal constraint: " + t["tribal"],
                "issuetype": {"name": "Bug"},
                "status": {"name": "Done"},
                "priority": {"name": "High"},
                "reporter": {"displayName": PERSONAS[lead][0]},
                "assignee": {"displayName": PERSONAS[lead][0]},
                "created": iso(start),
                "updated": iso(end),
                "labels": ["dataset-generated", t["channel"]],
                "resolution": {"name": "Fixed"},
                "attachment": [{"id": str(21000 + t["pr"]), "filename": n,
                                "size": att_path("jira", tid, n).stat().st_size,
                                "mimeType": "application/pdf" if n.endswith(".pdf") else ("image/png" if n.endswith(".png") else "text/plain"),
                                "created": iso(start + timedelta(minutes=40)),
                                "author": {"displayName": PERSONAS[lead][0]},
                                "content": str(att_path("jira", tid, n).relative_to(SYN))}
                               for n in docs_index.get(tid, []) if att_path("jira", tid, n).exists()],
            },
            "changelog": {
                "startAt": 0, "maxResults": 4, "total": 4,
                "histories": [
                    {"id": str(30000 + t["pr"]), "author": {"displayName": PERSONAS[lead][0]},
                     "created": iso(start),
                     "items": [{"field": "status", "fieldtype": "jira", "from": "10000", "fromString": "To Do", "to": "10001", "toString": "In Progress"},
                               {"field": "assignee", "fieldtype": "jira", "from": None, "fromString": None, "to": str(21000 + t["pr"]), "toString": PERSONAS[lead][0]}]},
                    {"id": str(30010 + t["pr"]), "author": {"displayName": PERSONAS[lead][0]},
                     "created": iso(red_dt),
                     "items": [{"field": "labels", "fieldtype": "jira", "from": None, "fromString": None, "to": None, "toString": "spec-first"}]},
                    {"id": str(30020 + t["pr"]), "author": {"displayName": PERSONAS[others[0]][0]},
                     "created": iso(impl_dt + timedelta(hours=2, minutes=5)),
                     "items": [{"field": "status", "fieldtype": "jira", "from": "10001", "fromString": "In Progress", "to": "10002", "toString": "In Review"},
                               {"field": "Link", "fieldtype": "jira", "from": None, "fromString": None, "to": None, "toString": f"PR #{t['pr']}"}]},
                    {"id": str(30030 + t["pr"]), "author": {"displayName": PERSONAS[lead][0]},
                     "created": iso(end),
                     "items": [{"field": "status", "fieldtype": "jira", "from": "10002", "fromString": "In Review", "to": "10003", "toString": "Done"},
                               {"field": "resolution", "fieldtype": "jira", "from": None, "fromString": None, "to": "10000", "toString": "Fixed"}]},
                ],
            },
        }

        gold.append({
            "task_id": tid, "jira": t["jira"], "pr": t["pr"], "branch": pr["branch"],
            "split": "eval" if tid.startswith("e") else "train",
            "channel": t["channel"],
            "window": {"start": iso(start), "end": iso(end)},
            "evidence": {
                "discord_first_message": msgs[0]["message_id"],
                "discord_last_message": msgs[-1]["message_id"],
                "jira_key": t["jira"],
                "github_merge_sha": pr["sha"],
            },
            "commits": [c for c in git("log", "--format=%H|%an|%aI|%s", f"{pr['sha']}^1..{pr['sha']}^2").splitlines()],
            "tribal_constraint": t["tribal"],
            "documents": docs_index.get(tid, []),
        })

    # ---- write discord channel files (bot.py archive schema)
    for ch, ml in discord.items():
        name, cid = CHANNELS[ch]
        ml.sort(key=lambda m: m["timestamp"])
        rec = {"channel": {"id": cid, "name": name, "type": "GUILD_TEXT"},
               "exported_at": iso(merge_dt), "message_count": len(ml), "messages": ml}
        (RAW / "discord").mkdir(parents=True, exist_ok=True)
        (RAW / "discord" / f"{name}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False))

    (RAW / "jira").mkdir(parents=True, exist_ok=True)
    (RAW / "jira" / "issues.json").write_text(json.dumps(list(jira.values()), indent=2, ensure_ascii=False))

    gold.sort(key=lambda g: g["window"]["start"])
    (SYN / "gold").mkdir(parents=True, exist_ok=True)
    (SYN / "gold" / "tasks_gold.json").write_text(json.dumps(
        {"repo": "meridian-erp", "generated_from": "git history", "tasks": gold}, indent=2, ensure_ascii=False))

    print(json.dumps({"channels": len(discord), "issues": len(jira), "gold": len(gold),
                      "discord_messages": sum(len(v) for v in discord.values())}))

if __name__ == "__main__":
    main()

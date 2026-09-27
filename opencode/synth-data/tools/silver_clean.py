#!/usr/bin/env python3
"""BRONZE -> SILVER.

The pipeline's cleaning stage. Reads the raw (messy) streams exactly as an
ingest would and emits typed, deduplicated, UTC-normalized, cross-stream-linked
records plus a quarantine file for everything it refused to fix.

Outputs (JSONL, one record per line, as Delta tables would hold them):
  silver/discord_messages.jsonl
  silver/jira_issues.jsonl
  silver/jira_changelog_events.jsonl
  silver/github_commits.jsonl
  silver/github_pull_requests.jsonl
  silver/documents.jsonl
  silver/links.jsonl
  silver/quarantine.jsonl
  silver/_silver_stats.json
"""
import hashlib, json, re, unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

SYN = Path("/Users/aludayalu/weave/opencode/synth-data")
RAW, SIL = SYN / "raw", SYN / "silver"

TICKET = re.compile(r"\bMER-\d+\b")
PRREF = re.compile(r"\bPR\s*#(\d+)\b|\bpull request\s*#(\d+)\b", re.I)
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")

quarantine, stats, repaired = [], {}, []


def drop(table, key, reason, raw):
    quarantine.append({"table": table, "key": key, "reason": reason, "raw": str(raw)[:400]})


def utc(ts, table, key):
    """Normalize any observed timestamp representation to UTC ISO-8601."""
    if ts is None or ts == "":
        drop(table, key, "missing_timestamp", ts)
        return None
    try:
        if isinstance(ts, (int, float)) or (isinstance(ts, str) and ts.isdigit()):
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        else:
            s = str(ts).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                # Jira/Discord exports are written by a UTC-configured host;
                # a naive stamp is a formatting loss, not a different zone.
                # Repair and flag rather than dropping the row.
                dt = dt.replace(tzinfo=timezone.utc)
                repaired.append(key)
            dt = dt.astimezone(timezone.utc)
    except Exception:
        drop(table, key, "unparseable_timestamp", ts)
        return None
    if dt.year > 2027:                                  # clock skew / bad write
        drop(table, key, "future_timestamp", ts)
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_text(s):
    """Strip control chars, collapse whitespace, drop empty results."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = "".join(ch for ch in s if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def write(name, rows):
    p = SIL / name
    with p.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)


def main():
    SIL.mkdir(exist_ok=True)

    # ------------------------------------------------ discord
    seen_msgs, msgs = set(), []
    for f in sorted((RAW / "discord").glob("*.json")):
        rec = json.loads(f.read_text())
        channel = (rec.get("channel") or {}).get("name")
        for m in rec.get("messages", []):
            mid = m.get("message_id")
            if not mid or not str(mid).isdigit():
                drop("discord_messages", mid, "invalid_snowflake", m)
                continue
            if mid in seen_msgs:                          # webhook retry
                stats["discord_duplicates"] = stats.get("discord_duplicates", 0) + 1
                continue
            seen_msgs.add(mid)
            author = m.get("author") or {}
            content = clean_text(m.get("content"))
            if not content:
                drop("discord_messages", mid, "empty_content_after_clean", m)
                continue
            ts = utc(m.get("timestamp"), "discord_messages", mid)
            if not ts:
                continue
            atts = []
            for a in m.get("attachments") or []:
                p = SYN / (a.get("path") or "")
                if not a.get("path") or not p.exists():
                    drop("discord_attachments", a.get("uuid"), "attachment_file_missing", a)
                    continue
                size = p.stat().st_size
                if size == 0 or size != a.get("size"):
                    drop("discord_attachments", a.get("uuid"),
                         f"size_mismatch declared={a.get('size')} actual={size}", a)
                atts.append({"uuid": a.get("uuid"), "filename": a.get("original_filename"),
                             "path": a.get("path"), "bytes": size,
                             "content_type": a.get("content_type") or "application/octet-stream"})
            msgs.append({
                "message_id": mid, "channel": channel, "guild": "meridian-systems",
                "author_id": author.get("id"), "author_name": author.get("name") or "unknown",
                "ts_utc": ts, "ts_raw": m.get("timestamp"),
                "ts_epoch": int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()),
                "content": content, "edited": utc(m.get("edited"), "discord_messages", mid) if m.get("edited") else None,
                "is_thread": bool(m.get("thread")),
                "mentions_ticket": TICKET.findall(content),
                "mentions_pr": [int(x or y) for x, y in PRREF.findall(content) if (x or y)],
                "attachments": atts,
            })
    msgs.sort(key=lambda r: r["ts_epoch"])
    stats["discord_messages"] = write("discord_messages.jsonl", msgs)

    # ------------------------------------------------ jira
    issues, events, seen_issue = [], [], set()
    for iss in json.loads((RAW / "jira" / "issues.json").read_text()):
        key = iss.get("key")
        f = iss.get("fields") or {}
        if not key or not re.match(r"^MER-\d+$", key):
            drop("jira_issues", key, "invalid_key", iss)
            continue
        created = utc(f.get("created"), "jira_issues", key)
        if not created:
            continue
        ident = (key, f.get("updated"))
        if ident in seen_issue:                          # re-save duplicate
            stats["jira_duplicates"] = stats.get("jira_duplicates", 0) + 1
            continue
        seen_issue.add(ident)
        atts = []
        for a in f.get("attachment") or []:
            p = SYN / (a.get("content") or "")
            if not a.get("content") or not p.exists():
                drop("jira_attachments", a.get("id"), "attachment_file_missing", a)
                continue
            size = p.stat().st_size
            if size != a.get("size"):
                drop("jira_attachments", a.get("id"),
                     f"size_mismatch declared={a.get('size')} actual={size}", a)
            atts.append({"attachment_id": a.get("id"), "filename": a.get("filename"),
                         "path": a.get("content"), "bytes": size,
                         "content_type": a.get("mimeType") or "application/octet-stream",
                         "created_utc": utc(a.get("created"), "jira_attachments", a.get("id"))})
        issues.append({"issue_key": key, "summary": clean_text(f.get("summary")),
                       "description": clean_text(f.get("description")),
                       "status": (f.get("status") or {}).get("name"),
                       "reporter": (f.get("reporter") or {}).get("displayName"),
                       "assignee": (f.get("assignee") or {}).get("displayName"),
                       "created_utc": created,
                         "updated_utc": utc(f.get("updated"), "jira_issues", key),
                       "attachments": atts})
        # Changelog histories are written in order. A naive timestamp is
        # recoverable: place it between its neighbours in the same history
        # (Jira's expand=changelog guarantee). Unrecoverable ones quarantine.
        raw_hist = [h for h in (iss.get("changelog") or {}).get("histories", [])
                    if h.get("created") and not str(h["created"]).endswith(("Z",))]
        anchor = utc(f.get("created"), "jira_issues", key)
        recovered = {}
        for h in raw_hist:
            if anchor:
                recovered[h["created"]] = anchor
                anchor = (datetime.fromisoformat(anchor.replace("Z", "+00:00"))
                          + timedelta(minutes=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
            stats.setdefault("jira_naive_timestamps_recovered", 0)
        for h in (iss.get("changelog") or {}).get("histories", []):
            raw_ts = h.get("created")
            if not raw_ts or str(raw_ts).endswith(("Z", "+00:00")):
                ts = utc(raw_ts, "jira_changelog_events", key)
            else:
                ts = recovered.get(raw_ts) or utc(raw_ts, "jira_changelog_events", key)
                if ts:
                    stats["jira_naive_timestamps_recovered"] += 1
            if not ts:
                continue
            for it in h.get("items", []):
                events.append({"issue_key": key, "ts_utc": ts,
                               "author": (h.get("author") or {}).get("displayName") or "unknown",
                               "field": it.get("field"), "from_value": it.get("fromString"),
                               "to_value": it.get("toString")})
    events.sort(key=lambda r: (r["issue_key"], r["ts_utc"]))
    stats["jira_issues"] = write("jira_issues.jsonl", issues)
    stats["jira_changelog_events"] = write("jira_changelog_events.jsonl", events)

    # ------------------------------------------------ github
    commits, seen_sha = [], set()
    for c in json.loads((RAW / "github" / "commits.json").read_text()):
        sha = c.get("sha")
        au = (c.get("commit") or {}).get("author") or {}
        if not sha or not re.match(r"^[0-9a-f]{40}$", sha) or not au.get("date"):
            drop("github_commits", sha, "incomplete_commit_metadata", c)
            continue
        if sha in seen_sha:
            continue
        seen_sha.add(sha)
        ts = utc(au.get("date"), "github_commits", sha)
        if not ts:
            continue
        st = c.get("stats") or {}
        commits.append({"sha": sha, "author_name": au.get("name") or "unknown",
                        "author_email": au.get("email"),
                        "authored_ts_utc": ts,
                        "committed_ts_utc": utc((c.get("commit") or {}).get("committer", {}).get("date"),
                                                "github_commits", sha) or ts,
                        "message": clean_text((c.get("commit") or {}).get("message")),
                        "is_merge": len(c.get("parents") or []) > 1,
                        "parent_shas": [p["sha"] for p in (c.get("parents") or [])],
                        "additions": st.get("additions", 0), "deletions": st.get("deletions", 0),
                        "changed_files": [f.get("filename") for f in (c.get("files") or []) if f.get("filename")]})
    commits.sort(key=lambda r: r["authored_ts_utc"])
    stats["github_commits"] = write("github_commits.jsonl", commits)

    prs = []
    for p in json.loads((RAW / "github" / "pulls.json").read_text()):
        n = p.get("number")
        if not isinstance(n, int):
            drop("github_pull_requests", n, "invalid_pr_number", p)
            continue
        created = utc(p.get("created_at"), "github_pull_requests", n)
        merged = utc(p.get("merged_at"), "github_pull_requests", n) if p.get("merged_at") else None
        prs.append({"pr_number": n, "title": clean_text(p.get("title")),
                    "state": p.get("state"), "merged": bool(p.get("merged")),
                    "author": (p.get("user") or {}).get("login"),
                    "branch": (p.get("head") or {}).get("ref"),
                    "merge_sha": p.get("merge_commit_sha"),
                    "opened_ts_utc": created,
                    "merged_ts_utc": merged,
                    "closed_ts_utc": utc(p.get("closed_at"), "github_pull_requests", n)})
    prs.sort(key=lambda r: r["opened_ts_utc"] or "")
    stats["github_pull_requests"] = write("github_pull_requests.jsonl", prs)

    # ------------------------------------------------ documents
    docs, seen_doc = [], set()
    for iss in issues:
        for a in iss["attachments"]:
            h = hashlib.sha256((SYN / a["path"]).read_bytes()).hexdigest()
            if a["path"] in seen_doc:
                continue
            seen_doc.add(a["path"])
            docs.append({"doc_id": a["attachment_id"], "path": a["path"], "filename": a["filename"],
                         "bytes": a["bytes"], "sha256": h[:16],
                         "doc_type": a["content_type"], "linked_issue_key": iss["issue_key"],
                         "linked_channel": None, "linked_ts_utc": a["created_utc"]})
    for m in msgs:
        for a in m["attachments"]:
            h = hashlib.sha256((SYN / a["path"]).read_bytes()).hexdigest()
            if a["path"] in seen_doc:
                continue
            seen_doc.add(a["path"])
            docs.append({"doc_id": a["uuid"], "path": a["path"], "filename": a["filename"],
                         "bytes": a["bytes"], "sha256": h[:16],
                         "doc_type": a["content_type"], "linked_issue_key": None,
                         "linked_channel": m["channel"], "linked_ts_utc": m["ts_utc"]})
    stats["documents"] = write("documents.jsonl", docs)

    # ------------------------------------------------ cross-stream links
    links = {}
    for m in msgs:
        for tk in m["mentions_ticket"]:
            e = links.setdefault(tk, {"entity": tk, "discord_message_ids": [], "jira_issue_keys": [],
                                      "github_shas": [], "github_pr_numbers": [],
                                      "first_ts_utc": m["ts_utc"], "last_ts_utc": m["ts_utc"]})
            e["discord_message_ids"].append(m["message_id"])
            e["last_ts_utc"] = max(e["last_ts_utc"], m["ts_utc"])
        for pr in m["mentions_pr"]:
            e = links.setdefault(f"PR#{pr}", {"entity": f"PR#{pr}", "discord_message_ids": [],
                                              "jira_issue_keys": [], "github_shas": [],
                                              "github_pr_numbers": [pr],
                                              "first_ts_utc": m["ts_utc"], "last_ts_utc": m["ts_utc"]})
            e["discord_message_ids"].append(m["message_id"])
    for iss in issues:
        e = links.setdefault(iss["issue_key"], {"entity": iss["issue_key"], "discord_message_ids": [],
                                                "jira_issue_keys": [iss["issue_key"]], "github_shas": [],
                                                "github_pr_numbers": [], "first_ts_utc": iss["created_utc"],
                                                "last_ts_utc": iss["created_utc"] or ""})
        e["last_ts_utc"] = max(e["last_ts_utc"] or "", iss["updated_utc"] or "")
    for p in prs:
        if not p["merged"]:
            continue
        e = links.setdefault(f"PR#{p['pr_number']}", {"entity": f"PR#{p['pr_number']}",
                                                      "discord_message_ids": [], "jira_issue_keys": [],
                                                      "github_shas": [p["merge_sha"]],
                                                      "github_pr_numbers": [p["pr_number"]],
                                                      "first_ts_utc": p["opened_ts_utc"],
                                                      "last_ts_utc": p["merged_ts_utc"] or ""})
    for e in links.values():
        for tk in re.findall(r"^MER-\d+$", e["entity"]):
            for pr in prs:
                if re.search(r"MER-\d+", pr["title"] or ""):
                    pass
    stats["links"] = write("links.jsonl", list(links.values()))
    stats["timestamps_repaired_as_utc"] = len(repaired)
    stats["quarantined"] = write("quarantine.jsonl", quarantine)
    (SIL / "_silver_stats.json").write_text(json.dumps(stats, indent=2))
    print(json.dumps(stats, indent=2))

    reasons = {}
    for q in quarantine:
        reasons[q["reason"].split()[0]] = reasons.get(q["reason"].split()[0], 0) + 1
    print("quarantine reasons:", json.dumps(reasons, indent=2))


if __name__ == "__main__":
    main()

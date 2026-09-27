#!/usr/bin/env python3
"""Load the pipeline output into Lakebase (Postgres).

    bronze/ + silver/ + detected/ + gold/  ->  Lakebase

Idempotent: every table is written with ON CONFLICT on its natural key, and
each run is recorded in ingest.ingest_batch. Re-running after adding a day of
chat updates the same rows instead of duplicating them.

Document bytes are NOT pushed into Postgres by default. The row keeps the
object URI, sha256 and byte count; pass --inline-docs to store small captures
(<1MB) as bytea instead.

Usage
    python3 load_lakebase.py --dry-run                     # verify mapping only
    python3 load_lakebase.py --dsn "$LAKEBASE_DSN"          # real load
    python3 load_lakebase.py --dsn ... --stages bronze,silver
"""
import argparse, hashlib, json, os, sys, uuid
from datetime import datetime, timezone
from pathlib import Path

SYN = Path("/Users/aludayalu/weave/opencode/synth-data")
PIPELINE_VERSION = "0.1.0"
# Stable namespace so the same input content always yields the same batch id.
UUID_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def utcnow():
    return datetime.now(timezone.utc)


def batch_id_for(stage, payload_digest):
    return uuid.uuid5(UUID_NS, f"{PIPELINE_VERSION}:{stage}:{payload_digest}")


def digest_dir(root, patterns):
    h = hashlib.sha256()
    files = []
    for pat in patterns:
        files += sorted(root.glob(pat))
    for f in sorted(set(files)):
        if f.is_file():
            h.update(str(f.relative_to(root)).encode())
            h.update(f.read_bytes())
    return h.hexdigest(), files


def jsonl(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def ts(v):
    """Parse any timestamp shape the exports produce into a UTC datetime."""
    if not v:
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        return datetime.fromtimestamp(int(v), tz=timezone.utc)
    s = str(v).replace("Z", "+00:00")
    d = datetime.fromisoformat(s)
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


# ------------------------------------------------------------------ plans
def plan_bronze():
    """Raw payloads, verbatim."""
    rows = {"bronze.discord_channel": [], "bronze.discord_message": [],
            "bronze.jira_issue": [], "bronze.jira_changelog_history": [],
            "bronze.github_commit": [], "bronze.github_commit_file": [],
            "bronze.github_pull_request": [], "bronze.github_event": [],
            "bronze.document": []}
    files = []

    for f in sorted((SYN / "raw" / "discord").glob("*.json")):
        rec = json.loads(f.read_text())
        bid = str(uuid.uuid5(UUID_NS, f"dc:{f.name}:{hashlib.sha256(f.read_bytes()).hexdigest()[:16]}"))
        ch = rec.get("channel") or {}
        files.append(("discord", f"raw/discord/{f.name}", f.stat().st_size, rec.get("message_count", 0)))
        rows["bronze.discord_channel"].append((bid, ch.get("id"), ch.get("name"), ch.get("type"),
                                        rec.get("exported_at"), rec.get("message_count"), json.dumps(rec)))
        for i, m in enumerate(rec.get("messages", [])):
            rows["bronze.discord_message"].append((str(m.get("message_id")), bid, i,
                                            json.dumps(m.get("author")), str(m.get("timestamp")),
                                            m.get("edited"), m.get("content"), m.get("jump_url"),
                                            json.dumps(m.get("attachments")), json.dumps(m)))

    for f in sorted((SYN / "raw" / "jira").glob("*.json")):
        for iss in json.loads(f.read_text()):
            bid = str(uuid.uuid5(UUID_NS, f"ji:{iss.get('key')}"))
            files.append(("jira", f"raw/jira/{f.name}", f.stat().st_size, 1))
            rows["bronze.jira_issue"].append((iss.get("key"), bid, str(iss.get("id")), json.dumps(iss)))
            for h in (iss.get("changelog") or {}).get("histories", []):
                rows["bronze.jira_changelog_history"].append(
                    (str(h.get("id")), iss.get("key"), bid, str(h.get("created")),
                     json.dumps(h.get("author")), json.dumps(h.get("items")), json.dumps(h)))

    for name, table in (("commits", "github_commit"), ("pulls", "github_pull_request"),
                        ("events", "github_event")):
        f = SYN / "raw" / "github" / f"{name}.json"
        if not f.exists():
            continue
        files.append(("github", f"raw/github/{f.name}", f.stat().st_size,
                      len(json.loads(f.read_text()))))
        bid = str(uuid.uuid5(UUID_NS, f"gh:{name}"))
        for rec in json.loads(f.read_text()):
            if table == "github_commit":
                au = (rec.get("commit") or {}).get("author") or {}
                cm = (rec.get("commit") or {}).get("committer") or {}
                rows["bronze.github_commit"].append((rec["sha"], bid, au.get("name"), au.get("email"),
                                              str(au.get("date")), str(cm.get("date")),
                                              (rec.get("commit") or {}).get("message"),
                                              [p["sha"] for p in rec.get("parents", [])],
                                              json.dumps(rec.get("stats")), json.dumps(rec)))
                for fl in rec.get("files", []):
                    rows["bronze.github_commit_file"].append((rec["sha"], fl.get("filename"),
                                                       fl.get("additions"), fl.get("deletions")))
            elif table == "github_pull_request":
                rows["bronze.github_pull_request"].append((rec["number"], bid, json.dumps(rec)))
            else:
                key = f"{rec.get('type')}|{rec.get('action')}|{rec.get('number')}|{rec.get('created_at')}"
                rows["bronze.github_event"].append((key, bid, rec.get("number"),
                                             str(rec.get("created_at")), json.dumps(rec)))

    for f in sorted((SYN / "raw" / "attachments").rglob("*")):
        if not f.is_file():
            continue
        blob = f.read_bytes()
        rel = str(f.relative_to(SYN))
        stream = rel.split("/")[2]
        rows["bronze.document"].append((hashlib.sha256(blob).hexdigest(), None, stream, f.name,
                                 rel, None, len(blob), hashlib.sha256(blob).hexdigest(),
                                 None, None, blob if os.environ.get("INLINE_DOCS") else None))
        files.append(("attachment", rel, len(blob), 1))
    return rows, files


def plan_silver():
    rows = {"silver.discord_message": [], "silver.jira_issue": [],
            "silver.jira_changelog_event": [], "silver.github_commit": [],
            "silver.github_pull_request": [], "silver.entity_link": [],
            "silver.quarantine": []}
    for r in jsonl(SYN / "silver" / "discord_messages.jsonl"):
        rows["silver.discord_message"].append((r["message_id"], r.get("channel"), r.get("guild"),
                                        r.get("author_id"), r["author_name"], ts(r["ts_utc"]),
                                        r.get("ts_raw"), ts(r["edited"]) if r.get("edited") else None,
                                        r["content"], r.get("is_thread", False),
                                        r.get("mentions_ticket", []), r.get("mentions_pr", []),
                                        [a["uuid"] for a in r.get("attachments", [])]))
    for r in jsonl(SYN / "silver" / "jira_issues.jsonl"):
        rows["silver.jira_issue"].append((r["issue_key"], r.get("summary"), r.get("description"),
                                   r.get("status"), r.get("reporter"), r.get("assignee"),
                                   ts(r["created_utc"]), ts(r["updated_utc"])))
    for i, r in enumerate(jsonl(SYN / "silver" / "jira_changelog_events.jsonl")):
        rows["silver.jira_changelog_event"].append((r["issue_key"], ts(r["ts_utc"]), r.get("author"),
                                             r.get("field"), r.get("from_value"), r.get("to_value")))
    for r in jsonl(SYN / "silver" / "github_commits.jsonl"):
        rows["silver.github_commit"].append((r["sha"], r["author_name"], r.get("author_email"),
                                      ts(r["authored_ts_utc"]), ts(r["committed_ts_utc"]),
                                      r.get("message"), r.get("is_merge", False),
                                      r.get("parent_shas", []), r.get("additions", 0),
                                      r.get("deletions", 0)))
    for r in jsonl(SYN / "silver" / "github_pull_requests.jsonl"):
        rows["silver.github_pull_request"].append((r["pr_number"], r.get("title"), r.get("state"),
                                            r.get("merged", False), r.get("author"), r.get("branch"),
                                            r.get("merge_sha"), ts(r["opened_ts_utc"]),
                                            ts(r["merged_ts_utc"]), ts(r["closed_ts_utc"])))
    for r in jsonl(SYN / "silver" / "links.jsonl"):
        rows["silver.entity_link"].append((r["entity"], "pull_request" if r["entity"].startswith("PR#") else "ticket",
                                    r.get("discord_message_ids", []), r.get("jira_issue_keys", []),
                                    r.get("github_shas", []), r.get("github_pr_numbers", []),
                                    ts(r.get("first_ts_utc")), ts(r.get("last_ts_utc"))))
    for r in jsonl(SYN / "silver" / "quarantine.jsonl"):
        rows["silver.quarantine"].append((r.get("table"), r.get("key"), r.get("reason"), r.get("raw")))
    return rows


def plan_detected():
    path = SYN / "detected" / "task_windows.json"
    if not path.exists():
        return []
    doc = json.loads(path.read_text())
    out = []
    for t in doc["tasks"]:
        ev = t.get("evidence", {})
        out.append((t["task_id"], doc.get("produced_by", "unknown"), None,
                    ts(t["start_ts_utc"]), ts(t["end_ts_utc"]), t.get("confidence"),
                    t.get("title"), t.get("summary"), t.get("participants", []),
                    json.dumps(ev)))
    return out


def plan_gold():
    idx_path = SYN / "gold" / "_index.json"
    if not idx_path.exists():
        return {}, []
    idx = json.loads(idx_path.read_text())["tasks"]
    tasks, msgs, evs, commits, docs, diffs = [], [], [], [], [], []
    ev_by_key = {}
    for r in jsonl(SYN / "silver" / "jira_changelog_events.jsonl"):
        k = (r["issue_key"], r["ts_utc"], r.get("field"))
        ev_by_key.setdefault(k, len(ev_by_key) + 1)   # mirror the serial order

    for row in idx:
        tid = row["task_id"]
        g = SYN / "gold" / tid
        brief = json.loads((g / "brief.json").read_text()) if (g / "brief.json").exists() else {}
        tasks.append((tid, row.get("jira"), row.get("pr"), row.get("split", "train"),
                      row.get("title"), row.get("channel"), ts(row["window"]["start"]),
                      ts(row["window"]["end"]), row.get("duration_minutes"),
                      None, row.get("tribal_constraint"),
                      json.dumps(brief), json.dumps({k: row.get(k) for k in
                                                     ("discord_messages", "jira_events", "commits",
                                                      "patch_bytes", "documents")})))
        if (g / "discord.json").exists():
            seg = json.loads((g / "discord.json").read_text())
            ids = [m["message_id"] for m in seg["messages"]]
            for i, mid in enumerate(ids):
                msgs.append((tid, mid, i, i == 0, i == len(ids) - 1))
        if (g / "jira.json").exists():
            seg = json.loads((g / "jira.json").read_text())
            key = row.get("jira")
            for e in seg.get("changelog_events", []):
                eid = ev_by_key.get((key, e["ts_utc"], e.get("field")))
                if eid:
                    evs.append((tid, eid))
        if (g / "git.json").exists():
            seg = json.loads((g / "git.json").read_text())
            for i, c in enumerate(seg.get("commits", [])):
                commits.append((tid, c["sha"], i))
            diffs.append((tid, seg.get("base_sha"), seg.get("head_sha"), seg.get("merge_sha"),
                          f"gold/{tid}/diff.patch",
                          hashlib.sha256((g / "diff.patch").read_bytes()).hexdigest()
                          if (g / "diff.patch").exists() else None,
                          (g / "diff.patch").stat().st_size if (g / "diff.patch").exists() else 0))
        for n in row.get("document_files", []):
            for stream in ("jira", "discord"):
                cand = SYN / "raw" / "attachments" / stream
                hit = next((c for c in cand.rglob(n)), None)
                if hit:
                    docs.append((tid, hashlib.sha256(hit.read_bytes()).hexdigest()))
                    break
    return {"gold.task": tasks, "gold.task_message": msgs, "gold.task_changelog_event": evs,
            "gold.task_commit": commits, "gold.task_document": docs,
            "gold.task_diff": diffs}, idx


# ------------------------------------------------------------------ SQL
STATEMENTS = {
    "bronze.discord_channel": """
        INSERT INTO bronze.discord_channel
          (bot_message_id, channel_id, channel_name, channel_type,
           exported_at, declared_count, raw)
        VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT (bot_message_id) DO UPDATE
          SET raw = EXCLUDED.raw, declared_count = EXCLUDED.declared_count""",
    "bronze.discord_message": """
        INSERT INTO bronze.discord_message
          (discord_message_id, bot_message_id, ordinal, author, timestamp_raw,
           edited_raw, content_raw, jump_url, attachments, raw)
        VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
        ON CONFLICT (discord_message_id) DO UPDATE
          SET content_raw = EXCLUDED.content_raw, attachments = EXCLUDED.attachments""",
    "bronze.jira_issue": """
        INSERT INTO bronze.jira_issue (issue_key, batch_id, jira_issue_id, raw)
        VALUES (%s,%s,%s,%s::jsonb)
        ON CONFLICT (issue_key) DO UPDATE SET raw = EXCLUDED.raw""",
    "bronze.jira_changelog_history": """
        INSERT INTO bronze.jira_changelog_history
          (history_id, issue_key, batch_id, created_raw, author, items, raw)
        VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)
        ON CONFLICT (history_id) DO UPDATE SET raw = EXCLUDED.raw""",
    "bronze.github_commit": """
        INSERT INTO bronze.github_commit
          (sha, batch_id, author_name, author_email, authored_raw, committed_raw,
           message, parents, stats, raw)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
        ON CONFLICT (sha) DO UPDATE SET raw = EXCLUDED.raw""",
    "bronze.github_commit_file": """
        INSERT INTO bronze.github_commit_file (sha, filename, additions, deletions)
        VALUES (%s,%s,%s,%s)
        ON CONFLICT (sha, filename) DO UPDATE
          SET additions = EXCLUDED.additions, deletions = EXCLUDED.deletions""",
    "bronze.github_pull_request": """
        INSERT INTO bronze.github_pull_request (pr_number, batch_id, raw)
        VALUES (%s,%s,%s::jsonb)
        ON CONFLICT (pr_number) DO UPDATE SET raw = EXCLUDED.raw""",
    "bronze.github_event": """
        INSERT INTO bronze.github_event (event_key, batch_id, pr_number, created_at, raw)
        VALUES (%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT (event_key) DO UPDATE SET raw = EXCLUDED.raw""",
    "bronze.document": """
        INSERT INTO bronze.document
          (document_id, batch_id, stream, filename, object_uri, mime_type,
           bytes, sha256, linked_issue_key, linked_channel, inline_bytes)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (document_id) DO UPDATE SET bytes = EXCLUDED.bytes""",
    "silver.discord_message": """
        INSERT INTO silver.discord_message
          (discord_message_id, batch_id, channel_name, guild_name, author_id, author_name,
           ts, ts_raw, edited_at, content, is_thread, mentions_ticket, mentions_pr, attachment_uuids)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (discord_message_id) DO UPDATE
          SET content = EXCLUDED.content, ts = EXCLUDED.ts""",
    "silver.jira_issue": """
        INSERT INTO silver.jira_issue
          (issue_key, batch_id, summary, description, status, reporter, assignee, created_at, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (issue_key) DO UPDATE
          SET summary = EXCLUDED.summary, updated_at = EXCLUDED.updated_at""",
    "silver.jira_changelog_event": """
        INSERT INTO silver.jira_changelog_event
          (batch_id, issue_key, ts, author, field, from_value, to_value)
        VALUES (%s,%s,%s,%s,%s,%s,%s)""",
    "silver.github_commit": """
        INSERT INTO silver.github_commit
          (sha, batch_id, author_name, author_email, authored_at, committed_at,
           message, is_merge, parent_shas, additions, deletions)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (sha) DO UPDATE SET message = EXCLUDED.message""",
    "silver.github_pull_request": """
        INSERT INTO silver.github_pull_request
          (pr_number, batch_id, title, state, merged, author, branch, merge_sha,
           opened_at, merged_at, closed_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (pr_number) DO UPDATE SET title = EXCLUDED.title""",
    "silver.entity_link": """
        INSERT INTO silver.entity_link
          (entity, kind, discord_message_ids, jira_issue_keys, github_shas,
           github_pr_numbers, first_ts, last_ts)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (entity) DO UPDATE
          SET discord_message_ids = EXCLUDED.discord_message_ids,
              github_shas = EXCLUDED.github_shas""",
    "silver.quarantine": """
        INSERT INTO silver.quarantine (batch_id, target_table, entity_key, reason, raw)
        VALUES (%s,%s,%s,%s,%s::jsonb)""",
    "detected.task_window": """
        INSERT INTO detected.task_window
          (task_id, batch_id, produced_by, model_version, start_ts, end_ts, confidence,
           title, summary, participants, evidence)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT (task_id) DO UPDATE
          SET start_ts = EXCLUDED.start_ts, end_ts = EXCLUDED.end_ts,
              evidence = EXCLUDED.evidence, produced_by = EXCLUDED.produced_by""",
    "gold.task": """
        INSERT INTO gold.task
          (task_id, batch_id, jira_issue_key, pr_number, split, title, channel,
           start_ts, end_ts, duration_minutes, confidence, tribal_constraint, brief, coverage)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
        ON CONFLICT (task_id) DO UPDATE
          SET brief = EXCLUDED.brief, coverage = EXCLUDED.coverage""",
    "gold.task_message": """
        INSERT INTO gold.task_message
          (task_id, discord_message_id, ordinal, is_start, is_end)
        VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (task_id, discord_message_id) DO NOTHING""",
    "gold.task_changelog_event": """
        INSERT INTO gold.task_changelog_event (task_id, event_id)
        VALUES (%s,%s) ON CONFLICT DO NOTHING""",
    "gold.task_commit": """
        INSERT INTO gold.task_commit (task_id, sha, ordinal)
        VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
    "gold.task_document": """
        INSERT INTO gold.task_document (task_id, document_id)
        VALUES (%s,%s) ON CONFLICT DO NOTHING""",
    "gold.task_diff": """
        INSERT INTO gold.task_diff
          (task_id, base_sha, head_sha, merge_sha, patch_object_uri, patch_sha256, patch_bytes)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (task_id) DO UPDATE
          SET patch_sha256 = EXCLUDED.patch_sha256, patch_bytes = EXCLUDED.patch_bytes""",
}


# Position of batch_id in each table's column list.
#   default (1)  = second column, right after the natural key
#   explicit 0  = first column
BATCH_AT = {"silver.quarantine": 0, "silver.jira_changelog_event": 0}

# Tables that take no batch_id, and why:
#   bronze.*            each row already carries the batch id of the file it
#                       came from (one file = one batch), so injecting again
#                       would duplicate it
#   child/join tables   a patch line, a task->message link or a commit file
#                       inherits provenance from its parent row; a column here
#                       would always agree with the parent and never disagree
#                       usefully
NO_BATCH = {
    "bronze.discord_channel", "bronze.discord_message", "bronze.jira_issue",
    "bronze.jira_changelog_history", "bronze.github_commit",
    "bronze.github_commit_file", "bronze.github_pull_request",
    "bronze.github_event", "bronze.document",
    "gold.task_message", "gold.task_changelog_event", "gold.task_commit",
    "gold.task_document", "gold.task_diff",
    "silver.entity_link",
}


def with_batch(table, row, bid):
    """Insert batch_id at the table's declared position, if it has one."""
    if table in NO_BATCH:
        return tuple(row)
    pos = BATCH_AT.get(table, 1)
    return tuple(row[:pos]) + (bid,) + tuple(row[pos:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("LAKEBASE_DSN"),
                    help="Postgres DSN. Omit (or use --dry-run) to only verify the mapping.")
    ap.add_argument("--stages", default="bronze,silver,detected,gold")
    ap.add_argument("--inline-docs", action="store_true",
                    help="store document bytes as bytea instead of object pointers")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    if args.inline_docs:
        os.environ["INLINE_DOCS"] = "1"

    plans, files, digests = {}, [], {}
    for stage in stages:
        if stage == "bronze":
            rows, f = plan_bronze()
            plans.update(rows)
            files += f
            digests["bronze"] = digest_dir(SYN / "raw", ["discord/*.json", "jira/*.json",
                                                          "github/*.json", "attachments/**/*"])[0]
        elif stage == "silver":
            plans.update(plan_silver())
            digests["silver"] = digest_dir(SYN / "silver", ["*.jsonl"])[0]
        elif stage == "detected":
            plans["detected.task_window"] = plan_detected()
            digests["detected"] = digest_dir(SYN / "detected", ["*.json"])[0]
        elif stage == "gold":
            g, idx = plan_gold()
            plans.update(g)
            digests["gold"] = digest_dir(SYN / "gold", ["_index.json", "*/brief.json"])[0]

    total = sum(len(v) for v in plans.values())
    print(json.dumps({"pipeline_version": PIPELINE_VERSION,
                      "stages": stages,
                      "tables": {k: len(v) for k, v in sorted(plans.items())},
                      "total_rows": total,
                      "source_files": len(files),
                      "documents_inlined": bool(args.inline_docs),
                      "mode": "dry-run" if (args.dry_run or not args.dsn) else "load"},
                     indent=2))

    if args.dry_run or not args.dsn:
        problems = []
        for table, rows in plans.items():
            if not rows:
                continue
            if table not in STATEMENTS:
                problems.append(f"{table}: no upsert statement")
                continue
            want = STATEMENTS[table].count("%s")
            got = len(with_batch(table, rows[0], "00000000-0000-0000-0000-000000000000"))
            if want != got:
                problems.append(f"{table}: statement wants {want} values, row supplies {got}")
        if problems:
            print("\nDRY RUN FAILED:", file=sys.stderr)
            for pr in problems:
                print("  -", pr, file=sys.stderr)
            return 1
        print(f"\ndry run OK — {len(plans)} tables mapped, "
              f"column arity matches every statement, batch_id positioned correctly")
        return 0

    try:
        import psycopg
    except ImportError:
        print("psycopg is required for a real load: pip install 'psycopg[binary]'", file=sys.stderr)
        return 2

    conn = psycopg.connect(args.dsn, autocommit=False)
    with conn, conn.cursor() as cur:
        for stage in stages:
            bid = str(batch_id_for(stage, digests.get(stage, "")))
            cur.execute("""INSERT INTO ingest.ingest_batch
                             (batch_id, pipeline_version, stage, status, source_root)
                           VALUES (%s,%s,%s,'running',%s)
                           ON CONFLICT (batch_id) DO UPDATE
                             SET started_at = now(), status = 'running'""",
                        (bid, PIPELINE_VERSION, stage, str(SYN)))
            for table, rows in plans.items():
                if not rows or not table.startswith(f"{stage}."):
                    continue
                stmt = STATEMENTS[table]
                # every table carries batch_id; inject it as the 2nd column
                for r in rows:
                    cur.execute(stmt, with_batch(table, r, bid))
            cur.execute("""UPDATE ingest.ingest_batch
                              SET finished_at = now(), status = 'success'
                            WHERE batch_id = %s""", (bid,))

            for stream, uri, size, rows in (files if stage == "bronze" else []):
                sfid = str(uuid.uuid5(UUID_NS, f"sf:{stream}:{uri}"))
                cur.execute("""INSERT INTO ingest.source_file
                                 (source_file_id, batch_id, stream, uri, sha256, bytes, row_count)
                               VALUES (%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (stream, uri, sha256) DO UPDATE
                                 SET landed_at = now()""",
                            (sfid, bid, stream, uri, hashlib.sha256(uri.encode()).hexdigest(), size, rows))

            # advance the delta watermark per stream
            if stage == "silver":
                cur.execute("""INSERT INTO ingest.watermark (stream, last_ts, last_key)
                               VALUES ('discord', (SELECT max(ts) FROM silver.discord_message), NULL)
                               ON CONFLICT (stream) DO UPDATE
                                 SET last_ts = EXCLUDED.last_ts, updated_at = now()""")
                cur.execute("""INSERT INTO ingest.watermark (stream, last_ts)
                               VALUES ('github', (SELECT max(authored_at) FROM silver.github_commit))
                               ON CONFLICT (stream) DO UPDATE
                                 SET last_ts = EXCLUDED.last_ts, updated_at = now()""")

        if "gold" in stages:
            cur.execute("""INSERT INTO ops.dataset_version
                             (dataset_version, task_counts, root_uri)
                           SELECT %s,
                                  (SELECT jsonb_build_object(
                                     'train', count(*) FILTER (WHERE split='train'),
                                     'eval',  count(*) FILTER (WHERE split='eval'))
                                    FROM gold.task),
                                  %s
                           ON CONFLICT (dataset_version) DO UPDATE
                             SET task_counts = EXCLUDED.task_counts""",
                        (PIPELINE_VERSION, str(SYN)))

        cur.execute("""SELECT table_schema || '.' || table_name
                         FROM information_schema.tables
                        WHERE table_schema IN ('bronze','silver','detected','gold','ingest')
                          AND table_type = 'BASE TABLE'
                        ORDER BY 1""")
        print("\nloaded. tables now present:", len(cur.fetchall()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

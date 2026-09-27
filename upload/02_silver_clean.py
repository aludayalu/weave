"""Notebook 02: clean bronze into silver.

Reads the raw bronze tables written by notebook 01 and produces tidy silver
tables: dedupe, drop the junk, repair the timestamps, quarantine anything that
cannot be trusted, and link messages to tickets and pull requests.

Re-running is safe: the silver tables are truncated and rebuilt each time.
"""

# %% [markdown]
# ## 2. Clean bronze into silver
#
# Reads `bronze.*` and writes `silver.*`. Anything unusable is not silently
# dropped: it lands in `silver.quarantine` with the reason.

# %%

import json
import re
from datetime import datetime, timezone

try:
    import psycopg
    from psycopg.types.json import Json
except ImportError:
    # The driver is not on the cluster yet, so install it and carry on. The
    # binary wheel bundles libpq, so it imports straight away with no restart.
    import subprocess
    import sys

    print("installing psycopg...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "psycopg[binary]"])
    import psycopg
    from psycopg.types.json import Json
    print("psycopg ready")

import dbutils

dbutils.widgets.text("bronze_db", "bronze", "bronze schema written by notebook 01")
dbutils.widgets.text("silver_db", "silver", "silver schema written by this notebook")
dbutils.widgets.text("strict", "0", "1 = quarantine naive timestamps instead of repairing")

bronze_db = dbutils.widgets.get("bronze_db")
silver_db = dbutils.widgets.get("silver_db")
strict = dbutils.widgets.get("strict") == "1"

SECRET_SCOPE = "tribal"
SECRET_KEY = "lakebase"
LAKEBASE_DSN = "postgresql://my_app_role:Nn6jLu98ceTBqsBTT69i-xYo@dbc-5a40bfd7-421c.cloud.databricks.com:5432/databricks_postgres?sslmode=require"

# %% connect

from databricks.sdk import WorkspaceClient


def secret_dsn():
    """Read the connection string from Databricks secrets, if reachable."""
    try:
        workspace = WorkspaceClient()
        secret = workspace.secrets.get_secret(SECRET_SCOPE, SECRET_KEY)
        value = getattr(secret, "value", None) or secret.value_or_raise()
        return value.strip()
    except Exception:
        return None


def store_dsn(value):
    """Save it once so future runs need nothing pasted."""
    try:
        workspace = WorkspaceClient()
        try:
            workspace.secrets.create_scope(SECRET_SCOPE, scope_backend_type="DATABRICKS")
        except Exception:
            pass
        workspace.secrets.put_secret(SECRET_SCOPE, SECRET_KEY, value)
        print(f"saved to secrets/{SECRET_SCOPE}/{SECRET_KEY} — nothing to paste from now on")
    except Exception as error:
        print(f"note: could not write secrets ({type(error).__name__}); using the DSN below")


def resolve_dsn():
    stored = secret_dsn()
    if stored:
        print(f"using secrets/{SECRET_SCOPE}/{SECRET_KEY}")
        return stored
    if LAKEBASE_DSN.strip():
        store_dsn(LAKEBASE_DSN.strip())
        return LAKEBASE_DSN.strip()
    raise SystemExit("No Lakebase connection. Put the connection string in LAKEBASE_DSN and run once.")


dsn = resolve_dsn()
conn = psycopg.connect(dsn, autocommit=False, connect_timeout=60)
cur = conn.cursor()
cur.execute("select current_database(), current_user, version()")
database, user, version = cur.fetchone()
print(f"connected: db={database} user={user}")

# %% cleaning rules

filtered = {}
quarantined = []
stamped_at = datetime.now(timezone.utc)


def count(rule):
    filtered[rule] = filtered.get(rule, 0) + 1


def quarantine(table, key, reason, raw=None):
    """Record a row we refused to keep, with the reason."""
    if raw is None:
        payload = None
    elif isinstance(raw, str):
        payload = raw[:2000] or None
    else:
        payload = json.dumps(raw)[:2000]
    quarantined.append((table, str(key)[:400], reason, payload, stamped_at))


def clean_text(value):
    """Tidy a text field: drop control characters, entities and stray spaces."""
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"&amp;|&lt;|&gt;", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_timestamp(value):
    """Return (iso timestamp, note), or (None, reason) if unusable.

    Unix seconds and ISO stamps are both accepted. A naive stamp came from a
    host whose UTC offset was lost, so it is read as UTC and flagged as repaired
    rather than shifted into some other zone.
    """
    if value is None or value == "":
        return None, "missing"
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return datetime.fromtimestamp(int(value), timezone.utc).isoformat(), "unix_seconds"
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            if strict:
                return None, "naive"
            return parsed.replace(tzinfo=timezone.utc).isoformat(), "repaired_naive"
        return parsed.astimezone(timezone.utc).isoformat(), "offset"
    except (ValueError, OverflowError, OSError):
        return None, "unparseable"


def is_tombstone(message):
    """A deleted-message ghost: no author, no link, no text."""
    author = message.get("author") or {}
    return (not author.get("id") and message.get("jump_url") is None
            and not (message.get("content") or "").strip())


# %% silver schema

SILVER_SQL = f"""
CREATE SCHEMA IF NOT EXISTS {silver_db};

CREATE TABLE IF NOT EXISTS {silver_db}.discord_message (
  message_id text PRIMARY KEY, channel text, author_id text, author_name text,
  ts timestamptz NOT NULL, ts_raw text, timestamp_repaired boolean DEFAULT false,
  content text NOT NULL, mentions_ticket text[] DEFAULT '{{}}',
  mentions_pr int[] DEFAULT '{{}}', attachment_filenames text[] DEFAULT '{{}}');

CREATE TABLE IF NOT EXISTS {silver_db}.jira_issue (
  issue_key text PRIMARY KEY, summary text, description text, status text,
  reporter text, assignee text, created_at timestamptz, updated_at timestamptz);

CREATE TABLE IF NOT EXISTS {silver_db}.jira_changelog_event (
  id bigserial PRIMARY KEY, issue_key text, ts timestamptz, timestamp_repaired boolean,
  author text, field text, from_value text, to_value text);

CREATE TABLE IF NOT EXISTS {silver_db}.github_commit (
  sha text PRIMARY KEY, author_name text, author_email text, authored_at timestamptz,
  message text, is_merge boolean DEFAULT false, parent_shas text[] DEFAULT '{{}}',
  additions int DEFAULT 0, deletions int DEFAULT 0, changed_files text[] DEFAULT '{{}}');

CREATE TABLE IF NOT EXISTS {silver_db}.github_pull_request (
  pr_number int PRIMARY KEY, title text, state text, merged boolean DEFAULT false,
  author text, branch text, merge_sha text, opened_at timestamptz, merged_at timestamptz);

CREATE TABLE IF NOT EXISTS {silver_db}.document (
  filename text PRIMARY KEY, stream text, object_path text, bytes bigint,
  sha256 text, size_ok boolean DEFAULT false);

CREATE TABLE IF NOT EXISTS {silver_db}.entity_link (
  entity text PRIMARY KEY, kind text, message_ids text[] DEFAULT '{{}}',
  pr_numbers int[] DEFAULT '{{}}', first_ts timestamptz, last_ts timestamptz);

CREATE TABLE IF NOT EXISTS {silver_db}.quarantine (
  id bigserial PRIMARY KEY, target_table text, entity_key text, reason text,
  raw jsonb, quarantined_at timestamptz DEFAULT now());
"""

SILVER_TABLES = [
    "discord_message", "jira_issue", "jira_changelog_event", "github_commit",
    "github_pull_request", "document", "entity_link", "quarantine",
]

cur.execute(SILVER_SQL)
conn.commit()
for table in SILVER_TABLES:
    cur.execute(f"TRUNCATE {silver_db}.{table} RESTART IDENTITY CASCADE")
conn.commit()
print(f"silver schema ready in {silver_db}")

# %% discord
#
# Drop the ghosts, the duplicates and the empty messages, and pull the ticket
# and pull-request references out of the text so they can be linked later.

channels = dict(cur.execute(
    f"SELECT file_id, channel_name FROM {bronze_db}.discord_channel").fetchall())

seen_ids = set()
messages = []
for message_id, file_id, raw_message in cur.execute(
        f"SELECT message_id, file_id, raw FROM {bronze_db}.discord_message").fetchall():
    message = raw_message if isinstance(raw_message, dict) else {}
    key = str(message.get("message_id") or message_id or "")

    if not key.isdigit():
        count("invalid_snowflake")
        quarantine("discord_message", key, "invalid_snowflake", message)
        continue
    if key in seen_ids:
        count("duplicate_message")
        continue
    seen_ids.add(key)

    if is_tombstone(message):
        count("tombstone")
        quarantine("discord_message", key, "tombstone_ghost", message)
        continue

    body = clean_text(message.get("content"))
    if not body:
        count("empty_content")
        quarantine("discord_message", key, "empty_content_after_clean", message)
        continue

    timestamp, note = parse_timestamp(message.get("timestamp"))
    if timestamp is None:
        count("unusable_timestamp")
        quarantine("discord_message", key, f"unusable_timestamp:{note}", message)
        continue
    if note == "repaired_naive":
        count("timestamp_repaired")

    author = message.get("author") or {}
    messages.append((
        key, channels.get(file_id), author.get("id"), author.get("name") or "unknown",
        timestamp, str(message.get("timestamp")), note == "repaired_naive", body,
        sorted(set(re.findall(r"\bMER-\d+\b", body))),
        sorted({int(number) for number in re.findall(r"\bPR\s*#(\d+)\b", body)}),
        [item.get("original_filename") for item in (message.get("attachments") or [])
         if item.get("original_filename")]))

if messages:
    cur.executemany(
        f"INSERT INTO {silver_db}.discord_message VALUES"
        " (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", messages)
conn.commit()
print(f"discord: {len(messages)} kept")

# %% jira
#
# A ticket re-saved from the UI arrives as a second bronze row for the same key
# with a later updated stamp, so dedupe on the key and keep the freshest copy.

freshest = {}
for issue_key, raw_issue in cur.execute(
        f"SELECT issue_key, raw FROM {bronze_db}.jira_issue").fetchall():
    issue = raw_issue if isinstance(raw_issue, dict) else {}
    if not re.match(r"^MER-\d+$", str(issue_key or "")):
        count("invalid_key")
        quarantine("jira_issue", issue_key, "invalid_key", issue)
        continue

    fields = issue.get("fields") or {}
    created, _note = parse_timestamp(fields.get("created"))
    if created is None:
        count("unusable_created")
        quarantine("jira_issue", issue_key, "unusable_created", issue)
        continue
    updated, _note = parse_timestamp(fields.get("updated"))

    row = (issue_key, clean_text(fields.get("summary")), clean_text(fields.get("description")),
           (fields.get("status") or {}).get("name"),
           (fields.get("reporter") or {}).get("displayName"),
           (fields.get("assignee") or {}).get("displayName"), created, updated)
    if issue_key in freshest:
        count("duplicate_ticket")
        if (updated or "") > (freshest[issue_key][7] or ""):
            freshest[issue_key] = row
    else:
        freshest[issue_key] = row

if freshest:
    cur.executemany(
        f"INSERT INTO {silver_db}.jira_issue VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT DO NOTHING", list(freshest.values()))
conn.commit()
print(f"jira: {len(freshest)} kept")

# %% jira changelog
#
# One row per field change, flattened out of the nested changelog history.

events = []
next_event_id = 0
for issue_key, created_raw, author, items in cur.execute(
        f"SELECT issue_key, created_raw, author, items"
        f" FROM {bronze_db}.jira_changelog_history").fetchall():
    timestamp, note = parse_timestamp(created_raw)
    if timestamp is None:
        count("unusable_history_timestamp")
        quarantine("jira_changelog_event", issue_key, f"unusable_history_timestamp:{note}")
        continue
    if note == "repaired_naive":
        count("timestamp_repaired")
    for item in items or []:
        next_event_id += 1
        events.append((next_event_id, issue_key, timestamp, note == "repaired_naive",
                       (author or {}).get("displayName") or "unknown", item.get("field"),
                       item.get("fromString"), item.get("toString")))

if events:
    cur.executemany(
        f"INSERT INTO {silver_db}.jira_changelog_event"
        " (id, issue_key, ts, timestamp_repaired, author, field, from_value, to_value)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", events)
conn.commit()
print(f"jira changelog: {len(events)} kept")

# %% git

commits = []
for sha, author, authored_raw, message, parents, stats, files in cur.execute(
        f"SELECT sha, author, authored_raw, message, parents, stats, files"
        f" FROM {bronze_db}.github_commit").fetchall():
    if not re.match(r"^[0-9a-f]{40}$", str(sha or "")):
        count("malformed_sha")
        quarantine("github_commit", sha, "malformed_sha")
        continue

    authored_at, note = parse_timestamp(authored_raw)
    if authored_at is None:
        count("unusable_commit_timestamp")
        quarantine("github_commit", sha, f"unusable_commit_timestamp:{note}")
        continue
    if not (author or {}).get("name"):
        count("missing_commit_author")
        quarantine("github_commit", sha, "missing_commit_author")
        continue

    parent_shas = [str(parent) for parent in (parents or [])]
    commits.append((
        sha, clean_text((author or {}).get("name")), (author or {}).get("email"),
        authored_at, clean_text(message), len(parent_shas) > 1, parent_shas,
        int((stats or {}).get("additions") or 0), int((stats or {}).get("deletions") or 0),
        sorted({str(item.get("filename")) for item in (files or [])
                if isinstance(item, dict) and item.get("filename")})))

if commits:
    cur.executemany(
        f"INSERT INTO {silver_db}.github_commit VALUES"
        " (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", commits)
conn.commit()
print(f"git: {len(commits)} commits kept")

pulls = []
for number, raw_pull in cur.execute(
        f"SELECT pr_number, raw FROM {bronze_db}.github_pull_request").fetchall():
    pull = raw_pull if isinstance(raw_pull, dict) else {}
    user = pull.get("user") or {}
    opened_at, _note = parse_timestamp(pull.get("created_at"))
    merged_at, _note = parse_timestamp(pull.get("merged_at"))
    merged = bool(pull.get("merged_at") or pull.get("merged"))
    if merged and not merged_at:
        count("merged_without_timestamp")
        quarantine("github_pull_request", number, "merged_without_timestamp", pull)
        continue
    head = (pull.get("head") or {})
    pulls.append((
        number, clean_text(pull.get("title")), (pull.get("state") or "").lower() or None,
        merged, user.get("login") or "unknown", head.get("ref"),
        (pull.get("merge_commit_sha") or None), opened_at, merged_at))

if pulls:
    cur.executemany(
        f"INSERT INTO {silver_db}.github_pull_request VALUES"
        " (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", pulls)
conn.commit()
print(f"git: {len(pulls)} pull requests kept")

# %% documents
#
# Check each attachment actually landed in the workspace and is the size we
# recorded, so a truncated copy is caught here rather than by a reader later.

documents = []
for filename, stream, object_path, size, sha256 in cur.execute(
        f"SELECT filename, stream, object_path, bytes, sha256"
        f" FROM {bronze_db}.document").fetchall():
    size_ok = False
    try:
        size_ok = dbutils.fs.head(object_path).size == size
    except Exception:
        size_ok = False
    if not size_ok:
        count("attachment_missing_or_wrong_size")
        quarantine("document", filename, "attachment_missing_or_wrong_size")
    documents.append((filename, stream, object_path, size, sha256, size_ok))

if documents:
    cur.executemany(
        f"INSERT INTO {silver_db}.document VALUES (%s, %s, %s, %s, %s, %s)"
        " ON CONFLICT DO NOTHING", documents)
conn.commit()
print(f"documents: {sum(1 for row in documents if row[5])} verified")

# %% entity links
#
# Which messages talk about which ticket or pull request, with the span of time
# each entity was discussed over.

links = {}
for message_id, ts, tickets, pull_numbers in cur.execute(
        f"SELECT message_id, ts, mentions_ticket, mentions_pr"
        f" FROM {silver_db}.discord_message").fetchall():
    for ticket in tickets or []:
        entry = links.setdefault(ticket, {"kind": "ticket", "messages": set(), "prs": set(),
                                          "first": ts, "last": ts})
        entry["messages"].add(message_id)
        entry["first"] = min(entry["first"], ts)
        entry["last"] = max(entry["last"], ts)
    for number in pull_numbers or []:
        entry = links.setdefault(f"PR#{number}", {"kind": "pull_request", "messages": set(),
                                                  "prs": {number}, "first": ts, "last": ts})
        entry["messages"].add(message_id)
        entry["first"] = min(entry["first"], ts)
        entry["last"] = max(entry["last"], ts)

if links:
    cur.executemany(
        f"INSERT INTO {silver_db}.entity_link VALUES (%s, %s, %s, %s, %s, %s)"
        " ON CONFLICT DO NOTHING",
        [(entity, entry["kind"], sorted(entry["messages"]), sorted(entry["prs"]),
          entry["first"], entry["last"]) for entity, entry in sorted(links.items())])
conn.commit()
print(f"links: {len(links)} entities")

# %% quarantine
#
# raw is a jsonb column, so text is wrapped rather than bound as a bare string.

if quarantined:
    cur.executemany(
        f"INSERT INTO {silver_db}.quarantine"
        " (target_table, entity_key, reason, raw, quarantined_at)"
        " VALUES (%s, %s, %s, %s, %s)",
        [(table, key, reason, Json(raw) if raw is not None else None, when)
         for table, key, reason, raw, when in quarantined])
conn.commit()

# %% summary

print("\nsilver:")
for table in SILVER_TABLES:
    cur.execute(f"SELECT count(*) FROM {silver_db}.{table}")
    print(f"  {table:<26} {cur.fetchone()[0]}")

print("\nfiltered out:")
for rule, number in sorted(filtered.items(), key=lambda item: -item[1]):
    print(f"  {rule:<34} {number}")
print(f"  {'quarantined':<34} {len(quarantined)}")

print(f"\nfinished {datetime.now(timezone.utc).isoformat()}")

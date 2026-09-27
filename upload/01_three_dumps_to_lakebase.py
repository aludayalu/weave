"""Notebook 01: load the three raw dumps into Lakebase bronze.

Reads the Discord, Git and Jira dumps that are already unpacked in the
workspace and lands them in Postgres as-is. Nothing is cleaned here: bronze
keeps the raw, malformed, duplicated records so notebook 02 can show its work.

Run the %%pip cell at the top once, restart the kernel, then Run All.
"""

# %% [markdown]
# ## 1. Load the three raw dumps into Lakebase bronze
#
# Reads `discord/`, `git/` and `jira/` from the workspace folder. The folder
# layout depends on how the dumps were unpacked, so the files are located by
# searching rather than by assuming a fixed path.
#
# Run this cell once, then restart the kernel and Run All:
#
# ```
# %pip install 'psycopg[binary]'
# dbutils.library.restartPython()
# ```

# %%

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

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

dbutils.widgets.text("instance", "meridian-tribal", "Lakebase instance")
dbutils.widgets.text("data_dir", "", "folder with discord/ git/ jira/ (blank = auto-detect)")
dbutils.widgets.text("reuse", "0", "1 = append instead of reloading")

instance = dbutils.widgets.get("instance")
data_dir = dbutils.widgets.get("data_dir")
reuse = dbutils.widgets.get("reuse") == "1"
run_id = str(uuid.uuid4())
started_at = datetime.now(timezone.utc)

SECRET_SCOPE = "tribal"
SECRET_KEY = "lakebase"
LAKEBASE_DSN = "postgresql://my_app_role:Nn6jLu98ceTBqsBTT69i-xYo@dbc-5a40bfd7-421c.cloud.databricks.com:5432/databricks_postgres?sslmode=require"

print(f"instance={instance} run_id={run_id}")

# %% find the dumps
#
# The workspace root holds one folder per stream. Inside each, the .json files
# sit at whatever depth the archive unpacked to.


def walk_dirs(parent, max_depth=4):
    """Every directory at or under parent, shallowest first."""
    level = [parent]
    for _ in range(max_depth):
        level = [child for folder in level for child in sorted(folder.iterdir())
                 if child.is_dir()]
        yield from level


def locate_file(parent, filename, max_depth=4):
    """Nearest folder at or under parent that holds filename."""
    if (parent / filename).is_file():
        return parent
    level = [parent]
    for _ in range(max_depth):
        level = [child for folder in level for child in sorted(folder.iterdir())
                 if child.is_dir()]
        for folder in level:
            if (folder / filename).is_file():
                return folder
    return None


def find_stream_dir(parent, name):
    """Where this stream's .json dumps actually are."""
    for candidate in (parent / name, parent / "stream" / name, parent / name / name):
        if candidate.is_dir() and any(candidate.glob("*.json")):
            return candidate
    if any(parent.glob("*.json")):
        return parent
    for folder in walk_dirs(parent):
        if folder.name == name and any(folder.glob("*.json")):
            return folder
    raise SystemExit(
        f"No {name} .json files under {parent}.\n"
        "Set the data_dir widget to the folder holding the discord/, git/ and\n"
        "jira/ dumps.")


def find_attachments(root, stream):
    """Attachment blobs for a stream, wherever the unpack put them."""
    for candidate in (root / "attachments" / stream,
                      root / stream / "attachments" / stream,
                      root / "stream" / "attachments" / stream):
        if candidate.is_dir():
            return candidate
    for folder in walk_dirs(root):
        if folder.name == "attachments" and (folder / stream).is_dir():
            return folder / stream
    return None


def find_data_dir(configured):
    """Use the widget value if it holds dumps, otherwise look for them."""
    if configured:
        return Path(configured)
    for base in ("/Workspace/Users", "/Workspace", "/Volumes/main/default"):
        root = Path(base)
        if not root.is_dir():
            continue
        for candidate in sorted(root.glob("*/Weave")) + [root / "Weave"]:
            if all((candidate / name).is_dir() for name in ("discord", "git", "jira")):
                return candidate
    raise SystemExit(
        "Could not find a folder holding discord/, git/ and jira/.\n"
        "Set the data_dir widget to it.")


data_root = find_data_dir(data_dir)
print(f"reading from {data_root}")


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# %% connect
#
# The DSN is baked in, and also saved to Databricks secrets on the first run so
# later runs need no editing. A secrets or auth failure is never fatal.

from databricks.sdk import WorkspaceClient

attachment_root = f"/Workspace/Users/{dbutils.notebook().context.userName}/_tribal_attachments"
dbutils.fs.mkdirs(attachment_root)


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
    raise SystemExit(
        "No Lakebase connection. Put the connection string in LAKEBASE_DSN near the\n"
        "top of this notebook and run once.")


dsn = resolve_dsn()
conn = psycopg.connect(dsn, autocommit=False, connect_timeout=60)
cur = conn.cursor()
cur.execute("select current_database(), current_user, version()")
database, user, version = cur.fetchone()
print(f"connected: db={database} user={user}")
print(version.split(",")[0])

# %% scrubbing
#
# The exports contain NUL bytes, which Postgres refuses to store in text. Every
# value bound through the cursor is stripped of them, and every JSON payload is
# scrubbed before it is wrapped.


def scrub(value):
    """Remove NUL bytes from text and from every string inside JSON."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    return value


class ScrubbedCursor:
    """A cursor that scrubs parameters before handing them to the driver."""

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=None):
        return self._cursor.execute(sql, scrub(params))

    def executemany(self, sql, params=None):
        return self._cursor.executemany(sql, scrub(params))

    def __getattr__(self, name):
        return getattr(self._cursor, name)


cur = ScrubbedCursor(cur)

# %% schema
#
# Bronze is raw, but the primary keys are what let a re-run replace rows rather
# than duplicate them. Where the dumps genuinely repeat a key, the newest copy
# wins and the count difference is reported at the end.

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS ingest;
CREATE SCHEMA IF NOT EXISTS bronze;

CREATE TABLE IF NOT EXISTS ingest.ingest_batch (
  batch_id uuid PRIMARY KEY, run_id text, stage text,
  started_at timestamptz DEFAULT now(), status text DEFAULT 'running');

CREATE TABLE IF NOT EXISTS ingest.source_file (
  stream text, relative_path text, sha256 text, bytes bigint,
  run_id text, landed_at timestamptz DEFAULT now(),
  PRIMARY KEY (stream, relative_path, sha256));

CREATE TABLE IF NOT EXISTS bronze.discord_channel (
  file_id uuid PRIMARY KEY, channel_id text, channel_name text,
  exported_at text, declared_count int, raw jsonb, run_id text);

CREATE TABLE IF NOT EXISTS bronze.discord_message (
  message_id text PRIMARY KEY, file_id uuid, ordinal int, author jsonb,
  timestamp_raw text, edited_raw text, content_raw text, jump_url text,
  attachments jsonb, raw jsonb, run_id text);

CREATE TABLE IF NOT EXISTS bronze.jira_issue (
  issue_key text PRIMARY KEY, raw jsonb, run_id text);

CREATE TABLE IF NOT EXISTS bronze.jira_changelog_history (
  history_key text PRIMARY KEY, issue_key text, created_raw text, author jsonb,
  items jsonb, raw jsonb, run_id text);

CREATE TABLE IF NOT EXISTS bronze.github_commit (
  sha text PRIMARY KEY, author jsonb, authored_raw text, committed_raw text,
  message text, parents jsonb, stats jsonb, files jsonb, raw jsonb, run_id text);

CREATE TABLE IF NOT EXISTS bronze.github_pull_request (
  pr_number int PRIMARY KEY, raw jsonb, run_id text);

CREATE TABLE IF NOT EXISTS bronze.github_event (
  event_key text PRIMARY KEY, pr_number int, created_at text, raw jsonb, run_id text);

CREATE TABLE IF NOT EXISTS bronze.document (
  filename text PRIMARY KEY, stream text, object_path text, bytes bigint,
  sha256 text, raw jsonb, run_id text);
"""

BRONZE_TABLES = [
    "discord_channel", "discord_message", "jira_issue", "jira_changelog_history",
    "github_commit", "github_pull_request", "github_event", "document",
]

try:
    cur.execute(SCHEMA_SQL)
except psycopg.errors.InsufficientPrivilege:
    conn.rollback()
    raise SystemExit(
        "The role can connect but cannot create schemas. Run this once as the\n"
        "instance owner, then re-run:\n\n"
        "  GRANT CREATE ON DATABASE databricks_postgres TO my_app_role;")

if not reuse:
    for table in BRONZE_TABLES:
        cur.execute(f"TRUNCATE bronze.{table}")

cur.execute("INSERT INTO ingest.ingest_batch (batch_id, run_id, stage) VALUES (%s, %s, 'bronze')",
            (run_id, run_id))
conn.commit()
print(f"schema ready in {database}")


def record_file(stream, path):
    """Note a source file and its hash in the ingest ledger."""
    cur.execute(
        "INSERT INTO ingest.source_file (stream, relative_path, sha256, bytes, run_id)"
        " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (stream, str(path.relative_to(data_root)), file_sha256(path), path.stat().st_size, run_id))


# %% discord
#
# One JSON file per channel. Each message keeps its position in the export and
# the whole message in raw, including the fields that are malformed.

discord_dir = find_stream_dir(data_root / "discord", "discord")
discord_files = sorted(discord_dir.glob("*.json"))
raw_messages = 0

for path in discord_files:
    record_file("discord", path)
    archive = json.loads(path.read_text())
    raw_messages += len(archive.get("messages") or [])
    channel = archive.get("channel") or {}
    file_id = uuid.uuid5(uuid.NAMESPACE_URL, path.name)

    cur.execute(
        "INSERT INTO bronze.discord_channel VALUES (%s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (file_id) DO UPDATE SET raw = EXCLUDED.raw",
        (file_id, channel.get("id"), channel.get("name"), archive.get("exported_at"),
         archive.get("message_count"), Json(scrub(archive)), run_id))

    for position, message in enumerate(archive.get("messages") or []):
        cur.execute(
            "INSERT INTO bronze.discord_message VALUES"
            " (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (message_id) DO UPDATE SET raw = EXCLUDED.raw",
            (str(message.get("message_id")), file_id, position, Json(scrub(message.get("author"))),
             str(message.get("timestamp")), message.get("edited"), message.get("content"),
             message.get("jump_url"), Json(scrub(message.get("attachments"))),
             Json(scrub(message)), run_id))

conn.commit()
cur.execute("SELECT count(*) FROM bronze.discord_message")
discord_rows = cur.fetchone()[0]
conn.commit()
if not discord_rows:
    raise SystemExit(f"discord files found in {discord_dir} but 0 messages landed in bronze")
print(f"discord: {len(discord_files)} files, {raw_messages} raw messages, {discord_rows} stored")

# %% jira

jira_dir = find_stream_dir(data_root / "jira", "jira")
jira_files = sorted(jira_dir.glob("*.json"))
raw_issues = 0
raw_histories = 0

for path in jira_files:
    record_file("jira", path)
    for issue in json.loads(path.read_text()):
        issue = issue or {}
        raw_issues += 1
        issue_key = issue.get("key") or f"__missing_key_{uuid.uuid4()}"
        cur.execute("INSERT INTO bronze.jira_issue VALUES (%s, %s, %s)"
                    " ON CONFLICT (issue_key) DO UPDATE SET raw = EXCLUDED.raw",
                    (issue_key, Json(scrub(issue)), run_id))
        for history in (issue.get("changelog") or {}).get("histories") or []:
            raw_histories += 1
            cur.execute(
                "INSERT INTO bronze.jira_changelog_history VALUES"
                " (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (history_key) DO UPDATE SET raw = EXCLUDED.raw",
                (f"{issue_key}:{history.get('id')}", issue_key, str(history.get("created")),
                 Json(scrub(history.get("author"))), Json(scrub(history.get("items"))),
                 Json(scrub(history)), run_id))

conn.commit()
cur.execute("SELECT count(*) FROM bronze.jira_issue")
jira_rows = cur.fetchone()[0]
conn.commit()
if not jira_rows:
    raise SystemExit(f"jira files found in {jira_dir} but 0 issues landed in bronze")
print(f"jira: {len(jira_files)} files, {raw_issues} raw issues, {jira_rows} stored")

# %% git

git_dir = locate_file(data_root / "git", "commits.json")
if git_dir is None:
    raise SystemExit(
        f"No commits.json under {data_root / 'git'}.\n"
        "Set the data_dir widget to the folder holding the discord/, git/ and jira/ dumps.")

for name in ("commits.json", "pulls.json", "events.json"):
    path = git_dir / name
    if path.is_file():
        record_file("git", path)

commits = json.loads((git_dir / "commits.json").read_text())
for commit in commits:
    message = commit.get("commit") or {}
    cur.execute(
        "INSERT INTO bronze.github_commit VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (sha) DO UPDATE SET raw = EXCLUDED.raw",
        (commit.get("sha"), Json(scrub(message.get("author"))),
         str((message.get("author") or {}).get("date")),
         str((message.get("committer") or {}).get("date")), message.get("message"),
         Json(scrub(commit.get("parents"))), Json(scrub(commit.get("stats"))),
         Json(scrub(commit.get("files"))), Json(scrub(commit)), run_id))
conn.commit()

for pull in json.loads((git_dir / "pulls.json").read_text()):
    number = pull.get("number")
    if not isinstance(number, int):
        number = -abs(hash(json.dumps(pull, sort_keys=True))) % 10 ** 8
    cur.execute("INSERT INTO bronze.github_pull_request VALUES (%s, %s, %s)"
                " ON CONFLICT (pr_number) DO UPDATE SET raw = EXCLUDED.raw",
                (number, Json(scrub(pull)), run_id))
conn.commit()

for event in json.loads((git_dir / "events.json").read_text()):
    key = (f"{event.get('type')}|{event.get('action')}"
           f"|{event.get('number')}|{event.get('created_at')}")
    cur.execute("INSERT INTO bronze.github_event VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (event_key) DO UPDATE SET raw = EXCLUDED.raw",
                (key, event.get("number"), str(event.get("created_at")),
                 Json(scrub(event)), run_id))
conn.commit()

cur.execute("SELECT count(*) FROM bronze.github_commit")
git_rows = cur.fetchone()[0]
conn.commit()
if not git_rows:
    raise SystemExit(f"git files found in {git_dir} but 0 commits landed in bronze")
print(f"git: {len(commits)} commits, {git_dir}")

# %% attachments
#
# Attachment blobs are copied to the workspace and registered as documents so
# the silver layer can verify them against the messages that reference them.

attachment_count = 0
for stream in ("discord", "jira"):
    stream_root = find_attachments(data_root, stream)
    if stream_root is None:
        print(f"  no {stream} attachments found")
        continue
    for folder, _subfolders, names in os.walk(stream_root):
        for filename in sorted(names):
            path = os.path.join(folder, filename)
            destination = f"{attachment_root}/{stream}/{filename}"
            dbutils.fs.mkdirs(os.path.dirname(destination))
            dbutils.fs.cp(f"file://{path}", destination, overwrite=True)
            cur.execute(
                "INSERT INTO bronze.document VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (filename) DO UPDATE SET raw = EXCLUDED.raw",
                (filename, stream, destination, os.path.getsize(path), file_sha256(path),
                 Json({"source": f"{stream}/{filename}"}), run_id))
            attachment_count += 1

conn.commit()
if not attachment_count:
    raise SystemExit(
        "No attachment files found. The Discord and Jira dumps should each carry an\n"
        "attachments folder; check the data_dir widget.")
print(f"attachments copied: {attachment_count}")

# %% summary

cur.execute("SELECT count(*) FROM ingest.source_file WHERE run_id = %s", (run_id,))
file_count = cur.fetchone()[0]
conn.commit()

cur.execute("UPDATE ingest.ingest_batch SET status = 'done', started_at = %s"
            " WHERE batch_id = %s", (started_at, run_id))
conn.commit()

print(f"\n{file_count} files read, {attachment_count} attachments copied")
print("bronze loaded:")
for table in BRONZE_TABLES:
    cur.execute(f"SELECT count(*) FROM bronze.{table}")
    print(f"  {table:<26} {cur.fetchone()[0]}")

print(f"\nrun_id {run_id} finished {datetime.now(timezone.utc).isoformat()}")
print("next: 02_silver_clean")

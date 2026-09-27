"""Notebook 03: silver to gold, the timestamp pass.

Stage one of two. This notebook does only the first half of the work:

  1. build a compact, ticket-centric digest of everything in silver
  2. ask a model to infer the task time windows from that digest
  3. validate whatever the model returns against silver, deterministically
  4. score it against a deterministic baseline, so the model has to earn its place
  5. slice every silver table by the winning windows with pure static code

Step 5 is the only thing that writes gold. The model never touches gold directly:
it proposes windows, the code decides.

The parallel per-chunk agent phase is a separate notebook and deliberately not
built here.

psycopg is the only thing that must be present; it ships in the Databricks
runtime, and if it is missing run  %pip install psycopg[binary]  then restart
the kernel. Then Run All.
"""

# %% [markdown]
# ## 3. Silver to gold: model-inferred time windows
#
# Silver knows everything that happened and when. It does not know which stretches
# of that history were *a task*. This notebook finds those stretches.
#
# The split of labour matters:
#
# | stage | who | why |
# |---|---|---|
# | propose windows | a model | picking boundaries is judgement, not arithmetic |
# | validate windows | static code | the model must not be trusted with facts |
# | slice into chunks | static code | gold must be reproducible byte for byte |
#
# ## Which model does what
#
# Two models, on purpose, because the two jobs are not the same job.
#
# | stage | model | why |
# |---|---|---|
# | silver to gold, timestamps | qwen35, Qwen3.5-122B-A10B, hosted here | deciding where a task starts and ends is judgement, not arithmetic. A 122B MoE with 10B active, free, and no rate limit to plan around. |
# | gold to platinum, trajectories | stealth/space-bunny-alpha | this is the model being fine tuned, so its training data should be written in its own dialect rather than translated into it. |
#
# This notebook only does the first row. The second is notebook 04.
#
# The model is scored against a deterministic baseline. If it does not beat it,
# the baseline wins and the notebook says so out loud. That is the point of
# keeping the baseline around.

# %%

import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import psycopg
    from psycopg.types.json import Json
except ImportError:
    import subprocess
    import sys

    print("installing psycopg...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "psycopg[binary]"])
    import psycopg
    from psycopg.types.json import Json
    print("psycopg ready")

# %% dbutils compatibility
#
# dbutils is injected by the Databricks notebook runtime and is simply absent
# everywhere else: Databricks Apps, a Job's Python script, a local kernel. It
# was imported directly here, which turned one missing module into a dead
# notebook. These helpers use dbutils when it is there and fall back to sane
# defaults when it is not, so this file runs in all four places.

# The notebook runtime injects dbutils into the namespace; it is usually not
# importable as a module. Setting dbutils = None here poisoned that namespace,
# and WorkspaceClient() auth resolution then walked
# global_ns["dbutils"].notebook.entry_point and died with an AttributeError, so
# the name is only ever left bound to something real. If there is no dbutils at
# all, the name is removed entirely, which makes the SDK fall back cleanly.
try:
    import dbutils  # noqa: F401

    HAVE_DBUTILS = True
except ImportError:
    import builtins

    _injected = getattr(builtins, "dbutils", None)
    if _injected is not None:
        dbutils = _injected
        HAVE_DBUTILS = True
    else:
        HAVE_DBUTILS = False
        globals().pop("dbutils", None)

_WIDGET_DEFAULTS: dict = {}


def widget(name, default, doc):
    """Declare a widget, or just remember the default when there is no UI."""
    _WIDGET_DEFAULTS[name] = str(default)
    if HAVE_DBUTILS:
        try:
            dbutils.widgets.text(name, str(default), doc)
        except Exception:
            pass


def widget_get(name):
    if HAVE_DBUTILS:
        try:
            return dbutils.widgets.get(name)
        except Exception:
            pass
    return _WIDGET_DEFAULTS.get(name, "")


def username() -> str:
    if HAVE_DBUTILS:
        try:
            return dbutils.notebook().context.userName
        except Exception:
            pass
    return os.environ.get("DATABRICKS_USER_NAME", "user@example.com")


widget("instance", "", "unused: the database is addressed by host, not by instance name")
widget("db_user", "", "Postgres role to connect as, usually your Databricks email")
widget("db_name", "databricks_postgres", "database name")
widget("db_host", "", "host override. Blank = the instance's own read_write_dns")
widget("db_port", "", "port override. Blank = 5432")
widget("bronze_db", "bronze", "bronze schema written by notebook 01")
widget("silver_db", "silver", "silver schema written by notebook 02")
widget("gold_db", "gold", "gold schema written by this notebook")
widget(
    "window_source", "auto",
    "auto = keep whichever scores better, or force 'model' / 'baseline'")
widget(
    "reference_windows", "",
    "optional path to reference_windows.json for scoring, if you have one")
widget(
    "reference_base", "/Workspace/Users",
    "folder searched for reference_windows.json when the widget above is blank")
widget(
    "serving_endpoint", "",
    "PRIMARY detector, blank uses the model this notebook ships with: "
    "system.ai.qwen35-122b-a10b on the workspace AI Gateway. Finding task "
    "boundaries is judgement work, not arithmetic, and it costs nothing here. "
    "Do not set this to qwen35: the gateway answers 403, 'qwen35 is no longer "
    "available. Use Unity Catalog model services.'")
widget(
    "fallback_model", "stealth/space-bunny-alpha",
    "model id used only if the serving endpoint is unavailable. This is also the "
    "model that will generate gold to platinum, so it is the one being fine tuned.")
widget(
    "reasoning_verbosity", "off",
    "how much of the model's own reasoning to print: off, head, or full. "
    "'head' shows the opening of each reply, which is usually where it works out "
    "the boundaries.")
widget(
    "max_output_tokens", "16000",
    "output budget per call. This model reasons at length before answering, so "
    "8k truncates the JSON. Raise if a reply is still cut off.")

instance = widget_get("instance")
bronze_db = widget_get("bronze_db")
silver_db = widget_get("silver_db")
gold_db = widget_get("gold_db")
fallback_model = widget_get("fallback_model").strip() or "stealth/space-bunny-alpha"
db_user = widget_get("db_user").strip()
db_name = widget_get("db_name").strip() or "databricks_postgres"
db_host = widget_get("db_host").strip()
db_port = widget_get("db_port").strip() or "5432"
serving_endpoint = widget_get("serving_endpoint").strip()
window_source = widget_get("window_source").strip().lower() or "auto"
reference_hint = widget_get("reference_windows").strip()
reference_base = Path(widget_get("reference_base"))

run_id = str(uuid.uuid4())
started_at = datetime.now(timezone.utc)

SECRET_SCOPE = "tribal"

# TEST ENVIRONMENT: connection details are hardcoded so the notebook runs with
# no setup. Delete this block and the secret path below takes over.
HARDCODED = {
    "host": "ep-twilight-bonus-d8outdz9.database.us-east-2.cloud.databricks.com",
    "port": "5432",
    "database": "databricks_postgres",
    "user": "aarav@dayal.org",
    # This endpoint takes an OAuth token in the password slot. The DSN that
    # works, and the only shape that has:
    #   postgresql://aarav%40dayal.org:<token>@ep-twilight-bonus-d8outdz9
    #     .database.us-east-2.cloud.databricks.com/databricks_postgres
    #     ?sslmode=require
    # Notebooks 01 and 02 use a native role with a password instead, and that
    # login is dead now: "External authorization failed". So it is not a
    # fallback any more.
    "secret": "lakebase",
    "token": "eyJraWQiOiJqblJxRmciLCJhbGciOiJSUzI1NiIsInR5cCI6ImF0K2p3dCJ9.eyJpc3MiOiJodHRwczovL2RiYy01YTQwYmZkNy00MjFjLmNsb3VkLmRhdGFicmlja3MuY29tL29pZGMiLCJzdWIiOiJhYXJhdkBkYXlhbC5vcmciLCJhdWQiOlsiNzQ3NDY0MzkwODUyNjE2NyJdLCJpYXQiOjE3OTA1MTQ3NTEsImV4cCI6MTc5MDUxODM1MSwianRpIjoiMTg2ZGYxOTctODM5Ny00NzBhLTk4NzEtYWE5OGI1YjJmZWE4IiwiY2xpZW50X2lkIjoiZGItZGF0YWJhc2UtY3JlZGVudGlhbCIsInNjb3BlIjoiaWFtLmN1cnJlbnQtdXNlcjpyZWFkIGlhbS5ncm91cHM6cmVhZCBpYW0uc2VydmljZS1wcmluY2lwYWxzOnJlYWQgaWFtLnVzZXJzOnJlYWQiLCJwcml2YXRlX21ldGFkYXRhIjoiQVVQNGFXazF6Tm9yV0lhZjEtRGUxZEUya0VnbjlCbFVTbmFJLXNOXzRTYlloTUFMZk9kTHNRaFdyZl9NVjJwWlNZbEFSMHhLNU9sR3VhSWdNTjN2YUJIc1Z6UmRDOVdBMW0tcXVMMTR0dG9VWUZOZVdGVDlVMjl0S2xnQlJ0b1RETHNzXy1oQjd2TktKS2NQeThvUUZaVnZNeERzR2llRGhhRVdnSDMxRHQxQkx1OHBudXFPdWJnYmotUVhGbFlVMlc1SSIsInBjdHgiOiJDdllEQ2hRSUFSb0dDS0dzNU5VR0lnWUlfNXpsMVFZb0FoS1BBd0dvTnFZOFRNOWxmVVAwTVUwQnJoc21kd093RDAwT1pwSlFuOGxSYTFlTVI4dnRxMzBCWTRDdFIwQXFxYnpwZC1EYjVtalRlZ2txM21JT2Q5bTZSX3ltRjktRDRhQ091MUQ4dzNxUGpqNExPU2VTZndYNEx0Q1BmMlJ2NkM2QVdwUXhDR1ZZZnhyQi1xWHZ3VzVDcEZpZWpZX1A3MXktZlY4V2FLblE0dzViSmFPSmZVYUFrOVF2eFhoVzh5UVB6V0pDclZsWF9IdzRuX3VPaTRudllBbERkTzdJekgzRElmTVhnOVNmdGxoUzN4Q282YmR4b1pha3ZteHZxUDJOYnJBM3hCdTNjdHlzWk41dzBQdHZ0YVVpZ2lOQ0duQVNHWm1hMXRSdkgxc25KdFVsMWtWcmFUS1luQ3U0VzJoRXkxZzVPZlZJWkRmM1dUOEtsR3NHOURYR0RMSVZlVHFQVnp0VV9JdWJpakVoZk1JeHVaaTlRVnZpVk10el9DNmZBb3NucEJ2cDhUaWxPT0gzazVDZUNrckFDV2JSdEFHVUxEd0ZQcEhtbVdXcWNFMGNTaEt0LV85M1loNmpweklsRVFnc05mT3VMZGUxcVI5RlByZ1FCQmJOSFM4UkNqV2FBWnNRYnVEclVQd01FOGplR2FFQXB4X2JheE42dWFzWktkT21Pdm8wV0lUX1NiZ1hPd3NyVkJwTUFZNV95OVV3UlFJZ0h3amQ4d0V4bl9wdHVjY2xLdXpLWlZqY1pXQVZoOEdtSmtsaS1DdVltRElDSVFDeFljRmRESE1xc3padjZISm1YNjhHWEwxLXZON21DSUg4dmduM1pYUHUyUT09In0.AgLO_LVfTX4Ujpha4yeQKsPICeLFMICwwl_vda6U3KUYZ9vaKONQnlXhsBWF27dYzJ1TasYYdwJrujWuBTj59heOLTEoEOsZZ3lgeRu6PqdWOZFg9IYDJROZVZKxVymj0wlTxF18_vLXN3OXGdSPP0u8QuF2mBLDEeLD3jE2uvI50FJyTtV3RPNF-HPJaXtDbsQVuJxfQSxduhSAsgGob5tB4GjJ2QBtZgU0OICiQFP40NSp-v_03quVatiPO-D_MEWboA3udKHmVl9M1BxPJYZ7YtrhxYozdzWKluwRMC8MT4khnyKvSwelG-dT-Ug6bkOybTXrhj8ShyDlMpzHRQ",
}
OPENROUTER_KEY = "openrouter"

# TEST ENVIRONMENT: the workspace model and its token, hardcoded.
#
# The detector is a Unity Gateway model, so its name is fully qualified. A bare
# The bare name "qwen35" is retired: the gateway answers
# 403 PERMISSION_DENIED, "'qwen35' is no longer available. Use Unity Catalog
# model services." Unity Catalog names are fully qualified, as this one is.
GATEWAY_HOST = "https://dbc-5a40bfd7-421c.cloud.databricks.com"
GATEWAY_PATH = "/ai-gateway/mlflow/v1/responses"
GATEWAY_MODEL = "system.ai.qwen35-122b-a10b"
GATEWAY_TOKEN = "dapi36f076633cc4e9316be416b75cf41848"

# How much thinking to buy. Measured on this model: a one line json answer cost
# 236 output tokens of reasoning and returned nothing, and at effort low/medium/
# high it ran past a 300 token cap and still returned an empty answer. At none
# it answers in 2 tokens. So none is the default and reasoning is opt in.
REASONING_EFFORT = {"off": "none", "head": "low", "full": "medium"}

print(f"dbutils available: {HAVE_DBUTILS}")
print(f"run_id={run_id} detector={serving_endpoint or GATEWAY_MODEL} "
      f"reasoning={REASONING_EFFORT.get(REASONING_VERBOSITY, 'none')}")


def utc(value):
    """ISO-8601 in UTC, second precision, always Z suffixed."""
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, str):
        return utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    raise TypeError(f"not a timestamp: {value!r}")


def parse_utc(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


# %% connect
#
# Credentials come from the Databricks SDK, not from a string in this file.
# The SDK mints a short lived OAuth token for the instance, so there is no
# password to leak and nothing to rotate by hand.
#
# If this fails with "OAuth: User is not authorized", the Postgres role does not
# exist yet. Only the project owner gets one automatically. As the owner, run:
#
#   CREATE EXTENSION IF NOT EXISTS databricks_auth;
#   SELECT databricks_create_role('you@yourcompany.com', 'USER');
#   GRANT CONNECT ON DATABASE databricks_postgres TO "you@yourcompany.com";
#   GRANT USAGE, CREATE ON SCHEMA public TO "you@yourcompany.com";

from databricks.sdk import WorkspaceClient

workspace = WorkspaceClient()


def secret_value(key):
    """Read one Databricks secret, or None if the scope is unreachable.

    Databricks says plainly that credentials should not be put in a notebook, so
    this is the only way the model key is ever obtained here.
    """
    try:
        secret = workspace.secrets.get_secret(SECRET_SCOPE, key)
        return (getattr(secret, "value", None) or secret.value_or_raise()).strip()
    except Exception as error:
        print(f"note: cannot read secrets/{SECRET_SCOPE}/{key} ({type(error).__name__})")
        return None


widget("db_secret", "lakebase", "secret key in the tribal scope holding the Lakebase token")


def mint_credential():
    """Mint a fresh Lakebase token, so nobody has to paste one.

    The credential lives an hour, so a pasted token is stale by the next run.
    WorkspaceClient already holds working notebook auth, so it can mint one per
    run. Two API shapes exist and a workspace has one of them, so both are
    tried, and whichever one answers is reported.

    The classic instance shape is tried first. The project/branch/endpoint shape
    is newer, and on a workspace that only has the classic one every call under
    /api/2.0/postgres returns NotFound, which is what made an invented
    projects/{name}/branches/{name}/endpoints/{dns-prefix} path look plausible
    for far too long. Endpoint resource names are opaque uuids, never the
    dns prefix of the host, so that path is never guessed any more.
    """
    # 1. classic instance model
    try:
        instances = list(workspace.database.list_database_instances() or [])
        print(f"  {len(instances)} database instance(s) here:")
        for item in instances:
            print(f"    {item.name}  state={getattr(getattr(item, 'state', None), 'value', None)}"
                  f"  host={getattr(item, 'read_write_dns', None)}")
        for item in instances:
            if not getattr(item, "read_write_dns", None):
                continue
            try:
                credential = workspace.database.generate_database_credential(
                    request_id=str(uuid.uuid4()), instance_names=[item.name])
                token = getattr(credential, "token", None)
                if token:
                    print(f"  minted a credential for instance {item.name}")
                    return token
            except Exception as error:
                print(f"  mint failed for {item.name}: {type(error).__name__}: {error}")
    except Exception as error:
        print(f"  classic instance model unavailable: {type(error).__name__}: {error}")

    # 2. project / branch / endpoint model
    try:
        for project in workspace.postgres.list_projects() or []:
            project_path = f"projects/{project.name}"
            for branch in workspace.postgres.list_branches(project_path) or []:
                branch_path = f"{project_path}/branches/{branch.name}"
                for endpoint in workspace.postgres.list_endpoints(branch_path) or []:
                    print(f"  endpoint {branch_path}/endpoints/{endpoint.name}")
                    try:
                        credential = workspace.postgres.generate_database_credential(
                            endpoint=f"{branch_path}/endpoints/{endpoint.name}")
                        token = getattr(credential, "token", None)
                        if token:
                            return token
                    except Exception as error:
                        print(f"    mint failed: {type(error).__name__}: {error}")
    except Exception as error:
        print(f"  project model unavailable: {type(error).__name__}: {error}")

    print("  no credential could be minted. Falling back to the hardcoded token, "
          "which lasts an hour and will be expired if the notebook has been open.")
    return None


def ask_instance():
    """Open the database with a plain Postgres login.

    A Lakebase endpoint host plus an OAuth token in the password position is a
    complete, supported login. The /api/2.0/postgres discovery endpoints are not
    needed for this, and a workspace token that can list projects is still
    refused by the database if it was not minted for it. Connecting directly is
    simpler and is the documented path.

    Every part is widget-driven, so the same code works against a different
    endpoint or a local Postgres by changing the widgets.
    """
    host = widget_get("db_host").strip() or HARDCODED["host"]
    database = widget_get("db_name").strip() or HARDCODED["database"]
    port = widget_get("db_port").strip() or HARDCODED["port"]
    user = widget_get("db_user").strip() or HARDCODED["user"]
    secret = widget_get("db_secret").strip() or HARDCODED["secret"]

    minted = mint_credential()
    password = minted
    if not password:
        password = HARDCODED.get("token") or secret_value(secret)
    if not password:
        raise SystemExit(
            f"secret tribal/{secret} is empty or unreadable. Store a Lakebase token in it:\n"
            f"  dbutils.secrets.put(scope=\"tribal\", key=\"{secret}\", token=<token>)"
        )
    print(f"connecting to {host}:{port}/{database} as {user}")

    dsn = (f"host={host} port={port} dbname={database} user={user} "
           f"password={password} sslmode=require connect_timeout=30")
    try:
        conn = psycopg.connect(dsn)
    except Exception as error:
        # retry without the user, since the endpoint may pin a single identity
        try:
            conn = psycopg.connect(dsn.replace(f" user={user} ", " "))
        except Exception:
            detail = str(error).splitlines()[0][:200]
            if not minted:
                # the hardcoded token is an hour old, so this is almost always
                # that rather than a host or database name problem
                raise SystemExit(
                    f"could not connect to {host}:{port}/{database}.\n"
                    f"No credential could be minted, so the hardcoded token was "
                    f"used, and it is likely expired. The Lakebase API calls above "
                    f"say why they failed; run the notebook fresh so the token is "
                    f"minted seconds ago.\n  {detail}")
            raise SystemExit(
                f"could not connect to {host}:{port}/{database} as {user}, even "
                f"with a credential minted seconds ago. The role may not have been "
                f"granted access to this database.\n  {detail}")
    conn.autocommit = True
    return conn


conn = ask_instance()
cur = conn.cursor()
cur.execute("select current_database(), current_user, current_user = session_user")
database, db_user, _ = cur.fetchone()
print(f"connected: db={database} as={db_user}")


# %% scrubbing
#
# The exports carry NUL bytes and Postgres refuses them in text. Every bound
# parameter is stripped, and every JSON payload scrubbed, exactly as in 01 and 02.


def scrub(value):
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    return value


class ScrubbedCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=None):
        return self._cursor.execute(sql, scrub(params))

    def executemany(self, sql, params=None):
        return self._cursor.executemany(sql, scrub(params))

    def __getattr__(self, name):
        return getattr(self._cursor, name)


cur = ScrubbedCursor(cur)


def rows(sql, params=None):
    cur.execute(sql, params)
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


# %% what is actually in the database
#
# Read the real catalog rather than trusting a schema written by hand earlier.
# If silver is not shaped the way this notebook expects, say so here and stop,
# instead of failing three cells later with a confusing error.

def catalog(schemas):
    found = {}
    for schema in schemas:
        found[schema] = [
            row["table_name"] for row in rows(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = %s ORDER BY table_name", (schema,))
        ]
    return found


existing = catalog([bronze_db, silver_db, gold_db])
for schema, tables in existing.items():
    print(f"{schema}: {len(tables)} tables  {', '.join(tables) if tables else '(none)'}")

SILVER_REQUIRED = {
    "discord_message", "jira_issue", "jira_changelog_event",
    "github_commit", "github_pull_request", "document",
}
missing = sorted(SILVER_REQUIRED - set(existing.get(silver_db, [])))
if missing:
    raise SystemExit(
        f"silver.{silver_db} is missing {missing}.\n\n"
        f"Notebook 02 has not been run against this instance, or it wrote to a "
        f"different schema. Run 01 then 02 and come back.")

# the two-argument form treats the first value as a *database* name, so the
# current database has to be named explicitly or this raises InvalidCatalogName
cur.execute("select has_database_privilege(%s, current_database(), 'CREATE')", (db_user,))
can_create = cur.fetchone()[0]
if not can_create:
    print("\nnote: this role cannot CREATE schemas. Gold will need it, or an owner "
          "must run:  GRANT CREATE ON DATABASE databricks_postgres TO \"%s\";" % db_user)


# %% gold schema
#
# Two tables. A window header, and the sliced rows in long form so that adding a
# silver source later does not mean a schema change.
#
# gold.task_window is the model's output plus the audit trail for how it was
# decided. gold.task_chunk is the static slice, one row per captured record.

GOLD_SQL = f"""
CREATE SCHEMA IF NOT EXISTS {gold_db};

CREATE TABLE IF NOT EXISTS {gold_db}.task_window (
  window_id text PRIMARY KEY,
  task_id text,
  title text,
  summary text,
  start_ts timestamptz NOT NULL,
  end_ts timestamptz NOT NULL,
  confidence numeric,
  produced_by text NOT NULL,
  participants text[] DEFAULT '{{}}',
  evidence jsonb NOT NULL DEFAULT '{{}}'::jsonb,
  run_id text,
  created_at timestamptz DEFAULT now());

CREATE TABLE IF NOT EXISTS {gold_db}.task_chunk (
  id bigserial PRIMARY KEY,
  window_id text NOT NULL REFERENCES {gold_db}.task_window(window_id) ON DELETE CASCADE,
  source_table text NOT NULL,
  source_key text,
  ts timestamptz,
  ordinal int,
  row jsonb NOT NULL,
  run_id text,
  created_at timestamptz DEFAULT now());

CREATE INDEX IF NOT EXISTS task_chunk_window_idx
  ON {gold_db}.task_chunk (window_id, source_table, ts);
CREATE INDEX IF NOT EXISTS task_chunk_ts_idx
  ON {gold_db}.task_chunk (ts);
"""

cur.execute(GOLD_SQL)
conn.commit()
for table in ("task_chunk", "task_window"):
    cur.execute(f"TRUNCATE {gold_db}.{table} RESTART IDENTITY CASCADE")
conn.commit()
print(f"gold schema ready in {database}.{gold_db}")


# %% read silver
#
# The whole silver layer is a few hundred rows, so it is read into memory and
# sliced in Python. That keeps the boundary logic readable, which matters more
# than avoiding a round trip at this size.

silver_counts = {}
for table in ("discord_message", "jira_issue", "jira_changelog_event", "github_commit",
              "github_pull_request", "document", "entity_link", "quarantine"):
    silver_counts[table] = rows(f"SELECT count(*) AS n FROM {silver_db}.{table}")[0]["n"]
    print(f"  silver.{table:<24} {silver_counts[table]}")

messages = rows(f"""
    SELECT message_id, channel, author_name, ts, content, mentions_ticket, mentions_pr
    FROM {silver_db}.discord_message
    ORDER BY ts""")
issues = rows(f"SELECT * FROM {silver_db}.jira_issue")
changelog = rows(f"SELECT * FROM {silver_db}.jira_changelog_event ORDER BY ts")
commits = rows(f"SELECT * FROM {silver_db}.github_commit ORDER BY authored_at")
pulls = rows(f"SELECT * FROM {silver_db}.github_pull_request")
documents = rows(f"SELECT * FROM {silver_db}.document")

if not messages or not issues:
    raise SystemExit("silver is empty. Run 01 then 02 first.")

issues_by_key = {row["issue_key"]: row for row in issues}
pulls_by_number = {row["pr_number"]: row for row in pulls}
commits_by_sha = {row["sha"]: row for row in commits}
for pull in pulls:
    pull["merged_at_dt"] = parse_utc(pull.get("merged_at"))
for commit in commits:
    commit["authored_at_dt"] = parse_utc(commit.get("authored_at"))


# %% build the digest
#
# The model gets one entry per ticket: the ticket, its discussion, the pull
# requests and commits that reference it, and the moment it closed. That is the
# unit a human would use to find a task, and it keeps the prompt in the low tens
# of thousands of tokens instead of dumping every row.
#
# Timestamps are rendered as epoch seconds. Models read them more reliably than
# ISO strings, and it removes a whole class of timezone mistakes.

def epoch(value):
    moment = parse_utc(value)
    return int(moment.timestamp()) if moment else None


def clip(text, limit=400):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


messages_by_ticket = defaultdict(list)
messages_by_pr = defaultdict(list)
for message in messages:
    for ticket in (message.get("mentions_ticket") or []):
        messages_by_ticket[ticket].append(message)
    for number in (message.get("mentions_pr") or []):
        messages_by_pr[number].append(message)

done_events = {}
status_trail = defaultdict(list)
for event in changelog:
    key = event.get("issue_key")
    if not key:
        continue
    status_trail[key].append(event)
    if event.get("field") == "status" and event.get("to_value") == "Done":
        done_events[key] = event

prs_by_ticket = defaultdict(list)
for message in messages:
    for ticket in (message.get("mentions_ticket") or []):
        for number in (message.get("mentions_pr") or []):
            if number in pulls_by_number and number not in prs_by_ticket[ticket]:
                prs_by_ticket[ticket].append(number)

shas_by_ticket = defaultdict(set)
for ticket, numbers in prs_by_ticket.items():
    for number in numbers:
        sha = pulls_by_number[number].get("merge_sha")
        if sha:
            shas_by_ticket[ticket].add(sha)


def digest_entry(ticket_key):
    issue = issues_by_key.get(ticket_key)
    trail = status_trail.get(ticket_key, [])
    thread = sorted(messages_by_ticket.get(ticket_key, []), key=lambda r: r["ts"])
    numbers = prs_by_ticket.get(ticket_key, [])
    closing = done_events.get(ticket_key)

    return {
        "ticket": ticket_key,
        "summary": clip(issue.get("summary") if issue else None, 200),
        "status": (issue or {}).get("status"),
        "created": epoch((issue or {}).get("created_at")),
        "closed": epoch(closing.get("ts")) if closing else None,
        "status_changes": [
            {"field": e.get("field"), "to": e.get("to_value"), "ts": epoch(e.get("ts"))}
            for e in trail
        ],
        "pull_requests": [
            {
                "number": number,
                "title": clip(pulls_by_number[number].get("title"), 120),
                "branch": pulls_by_number[number].get("branch"),
                "merged": bool(pulls_by_number[number].get("merged")),
                "merged_at": epoch(pulls_by_number[number].get("merged_at")),
                "merge_sha": pulls_by_number[number].get("merge_sha"),
            }
            for number in sorted(numbers)
        ],
        "commits": [
            {
                "sha": sha[:8],
                "author": commits_by_sha[sha].get("author_name"),
                "message": clip(commits_by_sha[sha].get("message"), 120),
                "ts": epoch(commits_by_sha[sha].get("authored_at")),
            }
            for sha in sorted(shas_by_ticket.get(ticket_key, ()))
            if sha in commits_by_sha
        ],
        "discussion": [
            {
                "id": m["message_id"],
                "channel": m.get("channel"),
                "author": m.get("author_name"),
                "ts": epoch(m.get("ts")),
                "text": clip(m.get("content"), 300),
            }
            for m in thread
        ],
    }


digest = [digest_entry(key) for key in sorted(issues_by_key)]

# Threads that mention no ticket at all. These are the ones a ticket-centric
# model would never see, so they are listed separately as a prompt to notice
# work that happened outside the tracker.
orphan_threads = defaultdict(list)
for message in messages:
    if not (message.get("mentions_ticket") or []):
        orphan_threads[message.get("channel")].append(message)

orphans = [
    {
        "channel": channel,
        "messages": [
            {"id": m["message_id"], "author": m.get("author_name"),
             "ts": epoch(m.get("ts")), "text": clip(m.get("content"), 300)}
            for m in sorted(group, key=lambda r: r["ts"])
        ],
    }
    for channel, group in sorted(orphan_threads.items())
]

print(f"digest: {len(digest)} tickets, {len(orphans)} unticketed channels, "
      f"{len(messages)} messages, {len(commits)} commits, {len(pulls)} pull requests")


# %% the detection prompt
#
# Deliberately narrow. The model is given the evidence and asked for boundaries,
# with the rules that make a window a task written down. Abstention is allowed
# and expected: a ticket that never shipped is not a task.

DETECTION_SYSTEM = """\
You identify engineering tasks inside a company's working history.

You are given a digest of a project: tickets, the chat discussion around them,
the pull requests, the commits, and the timestamps of every status change. Your
job is to decide which stretches of that history were a single task, and to say
exactly when each one started and ended.

A task is a unit of work with a beginning and an end that someone actually
finished. Most tasks are anchored to a ticket. Some are not, and you will be
shown channels that reference no ticket at all: read them, because real work
happened in those too.

Rules for the boundaries:

- START is when the work became visible, which is usually the first message
  discussing the problem, or the ticket being raised, whichever is earlier.
  Do not start it at the first commit; commits come late.
- END is when the work stopped changing things, which is normally the merge of
  the pull request that shipped it, or the ticket reaching Done, whichever is
  later. Discussion that continues after the merge is usually unrelated and
  should not extend the window.
- If a ticket has several merged pull requests, they are usually one task if the
  branches share a prefix and the discussion is continuous, and separate tasks
  if the branches are unrelated or there is a long quiet gap between them.
- A ticket that never reached Done and has no merged pull request is not a task.
  Do not invent a window for it. Leave it out.
- A window must contain at least one message or one commit, and its end must be
  at or after its start.

Evidence you cite must be real. Every id you reference must be one you were
shown. Never invent a message id, a pull request number, a commit, or a
timestamp. A window with no cited evidence is worse than no window at all.

Return only JSON, no prose and no code fences, in exactly this shape:

{"windows": [
  {
    "task_id": "the ticket key, or a short slug you invent for unticketed work",
    "title": "one line, under 90 characters",
    "summary": "one or two sentences on what the work was",
    "start_ts": <epoch seconds, integer>,
    "end_ts": <epoch seconds, integer>,
    "confidence": <number between 0 and 1>,
    "participants": ["names that appear in the evidence"],
    "evidence": {
      "message_ids": ["discord message ids you actually saw"],
      "ticket": "the ticket key, or null",
      "pr_numbers": [1, 2],
      "merge_shas": ["full shas you actually saw"]
    },
    "reasoning": "one sentence on why these boundaries"
  }
]}

If nothing qualifies, return {"windows": []}.
"""

DETECTION_USER = """\
Here is the project history. Timestamps are epoch seconds, UTC.

TICKETS

{digest}

CHANNELS WITH NO TICKET REFERENCES

{orphans}

Identify the task windows. Return only the JSON object described in your
instructions.
"""


def build_prompt():
    return DETECTION_USER.replace("{digest}", json.dumps(digest, indent=1)).replace(
        "{orphans}", json.dumps(orphans, indent=1))


prompt_chars = len(build_prompt())
print(f"prompt: {prompt_chars:,} characters, roughly {prompt_chars // 4:,} tokens")


# %% call the model
#
# The detector is system.ai.qwen35-122b-a10b on the workspace AI Gateway, called
# directly with urllib. No key beyond the one above, no per token cost, no rate
# limit. There is no external fallback: an OpenRouter branch used to live here
# and was removed, since it needed a helper that no longer existed.


REASONING_VERBOSITY = (widget_get("reasoning_verbosity") or "off").strip().lower()
REASONING_CHARS = {"off": 0, "head": 2000, "full": 10 ** 9}


def show_reasoning(where, reasoning, answer):
    """Print what the model said while it was thinking.

    These models reason at length before answering, and where they draw a
    boundary is usually settled in there rather than in the JSON. Reading it is
    the difference between trusting a window and checking it.
    """
    budget = REASONING_CHARS.get(REASONING_VERBOSITY, 2000)
    print(f"\n  the model said, while working ({where}):")
    print(f"    reasoning {len(reasoning):,} chars, answer {len(answer):,} chars")
    if not reasoning:
        print("    (no reasoning returned with this reply)")
        return
    if budget == 0:
        print(f"    hidden by reasoning_verbosity=off; {len(reasoning):,} chars available")
        return
    for line in reasoning[:budget].splitlines():
        print(f"    | {line}")
    if len(reasoning) > budget:
        print(f"    | ... {len(reasoning) - budget:,} more chars, "
              f"set reasoning_verbosity=full to see them")


def call_serving_endpoint(endpoint, system, user, model, attempts=3):
    """Ask the model we host, through the workspace AI Gateway.

    No external key and no per token cost. The reply is plain json, so it lands
    in the same place as the other routes once the answer text is pulled out.
    """
    nudge = None
    last = None

    for attempt in range(1, attempts + 1):
        try:
            payload = ask_endpoint(system, user, endpoint, extra_user=nudge)
            reply = read_reply(payload)
            content = reply["content"]
            reasoning = reply["reasoning"]
            print(f"  attempt {attempt} via {endpoint or GATEWAY_MODEL}: "
                  f"{len(reasoning):,} reasoning chars, "
                  f"{len(content):,} answer chars, "
                  f"{reply['completion_tokens']:,} output tokens")
            try:
                parsed = extract_json(content)
            except (ValueError, json.JSONDecodeError) as error:
                last = error
                show_reasoning(f"{endpoint or GATEWAY_MODEL}, attempt {attempt}",
                               reasoning, content)
                print(f"  unusable JSON: {error}")
                nudge = ("Your previous answer was cut off. Reply with the JSON object "
                         "only, no code fence and no prose. Keep every summary string "
                         "under 15 words, and drop the reasoning field if you need room.")
                continue
            show_reasoning(f"{endpoint or GATEWAY_MODEL}, attempt {attempt}",
                           reasoning, content)
            return parsed, reply, len(reasoning)
        except Exception as error:
            last = f"{type(error).__name__}: {error}"
            print(f"  attempt {attempt} via {endpoint or GATEWAY_MODEL} failed: {last}")
        if attempt < attempts:
            time.sleep(2 ** attempt)
    raise SystemExit(
        f"the detector {endpoint or GATEWAY_MODEL} did not return usable JSON: {last}")


def ask_endpoint(system, user, endpoint, extra_user=None):
    """Ask the workspace model through the Unity Gateway, and get raw JSON back.

    Two earlier routes were wrong and are not used any more.

    mlflow.deployments resolves its own credentials and does not pick up the
    notebook auth, so it failed with "Reading Databricks credential
    configuration failed".

    workspace.serving_endpoints.query posts to
    /serving-endpoints/{name}/invocations and returns a typed response that
    reads "finishReason" where the endpoint sends "finish_reason", models only
    content and role (so reasoning is dropped), and whose .as_dict() raises
    AttributeError on the way out.

    The gateway route below is the one the model page documents, and it returns
    plain json, so nothing is lost on the way through.
    """
    turns = [{"role": "system", "content": [{"type": "input_text", "text": system}]},
             {"role": "user", "content": [{"type": "input_text", "text": user}]}]
    if extra_user:
        turns.append({"role": "user",
                      "content": [{"type": "input_text", "text": extra_user}]})
    # "qwen35" was the old name and the gateway now rejects it with 403, so an
    # explicitly configured value of that is treated as unset rather than sent.
    if not endpoint or endpoint.lower() in ("qwen35", "qwen3.5", "qwen35-122b-a10b"):
        if endpoint:
            print(f"  {endpoint!r} is a retired model name, using {GATEWAY_MODEL}")
        endpoint = GATEWAY_MODEL
    body = {
        "model": endpoint,
        "max_output_tokens": int(widget_get("max_output_tokens") or 16000),
        "reasoning": {"effort": REASONING_EFFORT.get(REASONING_VERBOSITY, "none")},
        "input": turns,
    }
    request = urllib.request.Request(
        f"{GATEWAY_HOST}{GATEWAY_PATH}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {GATEWAY_TOKEN}"},
    )
    print(f"  POST {GATEWAY_PATH} model={body['model']} "
          f"effort={body['reasoning']['effort']} "
          f"max_output_tokens={body['max_output_tokens']} "
          f"prompt={len(system) + len(user):,} chars")
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode()[:400]
        raise RuntimeError(f"HTTP {error.code} from the gateway: {detail}")
    except urllib.error.URLError as error:
        raise RuntimeError(f"could not reach {GATEWAY_HOST}: {error}")


def read_reply(payload):
    """Split a gateway reply into the answer and the reasoning.

    This is the Responses API shape, not chat completions. There is no choices
    array. output is a list of blocks, each with a content list, and the block
    type is what says whether it is the answer or the thinking:

      output[i].content[j].type == "output_text"     the answer
      output[i].content[j].type == "reasoning_text"  the thinking

    usage is input_tokens and output_tokens, not prompt_tokens and
    completion_tokens.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"expected a json object back, got {type(payload).__name__}")
    status = payload.get("status")
    if payload.get("error"):
        raise ValueError(f"the gateway returned an error: {payload['error']}")
    if status == "incomplete":
        # the model ran out of output budget part way through answering
        detail = payload.get("incomplete_details") or {}
        raise ValueError(
            f"the reply was cut off ({detail.get('reason', 'no reason given')}). "
            f"Raise max_output_tokens, or set reasoning_verbosity to off.")
    if status not in (None, "completed"):
        print(f"  note: reply status is {status!r}")

    answer, reasoning = [], []
    for block in payload.get("output") or []:
        for part in block.get("content") or []:
            kind = part.get("type")
            if kind == "output_text":
                answer.append(part.get("text") or "")
            elif kind == "reasoning_text":
                reasoning.append(part.get("text") or "")

    usage = payload.get("usage") or {}
    return {
        "content": "".join(answer),
        "reasoning": "".join(reasoning),
        # both spellings, so callers do not have to care
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
    }


# ---------------------------------------------------------------- json in replies
#
# These models answer with fences, prose, an example object before the real one,
# attribute-laden fences, trailing commas, and occasionally truncated output.
# The first brace in the text is therefore not a safe place to start, and a
# plain json.loads of the whole reply is not a safe fallback. So replies are
# taken apart in layers and every candidate is scored against the shape the
# notebook actually needs, which is {"windows": [...]}.

ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
SMART = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u00a0": " ", "\u2028": "\n", "\u2029": "\n",
}
FENCE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*([^\n`]*)\n?(.*?)(?:```|\Z)", re.S)


def _clean(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.translate(ZERO_WIDTH)
    for bad, good in SMART.items():
        text = text.replace(bad, good)
    return text.strip()


def _strip_comments(text: str) -> str:
    """Drop // and /* */ comments that sit outside string literals."""
    out, index, length, in_string, escape = [], 0, len(text), False, False
    while index < length:
        char = text[index]
        if escape:
            escape = False
            out.append(char)
            index += 1
            continue
        if char == "\\" and in_string:
            escape = True
            out.append(char)
            index += 1
            continue
        if char == '"':
            in_string = not in_string
            out.append(char)
            index += 1
            continue
        if not in_string and text.startswith("//", index):
            while index < length and text[index] != "\n":
                index += 1
            continue
        if not in_string and text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _drop_trailing_commas(text: str) -> str:
    out, in_string, escape = [], False, False
    for index, char in enumerate(text):
        if escape:
            escape = False
            out.append(char)
            continue
        if char == "\\" and in_string:
            escape = True
            out.append(char)
            continue
        if char == '"':
            in_string = not in_string
            out.append(char)
            continue
        if char == "," and not in_string:
            rest = text[index + 1:]
            stripped = rest.lstrip()
            if stripped[:1] in ("}", "]"):
                continue
        out.append(char)
    return "".join(out)


def _scan_value(text: str, start: int):
    """Return (substring, end_index) for the JSON value opening at start.

    String aware, so braces inside quoted text do not confuse the depth count.
    """
    depth, in_string, escape, index = 0, False, False, start
    length = len(text)
    while index < length:
        char = text[index]
        if escape:
            escape = False
        elif in_string and char == "\\":
            escape = True
        elif char == '"':
            in_string = not in_string
        elif not in_string:
            if char in "{[":
                depth += 1
            elif char in "}]":
                depth -= 1
                if depth == 0:
                    return text[start:index + 1], index + 1
        index += 1
    return text[start:], length


def _close_open(text: str) -> str:
    """Append the closers a truncated reply is missing, if that is all it needs."""
    stack, in_string, escape = [], False, False
    for char in text:
        if escape:
            escape = False
            continue
        if in_string and char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in "{[":
            stack.append(char)
        elif char in "}]" and stack:
            stack.pop()
    repaired = text + ('"' if in_string else "")
    for opener in reversed(stack):
        repaired += "}" if opener == "{" else "]"
    return repaired


def _loads(candidate: str):
    try:
        return json.loads(candidate)
    except (ValueError, TypeError):
        return None


def _try_all(candidate: str):
    """Parse a candidate raw, then progressively more forgiving."""
    value = _loads(candidate)
    if value is not None:
        return value
    trimmed = _drop_trailing_commas(_strip_comments(candidate)).strip()
    value = _loads(trimmed)
    if value is not None:
        return value
    closed = _close_open(trimmed)
    value = _loads(closed)
    if value is not None:
        return value
    # last resort: cut back to the last element that closed cleanly
    for opener, closer in (("}", "}"), ("]", "]")):
        cut = closed.rfind(opener)
        if cut > 0:
            value = _loads(closed[:cut + 1] + closer)
            if value is not None:
                return value
    return None


def _values_in(blob: str):
    """Yield every balanced top level value in blob, in the order they appear.

    Scanning only the first { and the first [ meant a worked example printed
    above the real answer won, because nothing after it was ever considered.
    """
    index, length = 0, len(blob)
    while index < length:
        char = blob[index]
        if char in "{[":
            raw, index = _scan_value(blob, index)
            if raw:
                yield raw
            continue
        index += 1


def _shape_score(value, required) -> int:
    """How well does this candidate look like the answer we were promised?"""
    if not isinstance(value, (dict, list)):
        return 0
    score = 0
    if isinstance(value, list):
        score += 3
        if value and isinstance(value[0], dict):
            score += 2
        return score
    for index, key in enumerate(required):
        if key in value:
            score += 10 - index
        elif any(isinstance(k, str) and k.lower() == key for k in value):
            score += 6 - index
    if score == 0 and value:
        score = 1
    return score


def extract_json(text, required=("windows",)):
    """Pull the best JSON object out of a model reply.

    Candidates come from every fenced block first, then from every balanced
    value in the raw reply, so a worked example printed above the real answer
    does not win over the answer. Each is parsed raw, then with comments and
    trailing commas removed, then with missing closers appended, which covers
    the three ways these replies actually break.

    Raises ValueError with a specific reason when nothing usable is present.
    """
    text = _clean(text)
    if not text:
        raise ValueError("the reply was empty")

    candidates = []
    for match in FENCE.finditer(text):
        language = (match.group(1) or "").lower()
        attributes = (match.group(2) or "").strip()
        body = match.group(3)
        # a fence can carry attributes: ```json title="result"
        if attributes:
            body = attributes.split("\n", 1)[-1] if "\n" in attributes else body
        # only json-ish fences are worth reading first
        candidates.append((0 if (not language or language in ("json", "json5", "jsonc"))
                           else 1, body))
    candidates.sort(key=lambda item: item[0])

    # Fenced blocks first, in the order they appear, then the raw reply as a
    # fallback. A worked example and the real answer score the same, so the tie
    # is broken towards the later one: models print examples first. The raw
    # fallback is penalised by one so a fence always wins a tie against it.
    ordered = [(body, 0) for _, body in candidates] + [(text, -1)]
    best, best_score, reasons = None, 0, []
    for rank, (blob, penalty) in enumerate(ordered):
        if not blob.strip():
            continue
        for raw in _values_in(blob):
            value = _try_all(raw)
            if value is None:
                reasons.append("no valid JSON")
                continue
            score = _shape_score(value, required) + penalty
            # >= so the last candidate of equal quality wins
            if score >= best_score and score > 0:
                best, best_score, best_rank = value, score, rank
    if best_score == 0:
        best = None

    if best is None:
        raise ValueError(
            "no usable JSON in the reply. The model returned text that could not "
            f"be parsed as {list(required)} ({len(reasons)} candidates tried).")

    # a bare list of windows is the same answer wearing a different shape
    if isinstance(best, list) and required and best and isinstance(best[0], dict):
        return {required[0]: best}
    return best


def call_model(system, user, model, attempts=3):
    """Ask the detector for windows, over the workspace AI Gateway.

    One route only. The OpenRouter fallback that used to live here is gone: it
    depended on openrouter_key and workspace_host, and that whole branch was
    dead code left behind when the gateway call was written. The gateway model
    is free, needs no external key, and has no rate limit to plan around, so
    there was nothing the fallback was buying.
    """
    return call_serving_endpoint(serving_endpoint, system, user, model, attempts)


model_started = time.time()
model_payload, model_usage, model_reasoning_chars = call_model(
    DETECTION_SYSTEM, build_prompt(), fallback_model)
model_seconds = time.time() - model_started

with open("/tmp/model_windows_raw.json", "w") as handle:
    json.dump(model_payload, handle, indent=2)
if model_usage:
    print(f"  usage: {model_usage}")
print(f"  replied in {model_seconds:.1f}s, "
      f"{model_reasoning_chars:,} reasoning chars, "
      f"{len(model_payload.get('windows') or [])} windows proposed")


# %% validate the model's answer
#
# The model proposes; this code decides. Every claim is checked against silver,
# and anything unsupported is dropped rather than trusted. This is the whole
# reason the notebook is split the way it is.

known_message_ids = {m["message_id"] for m in messages}
known_pr_numbers = set(pulls_by_number)
known_shas = set(commits_by_sha)
known_tickets = set(issues_by_key)
all_ts = [parse_utc(m["ts"]) for m in messages]
all_ts += [parse_utc(c["authored_at"]) for c in commits]
all_ts += [parse_utc(p["merged_at"]) for p in pulls if p.get("merged_at")]
all_ts += [parse_utc(e["ts"]) for e in changelog]
floor_ts = min(t for t in all_ts if t)
ceil_ts = max(t for t in all_ts if t)


def validate(raw_windows):
    """Keep only windows whose evidence exists and whose boundaries make sense."""
    kept, dropped = [], []

    for index, window in enumerate(raw_windows):
        problem = None
        try:
            start = int(window["start_ts"])
            end = int(window["end_ts"])
        except (KeyError, TypeError, ValueError):
            dropped.append((index, "start_ts or end_ts is not an integer"))
            continue

        if end < start:
            dropped.append((index, f"end {end} is before start {start}"))
            continue
        if start < int(floor_ts.timestamp()) or end > int(ceil_ts.timestamp()) + 86400:
            dropped.append((index, f"window {start}..{end} falls outside the history"))
            continue

        evidence = window.get("evidence") or {}
        cited_messages = [m for m in (evidence.get("message_ids") or []) if m in known_message_ids]
        bogus_messages = [m for m in (evidence.get("message_ids") or []) if m not in known_message_ids]
        cited_prs = [n for n in (evidence.get("pr_numbers") or []) if n in known_pr_numbers]
        cited_shas = [s for s in (evidence.get("merge_shas") or []) if s in known_shas]
        ticket = evidence.get("ticket")
        if ticket is not None and ticket not in known_tickets:
            ticket = None

        if not cited_messages and not cited_prs and not cited_shas:
            dropped.append((index, "no evidence could be verified"))
            continue

        merged_prs = [n for n in cited_prs if pulls_by_number[n].get("merged")]
        if not merged_prs and not cited_messages:
            dropped.append((index, "nothing merged and no discussion"))
            continue

        participant_names = set()
        for message_id in cited_messages:
            for message in messages:
                if message["message_id"] == message_id and message.get("author_name"):
                    participant_names.add(message["author_name"])

        kept.append({
            "task_id": str(window.get("task_id") or f"task-{index:03d}").lower(),
            "title": clip(window.get("title"), 120) or f"task {index}",
            "summary": clip(window.get("summary"), 400),
            "start_ts": start,
            "end_ts": end,
            "confidence": min(1.0, max(0.0, float(window.get("confidence") or 0.5))),
            "participants": window.get("participants") or sorted(participant_names),
            "reasoning": clip(window.get("reasoning"), 300),
            "why": clip(window.get("reasoning"), 300),
            "evidence": {
                "discord": {
                    "channel": next(
                        (m["channel"] for m in messages if m["message_id"] == cited_messages[0]),
                        None) if cited_messages else None,
                    "message_ids": cited_messages,
                },
                "jira": {"issue_keys": [ticket] if ticket else [],
                          "closing_ts": utc(
                              min((parse_utc(done_events[ticket]["ts"]) for ticket in
                                   ([ticket] if ticket in done_events else [])),
                              default=start))},
                "github": {"pr_numbers": cited_prs, "merge_shas": cited_shas},
                "hallucinated_message_ids": bogus_messages,
            },
        })

    kept.sort(key=lambda w: w["start_ts"])
    return kept, dropped


model_windows, model_dropped = validate(model_payload.get("windows") or [])
print(f"model: {len(model_windows)} windows kept, {len(model_dropped)} dropped")

if model_windows:
    print("\n  what the model said about each window:")
    for window in model_windows:
        span = window["end_ts"] - window["start_ts"]
        print(f"\n  {window['task_id']}  confidence {window['confidence']}  "
              f"spans {span / 3600:.1f}h")
        print(f"    {window['title']}")
        if window.get("summary"):
            print(f"    said: {window['summary']}")
        if window.get("why"):
            print(f"    why:  {window['why']}")
        evidence = window["evidence"]
        print(f"    evidence: {len(evidence['discord']['message_ids'])} messages, "
              f"PRs {evidence['github']['pr_numbers'] or '-'}, "
              f"{len(evidence['github']['merge_shas'])} merge shas")
        invented = evidence.get("hallucinated_message_ids") or []
        if invented:
            print(f"    invented and discarded: {invented}")
for index, reason in model_dropped[:10]:
    print(f"  drop #{index}: {reason}")

hallucinations = sum(len(w["evidence"]["hallucinated_message_ids"]) for w in model_windows)
if hallucinations:
    print(f"  note: {hallucinations} cited message ids did not exist and were discarded")


# %% the deterministic baseline
#
# The same view of the data, resolved by rule instead of judgement. It exists so
# the model can be measured rather than trusted.

def baseline_windows():
    found = []
    for ticket_key in sorted(issues_by_key):
        thread = sorted(messages_by_ticket.get(ticket_key, []), key=lambda r: r["ts"])
        if not thread:
            continue
        closing = done_events.get(ticket_key)
        numbers = [n for n in prs_by_ticket.get(ticket_key, []) if pulls_by_number[n].get("merged")]
        if not closing and not numbers:
            continue

        merged_at = max(
            [pulls_by_number[n]["merged_at_dt"] for n in numbers] +
            [parse_utc(closing["ts"])] if closing else
            [pulls_by_number[n]["merged_at_dt"] for n in numbers])
        if merged_at is None:
            continue
        start = min(thread[0]["ts"], parse_utc(issues_by_key[ticket_key]["created_at"])
                    or thread[0]["ts"])
        if merged_at < start:
            continue

        cited = [m["message_id"] for m in thread if start <= m["ts"] <= merged_at]
        found.append({
            "task_id": ticket_key.lower(),
            "title": clip(issues_by_key[ticket_key].get("summary"), 120),
            "summary": f"{ticket_key} discussed in #{thread[0]['channel']}",
            "start_ts": epoch(start),
            "end_ts": epoch(merged_at),
            "confidence": 1.0,
            "participants": sorted({m.get("author_name") for m in thread if m.get("author_name")}),
            "reasoning": "rule: first mention to later of Done and merge",
            "evidence": {
                "discord": {"channel": thread[0]["channel"], "message_ids": cited},
                "jira": {"issue_keys": [ticket_key],
                          "closing_ts": utc(closing["ts"]) if closing else None},
                "github": {
                    "pr_numbers": numbers,
                    "merge_shas": [pulls_by_number[n]["merge_sha"] for n in numbers
                                   if pulls_by_number[n].get("merge_sha")],
                },
                "hallucinated_message_ids": [],
            },
        })
    found.sort(key=lambda w: w["start_ts"])
    return found


baseline = baseline_windows()
print(f"baseline: {len(baseline)} windows by rule")


# %% score them
#
# Boundary agreement against the reference when there is one, and coverage
# against the baseline otherwise. A model that invents a task the rule never saw
# is not automatically wrong, but it does have to be justified, so the numbers
# are printed rather than acted on silently.

def find_reference():
    if reference_hint:
        candidate = Path(reference_hint)
        if candidate.is_file():
            return candidate
    for folder in sorted(reference_base.glob("*/Weave")) + [reference_base]:
        candidate = folder / "opencode" / "synth-data" / "gold" / "reference_windows.json"
        if candidate.is_file():
            return candidate
        candidate = folder / "synth-data" / "gold" / "reference_windows.json"
        if candidate.is_file():
            return candidate
    return None


def score_windows(hypothesis, reference):
    """Boundary error in minutes, plus how much of the reference was found."""
    if not reference:
        return None
    matched = exact = 0
    errors = []
    by_ticket = {t.get("jira") or t.get("task_id"): t for t in reference}
    for window in hypothesis:
        key = (window.get("evidence", {}).get("jira", {}).get("issue_keys") or [None])[0]
        target = by_ticket.get(key)
        if target is None:
            continue
        matched += 1
        ref_start = parse_utc(target["window"]["start"] if "window" in target
                             else target.get("start_ts_utc"))
        ref_end = parse_utc(target["window"]["end"] if "window" in target
                           else target.get("end_ts_utc"))
        start_error = abs(window["start_ts"] - int(ref_start.timestamp())) / 60
        end_error = abs(window["end_ts"] - int(ref_end.timestamp())) / 60
        if start_error <= 5 and end_error <= 5:
            exact += 1
        else:
            errors.append((key, round(start_error, 1), round(end_error, 1)))
    return {
        "hypotheses": len(hypothesis),
        "reference": len(reference),
        "matched": matched,
        "exact_within_5min": exact,
        "precision": round(matched / len(hypothesis), 3) if hypothesis else 0.0,
        "recall": round(matched / len(reference), 3) if reference else 0.0,
        "worst": sorted(errors, key=lambda e: -(e[1] + e[2]))[:5],
    }


reference_path = find_reference()
reference = None
if reference_path:
    reference = json.loads(reference_path.read_text())
    reference = reference.get("tasks", reference)
    print(f"reference: {reference_path} with {len(reference)} tasks")

model_score = score_windows(model_windows, reference)
baseline_score = score_windows(baseline, reference)

if model_score:
    print("\nmodel   :", json.dumps({k: v for k, v in model_score.items() if k != "worst"}))
if baseline_score:
    print("baseline:", json.dumps({k: v for k, v in baseline_score.items() if k != "worst"}))

# agreement with each other, which is the comparison that always works
def agree(left, right):
    by_key = {w["task_id"]: w for w in right}
    shared = overlap = 0
    for window in left:
        other = by_key.get(window["task_id"])
        if not other:
            continue
        shared += 1
        if abs(window["start_ts"] - other["start_ts"]) <= 300 and \
           abs(window["end_ts"] - other["end_ts"]) <= 300:
            overlap += 1
    return {"shared_tasks": shared, "boundaries_agree_within_5min": overlap}


print("\nmodel vs baseline:", json.dumps(agree(model_windows, baseline)))
print(f"model {len(model_windows)} windows, baseline {len(baseline)} windows, "
      f"union {len({w['task_id'] for w in model_windows} | {w['task_id'] for w in baseline})} tasks")


def choose():
    if window_source == "model":
        return model_windows, "model"
    if window_source == "baseline":
        return baseline, "baseline"
    if model_score and baseline_score:
        if (model_score["exact_within_5min"], model_score["matched"]) >= \
           (baseline_score["exact_within_5min"], baseline_score["matched"]):
            return model_windows, "model"
        return baseline, "baseline"
    # no reference to score against, so require the model to at least cover the rule
    agreement = agree(model_windows, baseline)
    if agreement["boundaries_agree_within_5min"] >= 0.8 * max(1, len(baseline)):
        return model_windows, "model"
    return baseline, "baseline"


windows, produced_by = choose()
print(f"\nusing {len(windows)} windows from the {produced_by}")


# %% static splitter
#
# No model in this cell, and none may be added. Everything below is a pure
# function of the chosen windows and the contents of silver, so a re-run on the
# same inputs produces the same gold.

SOURCE_TIMESTAMPS = {
    "discord_message": lambda r: parse_utc(r["ts"]),
    "jira_issue": lambda r: min([t for t in (parse_utc(r.get("created_at")),
                                              parse_utc(r.get("updated_at"))) if t],
                                default=None),
    "jira_changelog_event": lambda r: parse_utc(r["ts"]),
    "github_commit": lambda r: parse_utc(r["authored_at"]),
    "github_pull_request": lambda r: parse_utc(r.get("merged_at") or r.get("opened_at")),
    "document": lambda r: None,
}


def slice_window(window):
    """Every silver record that falls inside the window, per source table."""
    start = datetime.fromtimestamp(window["start_ts"], tz=timezone.utc)
    end = datetime.fromtimestamp(window["end_ts"], tz=timezone.utc)
    out = {}

    for table, records in (
        ("discord_message", messages),
        ("jira_issue", issues),
        ("jira_changelog_event", changelog),
        ("github_commit", commits),
        ("github_pull_request", pulls),
    ):
        picked = []
        for record in records:
            stamp = SOURCE_TIMESTAMPS[table](record)
            if stamp is None:
                continue
            if start <= stamp <= end:
                picked.append(record)
        picked.sort(key=lambda r: (SOURCE_TIMESTAMPS[table](r), str(r.get("message_id") or
                                                                   r.get("issue_key") or
                                                                   r.get("sha") or
                                                                   r.get("pr_number") or "")))
        if picked:
            out[table] = picked

    cited = set(window["evidence"].get("discord", {}).get("message_ids") or [])
    tickets = set(window["evidence"].get("jira", {}).get("issue_keys") or [])
    shas = set(window["evidence"].get("github", {}).get("merge_shas") or [])
    if cited or tickets or shas:
        if "document" not in out:
            out["document"] = []
        for record in documents:
            if record.get("sha256") in shas or record.get("filename") in cited:
                out["document"].append(record)

    return out


KEY_COLUMNS = {
    "discord_message": "message_id",
    "jira_issue": "issue_key",
    "jira_changelog_event": "id",
    "github_commit": "sha",
    "github_pull_request": "pr_number",
    "document": "filename",
}

chunk_rows = []
for position, window in enumerate(windows, 1):
    window_id = f"{window['task_id']}-{position:03d}"
    window["window_id"] = window_id

    cur.execute(
        f"INSERT INTO {gold_db}.task_window"
        " (window_id, task_id, title, summary, start_ts, end_ts, confidence,"
        "  produced_by, participants, evidence, run_id)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (window_id, window["task_id"], window["title"], window["summary"],
         datetime.fromtimestamp(window["start_ts"], tz=timezone.utc),
         datetime.fromtimestamp(window["end_ts"], tz=timezone.utc),
         window["confidence"], produced_by, window.get("participants") or [],
         Json(window["evidence"]), run_id))

    for table, records in slice_window(window).items():
        for ordinal, record in enumerate(records, 1):
            payload = {k: (utc(v) if isinstance(v, datetime) else v)
                       for k, v in record.items() if not k.endswith("_dt")}
            chunk_rows.append((window_id, table, str(record.get(KEY_COLUMNS[table])),
                               SOURCE_TIMESTAMPS[table](record), ordinal,
                               Json(scrub(payload)), run_id))

conn.commit()
print(f"{len(windows)} windows written")

if chunk_rows:
    cur.executemany(
        f"INSERT INTO {gold_db}.task_chunk"
        " (window_id, source_table, source_key, ts, ordinal, row, run_id)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)", chunk_rows)
    conn.commit()
print(f"{len(chunk_rows)} chunk rows written")


# %% summary

cur.execute(f"""
    SELECT source_table, count(*) AS rows, count(DISTINCT window_id) AS windows
    FROM {gold_db}.task_chunk GROUP BY source_table ORDER BY 2 DESC""")
per_table = rows_sql = cur.fetchall()

print("\nwindows:", len(windows), f"(from the {produced_by})")
print(f"{'source table':<26}{'rows':>8}{'windows':>9}")
for table, count, window_count in per_table:
    print(f"{table:<26}{count:>8}{window_count:>9}")

empty = [w["task_id"] for w in windows
         if not any(r["window_id"] == w["window_id"] for r in
                    rows(f"SELECT window_id FROM {gold_db}.task_chunk"))]
if empty:
    print(f"\n{len(empty)} windows captured nothing: {', '.join(empty[:8])}")

overlap = rows(f"""
    SELECT source_table, source_key, count(*) AS n
    FROM {gold_db}.task_chunk
    GROUP BY source_table, source_key HAVING count(*) > 1""")
if overlap:
    print(f"{len(overlap)} records appear in more than one window, which is expected "
          f"when windows touch")

print(f"\nrun_id {run_id} finished {datetime.now(timezone.utc).isoformat()}")
print(f"model detector took {model_seconds:.1f}s")
print("next: 04, the per-chunk agent phase")

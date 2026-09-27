"""Recon: what is actually in this environment, and what shape is the data.

Run this before the two pipeline notebooks. It reports the runtime, the
packages, which dbutils APIs really exist, whether the Lakebase connection
works, and the shape of every table with row counts. Nothing here writes to the
database, so it is safe to run on a fresh cluster.
"""

# %% [markdown]
# ## 0. Recon
#
# Read-only. Reports the runtime, packages, the real `dbutils` API surface, the
# Lakebase connection, and the shape and size of every table.

# %%

import json
import os
import platform
import sys
import tempfile

print("=" * 70)
print("RUNTIME")
print("=" * 70)
print(f"python           {sys.version.split()[0]}  ({platform.python_implementation()})")
print(f"platform         {platform.platform()}")
for variable in ("DATABRICKS_RUNTIME_VERSION", "DBR_VERSION", "DATABRICKS_WORKSPACE_URL"):
    print(f"{variable:<17} {os.environ.get(variable, '(unset)')}")

try:
    import pyspark
    print(f"pyspark          {pyspark.__version__}")
except ImportError:
    print("pyspark          (absent)")

try:
    from IPython import get_ipython
    shell = get_ipython()
    if shell is not None:
        version = shell.user_ns.get("DATABRICKS_RUNTIME_VERSION")
        if version:
            print(f"runtime (ns)     {version}")
except Exception as error:
    print(f"runtime (ns)     unavailable: {type(error).__name__}")

print()
print("=" * 70)
print("PACKAGES")
print("=" * 70)
try:
    from importlib.metadata import distributions
    wanted = {}
    for dist in distributions():
        name = (dist.metadata["Name"] or "").lower()
        if name in ("psycopg", "psycopg-binary", "databricks-sdk", "databricks-connect",
                    "pyarrow", "pandas", "numpy", "delta-spark", "requests"):
            wanted[name] = dist.version
    for name in sorted(wanted):
        print(f"  {name:<24} {wanted[name]}")
    for name in ("psycopg", "databricks-sdk"):
        if name not in wanted:
            print(f"  {name:<24} (absent) - the pipeline installs it on first run")
except Exception as error:
    print(f"could not list packages: {type(error).__name__}: {error}")

print()
print("=" * 70)
print("DBUTILS API SURFACE")
print("=" * 70)
print("Every attribute below was checked with hasattr, not assumed.")
for name in ("widgets", "fs", "notebook", "library", "secrets", "displayHTML", "dbutils"):
    print(f"  dbutils.{name:<12} {'yes' if hasattr(dbutils, name) else 'NO'}")

print("\n  dbutils.notebook() members:")
try:
    notebook = dbutils.notebook()
    members = [m for m in dir(notebook) if not m.startswith("_")]
    print(f"    {', '.join(members)}")
    for attribute in ("context", "path", "notebook_id"):
        print(f"    notebook().{attribute:<12} "
              f"{'yes' if hasattr(notebook, attribute) else 'NO'}")
    print("    note: notebook().context is not public API - do not use it for the user name")
except Exception as error:
    print(f"    dbutils.notebook() failed: {type(error).__name__}: {error}")

print("\n  dbutils.fs members and real signatures:")
try:
    import inspect
    print(f"    {', '.join(m for m in dir(dbutils.fs) if not m.startswith('_'))}")
    for name in ("cp", "head", "mkdirs", "ls", "rm", "put"):
        member = getattr(dbutils.fs, name, None)
        try:
            print(f"      {name}{inspect.signature(member)}")
        except (TypeError, ValueError) as error:
            print(f"      {name}: signature unavailable ({type(error).__name__})")
    print("    note: cp takes (from, to, recurse) - there is no overwrite argument")
    print("    note: head returns the file bytes as a str, not an object with .size")
except Exception as error:
    print(f"    unavailable: {type(error).__name__}")

print("\n  writable folder check:")
for candidate in (os.getcwd(), "/Workspace", tempfile.gettempdir()):
    try:
        os.makedirs(candidate, exist_ok=True)
        probe = os.path.join(candidate, ".recon_probe")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe)
        print(f"    {candidate:<44} writable")
    except Exception as error:
        print(f"    {candidate:<44} {type(error).__name__}")

print("\n  dbutils.library members:")
try:
    print(f"    {', '.join(m for m in dir(dbutils.library) if not m.startswith('_'))}")
except Exception as error:
    print(f"    unavailable: {type(error).__name__}")

print()
print("=" * 70)
print("WORKSPACE LAYOUT")
print("=" * 70)


def walk_dirs(parent, max_depth=4):
    level = [parent]
    for _ in range(max_depth):
        level = [child for folder in level for child in sorted(folder.iterdir())
                 if child.is_dir()]
        yield from level


from pathlib import Path

for base in ("/Workspace/Users", "/Workspace", "/Volumes/main/default"):
    root = Path(base)
    if not root.is_dir():
        print(f"  {base}: not present")
        continue
    for candidate in sorted(root.glob("*/Weave")) + [root / "Weave"]:
        if not candidate.is_dir():
            continue
        print(f"  found {candidate}")
        for stream in ("discord", "git", "jira"):
            stream_root = candidate / stream
            if not stream_root.is_dir():
                continue
            files = [str(f.relative_to(candidate)) for f in stream_root.rglob("*.json")]
            attachments = [f for f in stream_root.rglob("*")
                           if f.is_file() and "attachments" in f.parts]
            print(f"    {stream:<9} {len(files)} json, {len(attachments)} attachment files")
            for name in files[:4]:
                print(f"                {name}")

print()
print("=" * 70)
print("LAKEBASE CONNECTION")
print("=" * 70)
print("The DSN is read from secrets/tribal/lakebase, or from the notebooks.")

SECRET_SCOPE = "tribal"
SECRET_KEY = "lakebase"
LAKEBASE_HOST = "ep-twilight-bonus-d8outdz9.database.us-east-2.cloud.databricks.com"
LAKEBASE_DSN = ("postgresql://my_app_role:Nn6jLu98ceTBqsBTT69i-xYo@"
               f"{LAKEBASE_HOST}:5432/databricks_postgres?sslmode=require")

try:
    import psycopg
    from psycopg.types.json import Json
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "psycopg[binary]"])
    import psycopg
    from psycopg.types.json import Json

from databricks.sdk import WorkspaceClient

dsn = None
try:
    secret = WorkspaceClient().secrets.get_secret(SECRET_SCOPE, SECRET_KEY)
    dsn = (getattr(secret, "value", None) or secret.value_or_raise()).strip()
    print(f"secret           {SECRET_SCOPE}/{SECRET_KEY}")
except Exception as error:
    print(f"secret           unavailable ({type(error).__name__})")
if not dsn:
    dsn = os.environ.get("LAKEBASE_DSN", "").strip() or LAKEBASE_DSN.strip()
    print("secret           not used, falling back to the DSN in this notebook")

if not dsn:
    raise SystemExit("No DSN available, cannot check the database.")

# The hostname in the notebooks is a guess until the platform confirms it.
# Lakebase project endpoints look like ep-abc-123.databricks.com, so ask the
# workspace what the real endpoint is and compare.
print()
print("  endpoint discovery:")
instance_name = os.environ.get("LAKEBASE_INSTANCE", "meridian-tribal")
try:
    from databricks.sdk import WorkspaceClient
    workspace = WorkspaceClient()
    print(f"    authenticated as  {workspace.current_user.me().user_name}")
    print(f"    workspace host    {workspace.config.host}")
    try:
        found = list(workspace.database.list_database_instances())
    except Exception as error:
        found = []
        print(f"    list_database_instances unavailable ({type(error).__name__}: {error})")

    print(f"    instances visible  {len(found)}")
    if not found:
        print("    -> this account can see no Lakebase instance, so there is no")
        print("       endpoint to connect to. Create one in the Lakebase app and")
        print("       enable 'PG native login' for password auth, or paste the")
        print("       connection string into the dsn widget in notebook 01.")
    for database_instance in found:
        print(f"      name={database_instance.name!r}")
        for attribute in ("state", "pg_version", "read_write_dns", "read_only_dns",
                          "enable_pg_native_login", "effective_enable_pg_native_login"):
            value = getattr(database_instance, attribute, None)
            if value is not None:
                print(f"        {attribute:<32} {value}")

    try:
        catalogs = list(workspace.database.list_database_catalogs())
    except Exception as error:
        catalogs = []
        print(f"    list_database_catalogs unavailable ({type(error).__name__}: {error})")
    print(f"    catalogs visible   {len(catalogs)}")
    for catalog in catalogs:
        print(f"      catalog={catalog.name!r}")
        for attribute in ("database_instance", "database_name", "database_branch_id",
                          "database_project_id", "uid"):
            value = getattr(catalog, attribute, None)
            if value is not None:
                print(f"        {attribute:<32} {value}")
        try:
            detail = workspace.database.get_database_catalog(name=catalog.name)
            for attribute in ("database_instance", "database_name", "database_project_id"):
                value = getattr(detail, attribute, None)
                if value is not None:
                    print(f"        detail.{attribute:<24} {value}")
        except Exception as error:
            print(f"        detail unavailable ({type(error).__name__})")

    for label, call in (
        ("get_database_instance", lambda: workspace.database.get_database_instance(name=instance_name)),
    ):
        try:
            found = call()
            host = (getattr(found, "read_write_dns", None)
                    or getattr(found, "read_write_dsn", None)
                    or getattr(found, "default_endpoint", None))
            print(f"    {label:<22} {host}")
        except Exception as error:
            print(f"    {label:<22} unavailable ({type(error).__name__})")
    database_api = [m for m in dir(workspace.database) if not m.startswith("_")]
    print(f"    database API      {', '.join(database_api)}")
except Exception as error:
    print(f"    could not query the workspace: {type(error).__name__}: {error}")

print(f"\n  DSN host in the notebooks: {dsn.split('@')[-1] if '@' in dsn else '(none)'}")
print("  if that is a dbc-...cloud.databricks.com host but the endpoint above is")
print("  ep-...databricks.com, the notebooks need the real endpoint.")

try:
    conn = psycopg.connect(dsn, autocommit=True, connect_timeout=30)
    cur = conn.cursor()
    cur.execute("select current_database(), current_user, version()")
    database, user, version = cur.fetchone()
    print(f"connected        db={database} user={user}")
    print(f"server           {version.split(',')[0]}")
except Exception as error:
    raise SystemExit(f"Could not connect: {type(error).__name__}: {error}")

# the two-argument form takes a database name, not a role, so ask about the
# role explicitly
cur.execute("select has_database_privilege(current_user, current_database(), 'CREATE'),"
            " has_schema_privilege('public', 'CREATE')")
can_create_db, can_create_public = cur.fetchone()
print(f"privileges       CREATE on database={can_create_db}  CREATE on public={can_create_public}")

# %% table shapes
#
# Every table, its columns, and how many rows it holds. Empty tables are called
# out, because a schema that exists but was never loaded is the failure this
# pipeline is most prone to.

print()
print("=" * 70)
print("TABLES AND SHAPES")
print("=" * 70)

cur.execute("""
  SELECT table_schema, table_name
  FROM information_schema.tables
  WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
  ORDER BY table_schema, table_name
""")
tables = cur.fetchall()
if not tables:
    print("  no user tables yet - run 01_three_dumps_to_lakebase first")

for schema, table in tables:
    cur.execute("""
      SELECT column_name, data_type, is_nullable, column_default
      FROM information_schema.columns
      WHERE table_schema = %s AND table_name = %s
      ORDER BY ordinal_position
    """, (schema, table))
    columns = cur.fetchall()

    try:
        cur.execute(f'SELECT count(*) FROM "{schema}"."{table}"')
        rows = cur.fetchone()[0]
    except Exception as error:
        rows = f"error: {type(error).__name__}"

    print(f"\n  {schema}.{table}   rows={rows}   columns={len(columns)}")
    for name, data_type, nullable, default in columns:
        flag = "null" if nullable == "YES" else "not null"
        extra = f" default {default}" if default else ""
        print(f"    {name:<26} {data_type:<28} {flag}{extra}")

# %% bronze against silver
#
# The two counts that matter: raw rows in, clean rows out.

print()
print("=" * 70)
print("PIPELINE COUNTS")
print("=" * 70)

expected = [
    ("bronze", "discord_message"), ("bronze", "discord_channel"),
    ("bronze", "jira_issue"), ("bronze", "jira_changelog_history"),
    ("bronze", "github_commit"), ("bronze", "github_pull_request"),
    ("bronze", "github_event"), ("bronze", "document"),
    ("silver", "discord_message"), ("silver", "jira_issue"),
    ("silver", "jira_changelog_event"), ("silver", "github_commit"),
    ("silver", "github_pull_request"), ("silver", "document"),
    ("silver", "entity_link"), ("silver", "quarantine"),
]

present = {(schema, table) for schema, table in tables}
for schema, table in expected:
    if (schema, table) not in present:
        print(f"  {schema}.{table:<24} missing")
        continue
    try:
        cur.execute(f'SELECT count(*) FROM "{schema}"."{table}"')
        count = cur.fetchone()[0]
    except Exception as error:
        count = f"error: {type(error).__name__}"
    if isinstance(count, int) and count == 0:
        note = "  <-- exists but empty"
    else:
        note = ""
    print(f"  {schema}.{table:<24} {count}{note}")

conn.close()
print("\nrecon complete")

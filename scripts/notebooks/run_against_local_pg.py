#!/usr/bin/env python3
"""Run the two notebooks as plain Python against a real local Postgres.

Databricks supplies dbutils and the DSN. Locally we stub dbutils and point the
DSN at a throwaway cluster, so the actual SQL, JSON handling, cleaning rules and
row counts are all exercised for real.

Usage: python3 run_against_local_pg.py [DSN]
"""
import os
import pathlib
import re
import shutil
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent
DSN = sys.argv[1] if len(sys.argv) > 1 else "postgresql://postgres@127.0.0.1:55432/tribal"
DATA_ROOT = "/tmp/ws/Weave"

WIDGETS = {"data_dir": DATA_ROOT, "instance": "meridian-tribal", "reuse": "0", "strict": "0",
           "bronze_db": "bronze", "silver_db": "silver", "upload_dir": "", "dsn": ""}


def make_dbutils():
    dbutils = types.ModuleType("dbutils")
    dbutils.widgets = types.SimpleNamespace(
        text=lambda *a, **k: None, get=lambda n="": WIDGETS.get(n, ""), getAll=lambda: [])
    dbutils.fs = types.SimpleNamespace(
        mkdirs=lambda p: os.makedirs(p, exist_ok=True),
        cp=lambda src, dst, **k: (
            os.makedirs(os.path.dirname(dst), exist_ok=True),
            shutil.copyfile(src.removeprefix("file://"), dst)),
        head=lambda p: types.SimpleNamespace(
            size=pathlib.Path(p).stat().st_size if pathlib.Path(p).exists() else 0))
    dbutils.secrets = types.SimpleNamespace(
        get=lambda **k: (_ for _ in ()).throw(Exception("no secret")))
    dbutils.library = types.SimpleNamespace(restartPython=lambda: None)
    return dbutils


LOCAL_WS = "/tmp/localdbfs"


def make_local_workspace():
    """Stand in for the DBFS root; "/" is read-only on macOS."""
    root = pathlib.Path(LOCAL_WS) / "Users" / "local" / "_tribal_attachments"
    root.mkdir(parents=True, exist_ok=True)
    return root


def run(filename):
    src = (HERE / filename).read_text()
    src = src.replace("/Workspace/Users/", f"{LOCAL_WS}/Users/")
    # the notebooks resolve their own DSN; hand them the local cluster instead
    os.environ["LAKEBASE_DSN"] = DSN
    make_local_workspace()
    dbutils = make_dbutils()
    sys.modules["dbutils"] = dbutils
    scope = {"__name__": "__main__", "dbutils": dbutils}
    print(f"\n{'=' * 60}\n{filename}\n{'=' * 60}")
    exec(compile(src, filename, "exec"), scope)
    return scope


if __name__ == "__main__":
    import psycopg
    one = run("01_three_dumps_to_lakebase.py")
    two = run("02_silver_clean.py")

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        for schema in ("bronze", "silver", "ingest"):
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s ORDER BY table_name", (schema,))
            tables = [r[0] for r in cur.fetchall()]
            print(f"\n{schema}:")
            for t in tables:
                cur.execute(f'SELECT count(*) FROM {schema}."{t}"')
                print(f"  {t:<26} {cur.fetchone()[0]}")

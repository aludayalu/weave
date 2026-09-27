#!/usr/bin/env python3
"""Load the dataset into Lakebase FROM INSIDE the Databricks workspace.

This is the fallback for when the instance has no public access: run it in a
Databricks notebook and the connection is made from inside the workspace, so
no IP allowlisting is involved.

Usage
  # in a Databricks notebook (or `databricks notebook run`), with the
  # synth-data folder uploaded to the workspace:
  python3 load_in_workspace.py --root /Workspace/Users/you@x.com/synth-data
"""
import argparse
import importlib
import os
import pathlib
import subprocess
import sys


def ensure_driver():
    try:
        import psycopg
        return psycopg.__version__
    except ImportError:
        pass
    print("installing psycopg ...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                    "psycopg[binary]"], check=True)
    import psycopg
    return psycopg.__version__


def find_root(explicit):
    if explicit:
        p = pathlib.Path(explicit)
        if p.is_dir():
            return p
        raise SystemExit(f"--root {p} is not a directory")
    if os.environ.get("SYNTH_DATA_ROOT"):
        p = pathlib.Path(os.environ["SYNTH_DATA_ROOT"])
        if p.is_dir():
            return p
    for base in (pathlib.Path.cwd(), pathlib.Path("/Workspace/Users"),
                 pathlib.Path("/Workspace")):
        if not base.is_dir():
            continue
        for cand in base.rglob("synth-data"):
            if (cand / "lakebase" / "load_lakebase.py").exists():
                return cand
    raise SystemExit("synth-data folder not found — upload it to the workspace first")


def resolve_dsn():
    try:
        from databricks.sdk import WorkspaceClient
        w = WorkspaceClient()
        inst = w.database.get_database_instance(name=os.environ.get(
            "LAKEBASE_INSTANCE", "meridian-tribal"))
        print(f"connected via SDK to {inst.name} (state: {inst.state})")
        return inst.read_write_dsn
    except Exception as e:
        print(f"SDK path unavailable: {type(e).__name__}: {e}")
    dsn = os.environ.get("LAKEBASE_DSN")
    if dsn:
        print("using LAKEBASE_DSN from the environment")
        return dsn
    raise SystemExit(
        "No DSN. Either grant this notebook access to the Lakebase instance, or\n"
        "set LAKEBASE_DSN as a notebook secret to the native-role DSN.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None, help="path to the uploaded synth-data folder")
    ap.add_argument("--instance", default="meridian-tribal")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("psycopg", ensure_driver())
    root = find_root(args.root)
    lb = root / "lakebase"
    os.environ["SYNTH_DATA_ROOT"] = str(root)
    os.environ.setdefault("LAKEBASE_INSTANCE", args.instance)
    print("dataset root:", root)

    sys.path.insert(0, str(lb))
    import load_lakebase
    importlib.reload(load_lakebase)
    load_lakebase.SYN = root

    if args.dry_run:
        sys.argv = ["load_lakebase", "--dry-run", "--stages", "bronze,silver,detected,gold"]
        return load_lakebase.main()

    import psycopg
    dsn = resolve_dsn()

    with psycopg.connect(dsn, connect_timeout=30, autocommit=True) as c, c.cursor() as cur:
        cur.execute("select current_database(), current_user, version()")
        db, usr, ver = cur.fetchone()
        print(f"db={db}  user={usr}\n{ver.split(',')[0]}")

    print("applying schema.sql (idempotent) ...")
    with psycopg.connect(dsn, connect_timeout=30) as c, c.cursor() as cur:
        cur.execute((lb / "schema.sql").read_text())

    sys.argv = ["load_lakebase", "--dsn", dsn]
    rc = load_lakebase.main()
    if rc:
        return rc

    print("\nrow counts")
    with psycopg.connect(dsn, connect_timeout=30) as c, c.cursor() as cur:
        cur.execute("""select table_schema, table_name from information_schema.tables
                        where table_schema in ('bronze','silver','detected','gold','ingest','ops')
                          and table_type='BASE TABLE' order by 1,2""")
        tables = cur.fetchall()
        for s, t in tables:
            cur.execute(f'select count(*) from "{s}"."{t}"')
            print(f"  {s + '.' + t:<40} {cur.fetchone()[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

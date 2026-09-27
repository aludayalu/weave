#!/usr/bin/env python3
"""Run the notebooks locally against an in-memory stand-in for Postgres.

The notebooks talk to Databricks (dbutils, WorkspaceClient) and to Postgres.
This substitutes both: a recording in-memory store that understands the
INSERT/SELECT the notebooks use, so the code path is genuinely exercised --
including reading rows back, which is how notebook 02 consumes bronze.

This is a real test, not a smoke test: it caught a NameError that a syntax
check could not see.

Usage: python3 run_notebook_local.py [01|02|both]
"""
import json
import os
import pathlib
import re
import shutil
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent
FAKE_ROOT = pathlib.Path("/tmp/notebook_test/Weave")


# --------------------------------------------------------------- fake store
class Store:
    def __init__(self):
        self.tables = {}
        self.schema = {}

    def create(self, table, body):
        cols = []
        for part in body.split(","):
            part = part.strip()
            if not part:
                continue
            name = part.split()[0]
            if name.upper() in ("PRIMARY", "UNIQUE", "CONSTRAINT", "FOREIGN", "CHECK"):
                continue
            cols.append(name.strip('"'))
        if cols:
            self.schema[table] = cols

    def insert(self, table, cols, params):
        if params is None:
            return
        # execute() supplies exactly one row; executemany() supplies a batch
        if isinstance(params, dict) or isinstance(params, tuple):
            params = [params]
        table = self.tables.setdefault(table, [])
        for row in params:
            if isinstance(row, dict):
                table.append(row)
                continue
            if len(row) == 1 and isinstance(row[0], (list, tuple)):
                row = row[0]
            table.append(dict(zip(cols, row)) if cols else {"v": row})

    def select(self, table):
        return self.tables.get(table, [])


STORE = Store()

INSERT = re.compile(r"INSERT INTO ([\w.]+)(?:\s*\((\w+(?:\s*,\s*\w+)*)\))?\s*VALUES", re.I)
SELECT = re.compile(r"SELECT (.+?) FROM ([\w.]+)", re.I | re.S)
TRUNCATE = re.compile(r"TRUNCATE ([\w.]+)", re.I)
CREATE = re.compile(r"CREATE TABLE (?:IF NOT EXISTS )?([\w.]+)\s*\((.*?)\)\s*;", re.I | re.S)


class FakeCursor:
    def __init__(self, log, store):
        self.log, self.store, self.rows = log, store, []

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self._ = self
        created = list(CREATE.finditer(flat))
        if created:
            for m in created:
                self.store.create(m.group(1), m.group(2))
            self.rows = []
        elif m := INSERT.search(flat):
            table, cols = m.group(1), m.group(2)
            if cols:
                cols = [c.strip() for c in cols.split(",")]
            else:
                cols = self.store.schema.get(table)
            self.store.insert(table, cols, params)
            self.rows = []
        elif m := TRUNCATE.search(flat):
            self.store.tables.pop(m.group(1), None)
            self.rows = []
        elif "current_database()" in flat:
            self.rows = [("databricks_postgres", "my_app_role", "PostgreSQL 17.5 on x86_64")]
        elif m := SELECT.search(flat):
            wanted, table = m.group(1), m.group(2)
            rows = self.store.select(table)
            cols = [c.strip() for c in wanted.split(",")] if wanted != "*" else None
            if "count(*)" in wanted.lower():
                self.rows = [(len(rows),)]
            else:
                out = []
                for r in rows:
                    if cols and len(cols) > 1:
                        out.append(tuple(r.get(c) for c in cols))
                    else:
                        out.append(tuple(r.values()) if len(r) > 1 else (r.get("v"),))
                self.rows = out
        else:
            self.rows = []
        return self

    def executemany(self, sql, seq):
        m = INSERT.search(" ".join(sql.split()))
        if m:
            table, cols = m.group(1), m.group(2)
            self.store.insert(table, [c.strip() for c in cols.split(",")] if cols else [], list(seq))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else (0,)

    def fetchall(self):
        return self.rows.pop(0) if self.rows else []

    def close(self):
        pass

    def __getattr__(self, name):
        return lambda *a, **k: None


class FakeConn:
    def cursor(self):
        return FakeCursor([], STORE)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


# --------------------------------------------------------------- databricks
def install_stubs():
    dbutils = types.ModuleType("dbutils")
    dbutils.widgets = types.SimpleNamespace(
        text=lambda *a, **k: None,
        get=lambda name="": {"data_dir": str(FAKE_ROOT), "instance": "meridian-tribal",
                             "strict": "0", "reuse": "0", "bronze_db": "bronze",
                             "silver_db": "silver", "upload_dir": "", "dsn": ""}.get(name, ""),
        getAll=lambda: [])
    dbutils.fs = types.SimpleNamespace(
        mkdirs=lambda p: None, cp=lambda *a, **k: None,
        head=lambda p: types.SimpleNamespace(
            size=pathlib.Path(p).stat().st_size if pathlib.Path(p).exists() else 0))
    dbutils.notebook = lambda: types.SimpleNamespace(
        context=types.SimpleNamespace(userName="tester@example.com"))
    dbutils.library = types.SimpleNamespace(restartPython=lambda: None)
    sys.modules["dbutils"] = dbutils

    psycopg = types.ModuleType("psycopg")
    psycopg.connect = lambda dsn, **k: FakeConn()
    errors = types.ModuleType("psycopg.errors")
    errors.InsufficientPrivilege = type("InsufficientPrivilege", (Exception,), {})
    psycopg.errors = errors
    types_json = types.ModuleType("psycopg.types.json")
    types_json.Json = lambda v: v
    types_pkg = types.ModuleType("psycopg.types")
    types_pkg.json = types_json
    sys.modules.update({"psycopg": psycopg, "psycopg.errors": errors,
                        "psycopg.types": types_pkg, "psycopg.types.json": types_json})

    sdk = types.ModuleType("databricks.sdk")

    class WorkspaceClient:
        def __init__(self, *a, **k):
            self.database = types.SimpleNamespace(
                get_database_instance=lambda name: types.SimpleNamespace(read_write_dsn="stub"),
                get_database_project=lambda name: types.SimpleNamespace(read_write_dsn="stub"))
            self.secrets = types.SimpleNamespace(
                get_secret=lambda s, k: (_ for _ in ()).throw(Exception("no secret")),
                create_scope=lambda *a, **k: None, put_secret=lambda *a, **k: None)
    sdk.WorkspaceClient = WorkspaceClient
    pkg = types.ModuleType("databricks")
    pkg.sdk = sdk
    sys.modules.update({"databricks": pkg, "databricks.sdk": sdk})


# --------------------------------------------------------------- fixture
def build_fixture():
    if FAKE_ROOT.exists():
        shutil.rmtree(FAKE_ROOT)
    for name in ("discord/discord", "discord/attachments/discord", "git",
                 "jira/jira", "jira/attachments/jira"):
        (FAKE_ROOT / name).mkdir(parents=True, exist_ok=True)
    src = pathlib.Path("/Users/aludayalu/weave/opencode/synth-data/raw")
    for f in (src / "discord").glob("*.json"):
        shutil.copy(f, FAKE_ROOT / "discord" / "discord" / f.name)
    for f in (src / "github").glob("*.json"):
        shutil.copy(f, FAKE_ROOT / "git" / f.name)
    for f in (src / "jira").glob("*.json"):
        shutil.copy(f, FAKE_ROOT / "jira" / "jira" / f.name)
    for tree in ("discord", "jira"):
        s = src / "attachments" / tree
        if not s.is_dir():
            continue
        for f in s.rglob("*"):
            if f.is_file():
                d = FAKE_ROOT / tree / "attachments" / tree / f.relative_to(s)
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(f, d)


def run(notebook):
    nb = json.loads((HERE / notebook).read_text())
    scope = {"__name__": "__main__", "dbutils": sys.modules["dbutils"]}
    print(f"\n=== {notebook} ({len(nb['cells'])} cells)")
    for i, cell in enumerate(nb["cells"]):
        if cell["cell_type"] != "code":
            continue
        src = "".join(cell["source"])
        if not src.strip() or src.strip().startswith("%"):
            continue
        try:
            exec(compile(src, f"cell{i}", "exec"), scope)
        except Exception as e:
            print(f"  FAILED cell {i}: {type(e).__name__}: {e}")
            for line in src.splitlines()[:14]:
                print("   |", line)
            return False
        print(f"  cell {i:>2} ok")
    return True


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "both"
    build_fixture()
    install_stubs()
    results = []
    if target in ("both", "01"):
        results.append(run("01_three_dumps_to_lakebase.ipynb"))
    if target in ("both", "02"):
        results.append(run("02_silver_clean.ipynb"))
    print("\nbronze rows landed:", {t: len(r) for t, r in sorted(STORE.tables.items())})
    print("RESULT:", "PASS" if all(results) else "FAIL")
    sys.exit(0 if all(results) else 1)

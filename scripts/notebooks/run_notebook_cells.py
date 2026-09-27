#!/usr/bin/env python3
"""Run the built .ipynb cell by cell, the way Databricks does.

Running the .py as a single unit hides whole classes of bug: in a notebook each
cell is executed separately against one shared namespace, so a name that is
only defined by a cell the builder accidentally dropped shows up as a NameError
on the cluster and nowhere else. This executes the real artifact.

Usage: python3 run_notebook_cells.py [01|02|both]
"""
import json
import os
import pathlib
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent
DSN = "postgresql://postgres@127.0.0.1:55432/tribal"
os.environ["LAKEBASE_DSN"] = DSN

WIDGETS = {"data_dir": "/tmp/ws/Weave", "instance": "meridian-tribal", "reuse": "0",
           "strict": "0", "bronze_db": "bronze", "silver_db": "silver", "dsn": ""}


def make_dbutils():
    dbutils = types.ModuleType("dbutils")
    dbutils.widgets = types.SimpleNamespace(
        text=lambda *a, **k: None, get=lambda n="": WIDGETS.get(n, ""), getAll=lambda: [])
    dbutils.fs = types.SimpleNamespace()
    dbutils.secrets = types.SimpleNamespace(
        get=lambda **k: (_ for _ in ()).throw(Exception("no secret")))
    dbutils.notebook = lambda: types.SimpleNamespace()
    dbutils.library = types.SimpleNamespace(restartPython=lambda: None)
    return dbutils


def run(name):
    notebook = json.loads((HERE / name).read_text())
    dbutils = make_dbutils()
    sys.modules["dbutils"] = dbutils
    scope = {"__name__": "__main__", "dbutils": dbutils}
    print(f"\n=== {name}: {len(notebook['cells'])} cells")
    executed = 0
    for index, cell in enumerate(notebook["cells"]):
        if cell["cell_type"] != "code":
            continue
        source = "".join(cell["source"])
        if not source.strip():
            continue
        try:
            exec(compile(source, f"{name}:cell{index}", "exec"), scope)
        except Exception as error:
            print(f"  FAILED cell {index}: {type(error).__name__}: {error}")
            print("  cell starts:")
            for line in source.splitlines()[:8]:
                print("   |", line)
            return False, executed
        executed += 1
    print(f"  all {executed} code cells ran")
    return True, executed


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "both"
    results = []
    if target in ("both", "00"):
        results.append(run("00_recon.ipynb")[0])
    if target in ("both", "01"):
        results.append(run("01_three_dumps_to_lakebase.ipynb")[0])
    if target in ("both", "02"):
        results.append(run("02_silver_clean.ipynb")[0])
    print("\nRESULT:", "PASS" if all(results) else "FAIL")
    sys.exit(0 if all(results) else 1)

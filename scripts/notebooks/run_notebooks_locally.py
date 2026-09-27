#!/usr/bin/env python3
"""Run 01 and 02 locally against upload/*.zip with a dbutils shim."""
import pathlib, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _dbutils_shim as shim
shim.install()
which = sys.argv[1] if len(sys.argv) > 1 else "both"
if which in ("both", "01"):
    shim.run(HERE / "01_ingest_bronze.py", "01_ingest_bronze")
if which in ("both", "02"):
    shim.run(HERE / "02_bronze_to_silver.py", "02_bronze_to_silver")

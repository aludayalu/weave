"""Minimal dbutils stand-in so the notebooks can be tested outside Databricks.

Local Spark has no Unity Catalog, no widgets and no dbutils.fs, so this shim
provides just enough of each for the two notebooks to run unmodified:

    python3 run_notebooks_locally.py            # both, against upload/*.zip
"""
import os
import pathlib
import shutil
import sys
import types
from datetime import datetime

LOCAL_UPLOAD = "/Users/aludayalu/weave/upload"
LOCAL_VOLUMES = "/tmp/lake_volumes"


# --------------------------------------------------------------- dbutils.fs
def _ls(path):
    p = pathlib.Path(path)
    if p.is_dir():
        return [_Entry(str(f)) for f in sorted(p.iterdir()) if f.is_file()]
    return []


def _cp(src, dst, overwrite=False):
    # Databricks accepts file:// URIs; local shim needs real paths
    if isinstance(src, str) and src.startswith("file://"):
        src = src[len("file://"):]
    s, d = pathlib.Path(src), pathlib.Path(dst)
    if s.is_dir():
        shutil.copytree(s, d, dirs_exist_ok=True)
        return
    if d.is_dir():
        d = d / s.name
    d.parent.mkdir(parents=True, exist_ok=True)
    if d.exists() and not overwrite:
        d.unlink()
    shutil.copy2(s, d)


def _mkdirs(path):
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)


def _head(path):
    p = pathlib.Path(path)
    return types.SimpleNamespace(path=str(p), size=p.stat().st_size)


class _Entry:
    def __init__(self, path):
        self.path = str(path)
        self.name = os.path.basename(str(path))
        self.size = os.path.getsize(str(path))


# ---------------------------------------------------------------- widgets
_DEFAULTS = {
    "catalog": "",                 # blank -> 2-level names, which local Spark supports
    "bronze_db": "tribal_bronze",
    "silver_db": "tribal_silver",
    "upload_dir": LOCAL_UPLOAD,
    "volumes_root": LOCAL_VOLUMES,
    "run_mode": "overwrite",
    "strict": "0",
}


class _Widgets:
    def text(self, name, default, doc=""):
        _DEFAULTS.setdefault(name, default)

    def get(self, name):
        return _DEFAULTS.get(name, "")


def install(with_delta=True):
    """Install the dbutils shim, and (optionally) pre-create a Delta-enabled
    SparkSession so the notebook's getOrCreate() picks it up.

    Databricks ships Delta built in; vanilla local Spark does not, so the
    difference lives here rather than in the notebook.
    """
    db = types.ModuleType("dbutils")
    db.fs = types.SimpleNamespace(ls=_ls, cp=_cp, mkdirs=_mkdirs, head=_head)
    db.widgets = _Widgets()
    sys.modules["dbutils"] = db
    if with_delta:
        try:
            from delta import configure_spark_with_delta_pip
            from pyspark.sql import SparkSession
            (configure_spark_with_delta_pip(
                SparkSession.builder.master("local[2]").appName("tribal-dataset"))
                .config("spark.sql.extensions",
                        "io.delta.sql.DeltaSparkSessionExtension")
                .config("spark.sql.catalog.spark_catalog",
                        "org.apache.spark.sql.delta.catalog.DeltaCatalog")
                .getOrCreate())
            print("[shim] Spark session with Delta ready")
        except Exception as e:
            print(f"[shim] Delta session failed ({type(e).__name__}: {e})")
    return db


def run(script_path, title):
    import runpy
    print("\n" + "=" * 74)
    print(f"RUNNING {title}")
    print("=" * 74)
    t0 = datetime.now()
    # `dbutils` is a builtin in Databricks; inject the shim into globals
    runpy.run_path(str(script_path), run_name="__main__",
                   init_globals={"dbutils": sys.modules["dbutils"]})
    print(f"\n[{title}] finished in {datetime.now() - t0}")

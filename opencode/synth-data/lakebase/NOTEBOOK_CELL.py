# Databricks notebook cell — one click, no public access required.
# 1) find and unzip the uploaded bundle
# 2) connect to Lakebase from inside the workspace
# 3) create the schema and load all rows
import pathlib, zipfile, sys, io, contextlib

cwd = pathlib.Path.cwd()

# --- 1. bundle ---
zips = list(cwd.rglob("synth-data.zip")) or list(cwd.rglob("*.zip"))
if not zips:
    raise SystemExit("synth-data.zip not found next to the notebook — upload it first")
with zipfile.ZipFile(zips[0]) as z:
    z.extractall(cwd)
root = next(p for p in cwd.rglob("synth-upload") if (p / "lakebase").is_dir())
print("unpacked ->", root)

# --- 2. run the loader (it does the rest) ---
sys.path.insert(0, str(root / "lakebase"))
import load_in_workspace
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = load_in_workspace.main()          # auto-discovers the unpacked root
out = buf.getvalue()
print(out[-4000:])                        # tail: the row counts
print("\nEXIT", rc)

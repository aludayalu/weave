import re, sys, types, pathlib, psycopg
src = pathlib.Path("/Users/aludayalu/weave/scripts/notebooks/03_silver_to_gold.py").read_text()
cells, cur, buf = [], None, []
for line in src.splitlines(keepends=True):
    if line.startswith("# %%"):
        if cur is not None: cells.append((cur, "".join(buf)))
        cur, buf = line.strip(), []
    else: buf.append(line)
if cur is not None: cells.append((cur, "".join(buf)))

TOK = open("/tmp/dtok.txt").read().strip()
class Secret:
    def get_secret(self, scope, key):
        o = types.SimpleNamespace(); o.value = TOK; return o
workspace = types.SimpleNamespace(secrets=Secret())
class Noop:
    def __getattr__(self, n): return lambda *a, **k: None
import databricks.sdk as _sdk
_sdk.WorkspaceClient = lambda *a, **k: workspace
G = {"workspace": workspace, "dbutils": Noop(), "getArgument": lambda n, d=None: d,
     "displayHTML": lambda *a, **k: None, "display": lambda *a, **k: None, "__name__": "__main__"}

def patch_conn():
    c = G.get("conn")
    if c is not None and not getattr(c, "_patched", False):
        c.autocommit = False            # so rollback actually works
        c.commit = lambda: None
        G["_real_commit"] = c
        c._patched = True

ran, skipped = [], False
for marker, code in cells:
    if marker == "# %% call the model":
        # no mlflow here: stub an empty proposal so the deterministic baseline wins
        G.update(model_payload={"windows": []}, model_usage={}, model_seconds=0.0,
                 model_started=None, model_reasoning_chars=0, prompt_chars=0,
                 model=MODEL if (MODEL:="qwen35") else "qwen35")
        skipped = True; print(">>> stubbed model cell (empty proposal) -> baseline should win\n"); continue
    if marker.endswith("[markdown]") or code.lstrip().startswith('"""'): continue
    try:
        exec(compile(code, f"<cell {marker}>", "exec"), G)
        ran.append(marker.replace("# %%","").strip())
    except Exception as e:
        print(f"\n!!! {marker} FAILED {type(e).__name__}: {str(e)[:400]}"); break
    patch_conn()

print("\n=== cells executed:", len(ran))
c = G.get("conn")
if c is not None:
    cur = c.cursor()
    for t in ("task_window", "task_chunk"):
        cur.execute(f"select count(*) from gold.{t}"); print(f"  gold.{t} rows written: {cur.fetchone()[0]}")
    cur.execute("select window_id, produced_by, start_ts, end_ts, confidence from gold.task_window order by start_ts limit 6")
    print("\n  sample windows:")
    for r in cur.fetchall(): print("   ", r)
    cur.execute("select source_table, count(*) from gold.task_chunk group by 1 order by 2 desc")
    print("\n  chunks by source:")
    for r in cur.fetchall(): print(f"    {r[0]}: {r[1]}")
    c.rollback()
    cur.execute("select count(*) from gold.task_window"); print("\nafter ROLLBACK gold.task_window:", cur.fetchone()[0])
    c.close()

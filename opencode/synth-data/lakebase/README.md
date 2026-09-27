# Storing the dataset on Lakebase

Lakebase (Postgres) is the **serving and bookkeeping plane**. The medallion
layers live there so the detection loop and the RL-generation jobs can do fast
point lookups — one task is one row plus a few hundred child rows — while heavy
scans run against the replicated Delta tables. Lakebase replicates into Unity
Catalog, so nothing is trapped inside Postgres.

```
synth-data/
├── lakebase/
│   ├── schema.sql          33 tables across 5 schemas + 1 scoring view
│   ├── load_lakebase.py    idempotent loader (bronze|silver|detected|gold)
│   └── README.md           this file
```

## Schemas

| schema | holds | why here |
|---|---|---|
| `ingest` | batch registry, landed-file registry, delta watermarks | answers "did the 24h trigger pick up the new files?" |
| `bronze` | raw payloads verbatim in `jsonb` | lossless; the dirty export stays auditable |
| `silver` | typed, normalized, deduplicated + `quarantine` | what the detection model reads |
| `detected` | the model's timeframe output | scored, replayed, diffed against a later run |
| `gold` | the dissected per-task material | what the next stage consumes |
| `ops` | model runs, RL rollouts + steps, fine-tune jobs, per-assertion scores | makes "trained vs untrained" reproducible |

## Load it

```bash
pip install 'psycopg[binary]'

# 1. verify the mapping without touching a database
python3 load_lakebase.py --dry-run

# 2. create the schema
psql "$LAKEBASE_DSN" -f schema.sql

# 3. load
python3 load_lakebase.py --dsn "$LAKEBASE_DSN"
```

From Databricks, the DSN comes from the Lakebase instance itself — either read
`DATABRICKS_SERVERLESS_COMPUTE_...`/instance connection details from the
workspace, or let the job resolve it:

```python
from databricks.sdk import WorkspaceClient
w = WorkspaceClient()
inst = w.database.get_database_instance(name="meridian-tribal")
dsn = inst.read_write_dsn        # host/port/db/user/password for psycopg
```

`--stages bronze,silver,detected,gold` loads a subset; re-running updates rows
instead of duplicating them (every table upserts on its natural key).

## Design decisions worth defending

**Documents stay in object storage.** `bronze.document` holds `object_uri`,
`sha256` and `bytes`, not the PDF/PNG payload. Lakebase is not a blob store.
Pass `--inline-docs` to store captures under 1MB as `bytea`, but keep the
default.

**The patch is externalized too.** `gold.task_diff` stores `base_sha`,
`head_sha`, `patch_object_uri`, `patch_sha256`, `patch_bytes` — the diff is
recomputable from the repo, and the hash proves the text the pipeline sliced
is the text the model will be trained on. `base_sha` is the **merge-base**, not
the merge commit's first parent, so work that landed on `main` while the branch
was open is never attributed to the task.

**Quarantine is a first-class table.** `silver.quarantine` should be non-empty;
it is the evidence that bronze was genuinely dirty and that silver did work
rather than passing malformed rows through.

**Two timestamp columns.** Every silver/bronze timestamp sits next to its
original string (`ts` / `ts_raw`). When a naive or unix-seconds timestamp shows
up, silver repairs it *and keeps the original* so the repair is auditable.

**The model's answer is not gold.** `detected.task_window` is the timeframe
output; `gold.task` is what the pipeline produced *from* it. Keeping them apart
is what lets you score a detector and diff two runs of it.

## Benchmark bookkeeping

`ops` is the part that makes the experiment defensible:

```
ops.dataset_version   {"train": 25, "eval": 7}
ops.model_run         base model, gateway endpoint, prompt + harness version
ops.rl_run            one rollout per task, split-tagged
ops.rl_run_step       every message: reasoning_content, tool_args, tool_result
ops.fine_tune_job     kind = 'train' | 'eval'
ops.eval_result       one row per assertion, with weight and category
```

`ops.eval_task_score` gives the weighted score per task:

```sql
SELECT kind, task_id, earned_weight, total_weight, pct
FROM ops.eval_task_score
ORDER BY kind, pct DESC;
```

Scoring is weighted partial credit across `spec` / `regression` / `convention`
categories rather than pass/fail, so a single missed assertion does not read as
total failure and the trained-vs-untrained gap is a distribution, not a bit.

## 24h incremental trigger

`ingest.watermark` holds `last_ts` per stream; the job reads only rows past the
watermark and upserts. Re-running the same day is a no-op because every write is
keyed on a natural id:

```sql
SELECT last_ts FROM ingest.watermark WHERE stream = 'discord';
-- pull only newer messages, then:
--   INSERT ... ON CONFLICT (discord_message_id) DO UPDATE
```

`ingest.source_file` is the safety net: if a file's `sha256` changes under the
same URI, the bronze upsert rewrites it and the row's `landed_at` moves, so a
re-exported channel never silently goes stale.

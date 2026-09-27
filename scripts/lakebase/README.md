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

### 1. Give it credentials — via a file, never in chat

```bash
cd synth-data/lakebase
cp .env.example .env      # .env is gitignored
$EDITOR .env              # fill in ONE of the two shapes below
```

**Shape A (preferred — no long-lived password).** Create a database instance in
the workspace UI (*Database instances* / Lakebase), then:

```bash
DATABRICKS_HOST=https://<workspace>.cloud.databricks.com
LAKEBASE_INSTANCE=<instance-name>
DATABRICKS_TOKEN=<token>          # `databricks auth login` writes this
```

The SDK asks Lakebase for a short-lived database password, so nothing
long-lived is stored. Requires `pip install databricks-sdk`.

**Shape B — a plain DSN**, copied from the instance page in the UI:

```bash
LAKEBASE_DSN=postgresql://<user>:<password>@<host>:5432/<db>?sslmode=require
```

### 2. Verify

```bash
python3 -m venv .venv && .venv/bin/pip install 'psycopg[binary]'
.venv/bin/python check_connection.py      # masked creds + connectivity + row counts
.venv/bin/python load_lakebase.py --dry-run   # mapping + column arity, no DB
```

### 3. Load

```bash
.venv/bin/python load_lakebase.py --apply-schema   # creates the 33 tables
.venv/bin/python load_lakebase.py                  # ~3.3k rows, idempotent
.venv/bin/python check_connection.py               # read the row counts back
```

`credentials.py` resolves in this order: process env → `./.env` →
`~/.databrickscfg`. Nothing it prints is ever unmasked.

`--stages bronze,silver,detected,gold` loads a subset. Re-running updates rows
instead of duplicating them, because every table upserts on its natural key.

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

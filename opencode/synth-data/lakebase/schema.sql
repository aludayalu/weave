-- =====================================================================
-- meridian-erp tribal-knowledge dataset — Lakebase (Postgres) schema
-- =====================================================================
-- Lakebase is the serving/OLTP plane. The medallion layers live here so
-- the detection loop and the RL-generation jobs can do fast point lookups
-- (one task = one row + a few hundred child rows), while the heavy scans
-- run against the replicated Delta tables. Lakebase replicates into Unity
-- Catalog, so nothing is locked inside Postgres.
--
-- Conventions
--   * every table carries the ingest batch that wrote it (batch_id)
--   * bronze keeps the original payload verbatim in `raw` jsonb; silver
--     adds the typed, normalized, deduplicated columns
--   * timestamps are `timestamptz` (UTC) and always sit next to the
--     original string (`*_raw`) so a bad export stays auditable
--   * natural keys make re-ingest idempotent: message snowflake, issue
--     key, history id, commit sha, pr number, document sha256
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS ingest;
CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;
CREATE SCHEMA IF NOT EXISTS ops;

-- ---------------------------------------------------------------- ingest
-- One row per pipeline run. batch_id is derived from the content hash of
-- the run, so re-running the same inputs reuses the same id.
CREATE TABLE IF NOT EXISTS ingest.ingest_batch (
    batch_id          uuid PRIMARY KEY,
    pipeline_version  text        NOT NULL,
    stage             text        NOT NULL,   -- bronze | silver | detected | gold
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,
    status            text        NOT NULL DEFAULT 'running',
    source_root       text,
    notes             jsonb
);

-- Landing registry: every file the ingest read, with its hash. This is
-- what makes "did the 24h trigger pick up the new files?" answerable.
CREATE TABLE IF NOT EXISTS ingest.source_file (
    source_file_id    uuid PRIMARY KEY,
    batch_id          uuid        NOT NULL REFERENCES ingest.ingest_batch(batch_id),
    stream            text        NOT NULL,   -- discord | jira | github | attachment
    uri               text        NOT NULL,
    sha256            text        NOT NULL,
    bytes             bigint      NOT NULL,
    row_count         bigint,
    landed_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (stream, uri, sha256)
);

-- Delta watermarks for the 24h incremental trigger.
CREATE TABLE IF NOT EXISTS ingest.watermark (
    stream            text PRIMARY KEY,
    last_ts           timestamptz,
    last_key          text,
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- bronze
-- Lossless. `raw` is the source payload exactly as the API returned it.
-- bot_message_id is content-derived (uuid5 of file name + content hash), so it
-- doubles as the per-file batch key; no separate batch_id column is needed.
CREATE TABLE IF NOT EXISTS bronze.discord_channel (
    bot_message_id    uuid PRIMARY KEY,
    guild_id          text,
    guild_name        text,
    channel_id        text,
    channel_name      text,
    channel_type      text,
    exported_at       timestamptz,
    declared_count    integer,              -- exporter's own count, often wrong
    raw               jsonb       NOT NULL
);

CREATE TABLE IF NOT EXISTS bronze.discord_message (
    discord_message_id text PRIMARY KEY,
    bot_message_id     uuid        NOT NULL REFERENCES bronze.discord_channel(bot_message_id),
    ordinal            integer     NOT NULL,
    author             jsonb,
    timestamp_raw      text,                -- may be unix seconds / naive / offset
    edited_raw         text,
    content_raw        text,
    jump_url           text,
    attachments        jsonb,
    raw                jsonb       NOT NULL
);

CREATE TABLE IF NOT EXISTS bronze.jira_issue (
    issue_key          text PRIMARY KEY,
    batch_id           uuid        NOT NULL,
    jira_issue_id      text,
    raw                jsonb       NOT NULL
);

CREATE TABLE IF NOT EXISTS bronze.jira_changelog_history (
    history_id         text PRIMARY KEY,
    issue_key          text        NOT NULL,
    batch_id           uuid        NOT NULL,
    created_raw        text,
    author             jsonb,
    items              jsonb,
    raw                jsonb       NOT NULL
);

CREATE TABLE IF NOT EXISTS bronze.github_commit (
    sha                text PRIMARY KEY,
    batch_id           uuid        NOT NULL,
    author_name        text,
    author_email       text,
    authored_raw       text,
    committed_raw      text,
    message            text,
    parents            text[],
    stats              jsonb,
    raw                jsonb       NOT NULL
);

CREATE TABLE IF NOT EXISTS bronze.github_commit_file (
    sha                text        NOT NULL,
    filename           text        NOT NULL,
    additions          integer,
    deletions          integer,
    PRIMARY KEY (sha, filename)
);

CREATE TABLE IF NOT EXISTS bronze.github_pull_request (
    pr_number          integer     PRIMARY KEY,
    batch_id           uuid        NOT NULL,
    raw                jsonb       NOT NULL
);

CREATE TABLE IF NOT EXISTS bronze.github_event (
    event_key          text PRIMARY KEY,    -- type|action|pr|created_at
    batch_id           uuid        NOT NULL,
    pr_number          integer,
    created_at         timestamptz,
    raw                jsonb       NOT NULL
);

-- Document metadata only. The bytes live in object storage (UC Volume /
-- S3); Postgres keeps the pointer plus the hash so a re-ingest can prove
-- the file has not changed. Set inline_bytes to store small captures
-- (<1MB) in the row instead.
CREATE TABLE IF NOT EXISTS bronze.document (
    document_id        text PRIMARY KEY,    -- sha256 of the file
    batch_id           uuid        NOT NULL,
    stream             text        NOT NULL,  -- discord | jira
    filename           text        NOT NULL,
    object_uri         text        NOT NULL,
    mime_type          text,
    bytes              bigint      NOT NULL,
    sha256             text        NOT NULL,
    linked_issue_key   text,
    linked_channel     text,
    inline_bytes       bytea
);

-- ---------------------------------------------------------------- silver
-- Cleaned, typed, deduplicated. Nothing malformed may reach these tables.
CREATE TABLE IF NOT EXISTS silver.discord_message (
    discord_message_id text PRIMARY KEY,
    batch_id           uuid        NOT NULL,
    channel            text,
    guild              text,
    author_id          text,
    author_name        text        NOT NULL,
    ts                 timestamptz NOT NULL,
    ts_raw             text,
    edited_at          timestamptz,
    content            text        NOT NULL,
    is_thread          boolean     NOT NULL DEFAULT false,
    mentions_ticket    text[]      NOT NULL DEFAULT '{}',
    mentions_pr        integer[]   NOT NULL DEFAULT '{}',
    attachment_uuids   text[]      NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS silver_discord_ts       ON silver.discord_message (ts);
CREATE INDEX IF NOT EXISTS silver_discord_channel  ON silver.discord_message (channel, ts);
CREATE INDEX IF NOT EXISTS silver_discord_ticket   ON silver.discord_message USING gin (mentions_ticket);

CREATE TABLE IF NOT EXISTS silver.jira_issue (
    issue_key          text PRIMARY KEY,
    batch_id           uuid        NOT NULL,
    summary            text,
    description        text,
    status             text,
    reporter           text,
    assignee           text,
    created_at         timestamptz,
    updated_at         timestamptz
);

CREATE TABLE IF NOT EXISTS silver.jira_changelog_event (
    event_id           bigserial PRIMARY KEY,
    batch_id           uuid        NOT NULL,
    issue_key          text        NOT NULL REFERENCES silver.jira_issue(issue_key),
    ts                 timestamptz NOT NULL,
    author             text,
    field              text,
    from_value         text,
    to_value           text
);
CREATE INDEX IF NOT EXISTS silver_changelog_key_ts ON silver.jira_changelog_event (issue_key, ts);

CREATE TABLE IF NOT EXISTS silver.github_commit (
    sha                text PRIMARY KEY,
    batch_id           uuid        NOT NULL,
    author_name        text        NOT NULL,
    author_email       text,
    authored_at        timestamptz NOT NULL,
    committed_at       timestamptz,
    message            text,
    is_merge           boolean     NOT NULL DEFAULT false,
    parent_shas        text[]      NOT NULL DEFAULT '{}',
    additions          integer     NOT NULL DEFAULT 0,
    deletions          integer     NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS silver_commit_authored ON silver.github_commit (authored_at);

CREATE TABLE IF NOT EXISTS silver.github_pull_request (
    pr_number          integer     PRIMARY KEY,
    batch_id           uuid        NOT NULL,
    title              text,
    state              text,
    merged             boolean     NOT NULL DEFAULT false,
    author             text,
    branch             text,
    merge_sha          text,
    opened_at          timestamptz,
    merged_at          timestamptz,
    closed_at          timestamptz
);

-- The cross-stream join: what makes the three streams dissectable.
CREATE TABLE IF NOT EXISTS silver.entity_link (
    entity             text PRIMARY KEY,   -- MER-101 or PR#12
    kind               text        NOT NULL, -- ticket | pull_request
    discord_message_ids text[]     NOT NULL DEFAULT '{}',
    jira_issue_keys    text[]      NOT NULL DEFAULT '{}',
    github_shas        text[]      NOT NULL DEFAULT '{}',
    github_pr_numbers  integer[]   NOT NULL DEFAULT '{}',
    first_ts           timestamptz,
    last_ts            timestamptz
);

-- Everything silver refused, with the reason. This table is the argument
-- that bronze really was dirty: it should be non-empty and explain itself.
CREATE TABLE IF NOT EXISTS silver.quarantine (
    quarantine_id      bigserial PRIMARY KEY,
    batch_id           uuid        NOT NULL,
    target_table       text        NOT NULL,
    entity_key         text,
    reason             text        NOT NULL,
    raw                jsonb,
    quarantined_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS silver_quarantine_reason ON silver.quarantine (reason);

-- ------------------------------------------------- detected (pre-gold)
-- The MODEL's timeframe output, stored so it can be scored, replayed and
-- diffed against a later run. This is not gold: it is the model's answer,
-- which the pipeline then acts on.
CREATE TABLE IF NOT EXISTS detected.task_window (
    task_id            text PRIMARY KEY,
    batch_id           uuid        NOT NULL,
    produced_by        text        NOT NULL,  -- model id or 'local-standin-detector'
    model_version      text,
    start_ts           timestamptz NOT NULL,
    end_ts             timestamptz NOT NULL,
    confidence         numeric(3,2),
    title              text,
    summary            text,
    participants       text[]      NOT NULL DEFAULT '{}',
    evidence           jsonb       NOT NULL,
    scored_at          timestamptz,
    matched_reference  boolean,
    start_error_minutes  numeric(8,2),
    end_error_minutes    numeric(8,2)
);

-- ---------------------------------------------------------------- gold
-- The dissected material the next model consumes.
CREATE TABLE IF NOT EXISTS gold.task (
    task_id            text PRIMARY KEY,
    batch_id           uuid        NOT NULL,
    jira_issue_key     text,
    pr_number          integer,
    split              text        NOT NULL CHECK (split IN ('train', 'eval')),
    title              text,
    channel            text,
    start_ts           timestamptz NOT NULL,
    end_ts             timestamptz NOT NULL,
    duration_minutes   integer,
    confidence         numeric(3,2),
    tribal_constraint  text,
    brief              jsonb,
    coverage           jsonb,
    CHECK (end_ts > start_ts)
);
CREATE INDEX IF NOT EXISTS gold_task_split ON gold.task (split, start_ts);
CREATE INDEX IF NOT EXISTS gold_task_jira  ON gold.task (jira_issue_key);

CREATE TABLE IF NOT EXISTS gold.task_message (
    task_id            text        NOT NULL REFERENCES gold.task(task_id) ON DELETE CASCADE,
    discord_message_id text        NOT NULL,
    ordinal            integer     NOT NULL,
    is_start           boolean     NOT NULL DEFAULT false,
    is_end             boolean     NOT NULL DEFAULT false,
    PRIMARY KEY (task_id, discord_message_id)
);

CREATE TABLE IF NOT EXISTS gold.task_changelog_event (
    task_id            text        NOT NULL REFERENCES gold.task(task_id) ON DELETE CASCADE,
    event_id           bigint      NOT NULL,
    PRIMARY KEY (task_id, event_id)
);

CREATE TABLE IF NOT EXISTS gold.task_commit (
    task_id            text        NOT NULL REFERENCES gold.task(task_id) ON DELETE CASCADE,
    sha                text        NOT NULL,
    ordinal            integer     NOT NULL,
    PRIMARY KEY (task_id, sha)
);

CREATE TABLE IF NOT EXISTS gold.task_document (
    task_id            text        NOT NULL REFERENCES gold.task(task_id) ON DELETE CASCADE,
    document_id        text        NOT NULL,
    PRIMARY KEY (task_id, document_id)
);

-- The patch. Text is externalized to object storage (that is what the RL
-- prompt streams in); the row keeps the pointer, hash and size.
CREATE TABLE IF NOT EXISTS gold.task_diff (
    task_id            text PRIMARY KEY REFERENCES gold.task(task_id) ON DELETE CASCADE,
    base_sha           text        NOT NULL,  -- merge-base: keeps concurrent
    head_sha           text        NOT NULL,  -- work on main out of the task
    merge_sha          text,
    patch_object_uri   text,
    patch_sha256       text,
    patch_bytes        integer
);

-- Scoring instruments. Never loaded from the candidate tree at eval time;
-- injected by the scorer.
CREATE TABLE IF NOT EXISTS gold.task_rubric (
    task_id            text PRIMARY KEY REFERENCES gold.task(task_id) ON DELETE CASCADE,
    suite_object_uri   text        NOT NULL,
    categories         jsonb       NOT NULL,  -- {spec:4, regression:3, convention:2}
    total_weight       integer     NOT NULL
);

-- ---------------------------------------------------------------- ops
-- Bookkeeping for "trained vs not trained", which is the whole experiment.
CREATE TABLE IF NOT EXISTS ops.dataset_version (
    dataset_version    text PRIMARY KEY,
    created_at         timestamptz NOT NULL DEFAULT now(),
    task_counts        jsonb,               -- {"train":25,"eval":7}
    root_uri           text,
    manifest_sha256    text
);

CREATE TABLE IF NOT EXISTS ops.model_run (
    run_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    base_model         text        NOT NULL,
    model_endpoint     text,                 -- databricks model gateway route
    dataset_version    text        REFERENCES ops.dataset_version(dataset_version),
    prompt_version     text        NOT NULL, -- detection/rl prompt hash
    harness_version    text        NOT NULL,
    params             jsonb,
    created_at         timestamptz NOT NULL DEFAULT now()
);

-- One synthetic RL rollout per task. Output is the JSON array in the
-- gateway message format, stored in object storage and referenced here.
CREATE TABLE IF NOT EXISTS ops.rl_run (
    run_id             uuid        NOT NULL REFERENCES ops.model_run(run_id) ON DELETE CASCADE,
    task_id            text        NOT NULL,
    split              text        NOT NULL CHECK (split IN ('train', 'eval')),
    status             text        NOT NULL DEFAULT 'pending',
    started_at         timestamptz,
    finished_at        timestamptz,
    tool_calls         integer,
    tokens_in          integer,
    tokens_out         integer,
    output_object_uri  text,
    PRIMARY KEY (run_id, task_id)
);

-- Per-step trace: the thinking traces and tool I/O, one row per message
-- in the run's JSON array.
CREATE TABLE IF NOT EXISTS ops.rl_run_step (
    run_id             uuid        NOT NULL,
    task_id            text        NOT NULL,
    step               integer     NOT NULL,
    role               text        NOT NULL,
    reasoning_content  text,                 -- thinking trace
    tool_call_id       text,
    tool_name          text,
    tool_args          jsonb,
    tool_result        jsonb,                 -- {completed,timed_out,stdout,...}
    latency_ms         integer,
    PRIMARY KEY (run_id, task_id, step),
    FOREIGN KEY (run_id, task_id) REFERENCES ops.rl_run(run_id, task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ops.fine_tune_job (
    job_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id             uuid        REFERENCES ops.model_run(run_id),
    kind               text        NOT NULL CHECK (kind IN ('train', 'eval')),
    base_model         text        NOT NULL,
    training_set_uri   text,
    started_at         timestamptz,
    finished_at        timestamptz,
    status             text        NOT NULL DEFAULT 'queued',
    endpoint           text
);

-- Per-assertion scoring. Weighted partial credit, not pass/fail: an
-- untrained model lands mid-range, a trained one high, and the gap is
-- read off these rows rather than asserted.
CREATE TABLE IF NOT EXISTS ops.eval_result (
    job_id             uuid        NOT NULL REFERENCES ops.fine_tune_job(job_id) ON DELETE CASCADE,
    task_id            text        NOT NULL,
    suite_name         text        NOT NULL,
    category           text        NOT NULL CHECK (category IN ('spec', 'regression', 'convention', 'cross_task')),
    assertion          text        NOT NULL,
    weight             integer     NOT NULL DEFAULT 1,
    passed             boolean     NOT NULL,
    error              text,
    PRIMARY KEY (job_id, task_id, suite_name, assertion)
);
CREATE INDEX IF NOT EXISTS ops_eval_by_task ON ops.eval_result (task_id, category);

-- Convenience view: score per task, weighted.
CREATE OR REPLACE VIEW ops.eval_task_score AS
SELECT job_id, task_id, kind,
       sum(weight)                                        AS total_weight,
       sum(weight) FILTER (WHERE passed)                  AS earned_weight,
       round(100.0 * sum(weight) FILTER (WHERE passed)
             / NULLIF(sum(weight), 0), 1)                 AS pct
FROM ops.eval_result er
JOIN ops.fine_tune_job j USING (job_id)
GROUP BY job_id, task_id, kind;

# Meridian ERP — Synthetic Data Pipeline (Databricks)

Three raw streams land unsplit. All segmentation, cleaning, and window detection
happens here — never upstream. Medallion stages are mandatory.

```
raw/                      (as written to disk, byte-identical to source APIs)
├── discord/<channel>.json        5 files, 448 messages, bot.py archive schema
├── jira/issues.json              32 issues, 128 changelog entries, REST v2 shape
├── github/commits.json           128 commits, all refs
├── github/pulls.json             34 PRs (32 merged + 2 parked spikes)
├── github/events.json            100 PR lifecycle events
└── attachments/                  64 documents (PDF reports, PNG captures, .log files)
```

| Stream | Source | Ingest cadence | Watermark |
|---|---|---|---|
| Discord | `bot.py` archive job (guild + channels + threads) | every 24h, delta by `message_id` | max `message_id` per channel |
| Jira | `GET /rest/api/3/search?jql=project=MER` + `?expand=changelog` | every 24h | max `changelog.histories[].created` |
| GitHub | `GET /repos/meridian/meridian-erp/{commits,pulls}` + `git` bundle | every 24h | max `commit.committer.date` |

Documents are referenced, never inlined: Discord carries `attachments[]`
(`uuid`, `original_filename`, `size`, `content_type`, `path`), Jira carries
`fields.attachment[]` (`id`, `filename`, `size`, `mimeType`, `content`, `created`).
Files stay on disk; bronze stores the path plus size, silver stores the extracted text.

## Stage 1 — BRONZE (raw, append-only, lossless)

| Table | Source | Key notes |
|---|---|---|
| `bronze.discord_messages` | `raw/discord/*.json` | every field as string/json; `ingested_at`, `_source_file`, `_ingest_id` |
| `bronze.jira_issues` | `raw/jira/issues.json` | full issue payload, `changelog` kept as raw JSON |
| `bronze.github_commits` | `raw/github/commits.json` | one row per commit, `files` array as JSON |
| `bronze.github_pulls` | `raw/github/pulls.json` | one row per PR |
| `bronze.github_events` | `raw/github/events.json` | PR lifecycle events |
| `bronze.documents` | `raw/attachments/**` | `doc_id`, `stream`, `filename`, `size`, `content_type`, `path` |

Rules: never drop a column, never fail a batch on bad rows (quarantine + `_errors`),
store the ingest batch id, and make bronze idempotent on re-ingest (re-running the
same day must not duplicate rows).

## Stage 2 — SILVER (typed, normalized, entity-resolved)

| Table | Transformation |
|---|---|
| `silver.discord_messages` | explode to one row per message; `ts_utc` (timestamp), `ts_epoch`, `author_id`, `author_name`, `channel`, `guild`, `content`, `is_thread`, `attachment_ids[]`, `mentions_ticket` (regex `MER-\d+`), `mentions_pr` (regex `PR #\d+`) |
| `silver.jira_changelog_events` | explode `changelog.histories` → one row per history: `issue_key`, `event_ts_utc`, `event_ts_epoch`, `author`, `field`, `from_value`, `to_value` |
| `silver.jira_issues` | typed issue rows + denormalized `first_seen_ts`, `last_seen_ts` |
| `silver.github_commits` | `sha`, `repo`, `author_name`, `author_email`, `authored_ts_utc`, `committed_ts_utc`, `message`, `is_merge`, `parent_shas[]`, `additions`, `deletions`, `changed_files[]` |
| `silver.github_pull_requests` | `pr_number`, `state`, `merged`, `opened_ts_utc`, `merged_ts_utc`, `branch`, `merge_sha`, `author`, `title` |
| `silver.documents` | join bronze + text extraction (pypdf for PDF, utf-8 for logs, vision/OCR for PNG): `doc_id`, `filename`, `doc_type`, `extracted_text`, `extracted_chars`, `linked_issue_key`, `linked_channel` |
| `silver.links` | the cross-stream join that makes the data dissectable: `entity` (MER-xxx / PR #), `discord_message_ids[]`, `jira_issue_keys[]`, `github_shas[]`, `github_pr_numbers[]`, `first_ts_utc`, `last_ts_utc` |

Silver is where UTC normalization happens. All three sources are already UTC; any
offset or naive timestamp is rejected to `_errors`, never silently coerced.

## Between silver and gold: the detection loop

Silver is ingested **in a loop** by the task-detection model. The model reads the
silver tables and decides, for every task, its start and end timestamps. That output
is written to `detected/task_windows.json` and is the *only* input to the dissection
step. Nothing else chooses a boundary.

```
bronze/ (raw, malformed)  ->  silver/ (cleaned, typed, linked)  ->  MODEL LOOP
                                                                       |
                                              detected/task_windows.json  (timeframes)
                                                                       |
                                                                     GOLD
```

`detected/task_windows.json` shape (see `specs/task_detection_prompt.md`):

```json
{"task_id":"mer-101","start_ts_utc":"2026-07-08T15:00:00Z","end_ts_utc":"2026-07-10T21:12:00Z",
 "confidence":0.95,"title":"...","summary":"...","participants":["..."],
 "evidence":{"discord":{"channel":"billing","start_message_id":"...","end_message_id":"...",
                        "supporting_message_ids":["..."]},
             "jira":{"issue_keys":["MER-101"],"closing_event_ts_utc":"..."},
             "github":{"merge_shas":["..."],"pr_numbers":[1],"branch":"..."},
             "documents":["..."]}}
```

## Stage 3 — GOLD (the slices carved by those timeframes)

The timeframe output is used to slice every stream. Each task becomes a self-contained
directory, which is the material passed to the next stage of the pipeline.

| Path under `gold/<task_id>/` | Contents |
|---|---|
| `discord.json` | the messages inside the window, with author, attachments, ticket/PR references |
| `jira.json` | the issue plus the changelog events inside the window |
| `git.json` | the PR, the merge-base, the branch tip, and the commits the PR brought |
| `diff.patch` | the real patch: `git diff <merge-base>..<branch-tip>` for that task |
| `documents.json` | attached documents with extracted text, linked to the task |
| `brief.json` | the handoff: problem, tribal evidence, participants, coverage counters |
| `gold/_index.json` | one row per task: window, duration, and how much of each stream it captured |

The patch uses the **merge-base**, not the merge commit's first parent, so work that
landed on `main` while the branch was open is not attributed to the task.

Ground truth for scoring the detector is `gold/reference_windows.json`. It is compared
against the model's output with `tools/gold_detect.py score`, and is never fed to the
detection prompt.

## Fine-tuning / evaluation routing

- `train` split (t01–t25) → detection + synthetic RL generation → fine-tune set.
- `eval` split (e01–e07) → gold windows are held out of every training path. The
  scored repository snapshot is the commit at the eval window's start, and the eval
  task's test suite is injected at scoring time only.
- The pipeline must never emit eval windows into a training dataset; assert the
  split column at the gold write.

## Directory layout produced by the pipeline

```
synth-data/
├── raw/                 (input, never modified)
├── gold/
│   ├── tasks_gold.json       ground truth (scoring only)
│   ├── documents_index.json
│   └── slots_all32.json
├── specs/                    prompts + harness contract
└── tools/                    generators + validator used to build the raw data
```

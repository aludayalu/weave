# Meridian ERP — synthetic tribal-knowledge dataset

Builds a dataset that teaches a model *how this specific team solved problems*,
and measures whether that tribal data actually helps.

Everything lives under `weave/opencode/`:

```
meridian/                 the synthetic codebase (real git history, 128 commits)
synth-data/
  raw/                    the three unsplit source streams + attached documents
  gold/                   ground truth for scoring (never fed to the pipeline)
  specs/                  Databricks pipeline, detection prompt, RL prompt + harness
  tools/                  generators + validator
viewer/                   website: pick a task, see the segmented 3-stream view
```

## The three raw streams

| stream | shape | size |
|---|---|---|
| Discord | one JSON per channel, exactly the schema `weave/bot/bot.py` archives | 5 channels, 448 messages |
| Jira | REST v2 issues with full `changelog` histories | 32 issues, 128 changelog entries |
| GitHub | REST-shaped `commits.json`, `pulls.json`, `events.json` | 128 commits, 34 PRs (32 merged, 2 parked) |

Plus **64 attached documents** (programmatic client-report PDFs, screenshot PNGs,
timestamped `.log` captures) referenced from both Discord `attachments[]` and
Jira `fields.attachment[]`, with real file sizes.

The streams are **raw and unsplit**. Nothing here decides where a task starts or
ends — the pipeline does that on Databricks, so the judges see the real
bronze → silver → gold work:

- `synth-data/specs/medallion_pipeline.md` — table-by-table bronze/silver/gold design
- `synth-data/specs/task_detection_prompt.md` — the agent prompt that finds task windows
- `synth-data/specs/rl_generation_prompt.md` — synthetic RL run contract, harness, and thinking traces

## How a model is dissected from this

Every task window is anchored identically across all three streams, which is what
makes the segmentation recoverable from raw data alone:

- a Discord message that first raises the problem (start anchor)
- a Jira issue created inside the window, transitions ending in Done
- a `MER-xxx` / `PR #n` reference appearing in chat, ticket, and branch name
- a merge commit whose committer timestamp falls inside the window (end anchor)
- attached documents whose content belongs to that window

Validated mechanically:

```
$ python3 synth-data/tools/validate_streams.py
discord: 5 channels / 448 messages
jira:     32 issues, 128 changelog entries
github:  128 commits, 34 PRs (32 merged)
gold:     32 task windows

ALL CHECKS PASSED
```

The validator enforces producer-faithful schemas, UTC + monotonic timestamps,
changelog ordering inside issue lifetimes, parent/child commit integrity, merge
commit shape, attachment existence and byte size, and that every one of the 32
windows is anchored by a real chat message, a real issue, and a real merge.

## The 32 tasks

25 training (t01–t25) + 7 held-out (e01–e07). Each one is a problem that only
exists at this codebase's size — an append-only numeric contract, a flag key
that lives in three spellings, a redacting marshaler beside a generated one, a
cookie policy that differs between two coexisting middleware profiles.

The repository grew task by task: each task is a `test: … (red)` spec commit, a
fix commit, and a `--no-ff` merge commit, interleaved with background work and
parked spikes, so git history is the ground truth for the windows.

Scoring instruments: `synth-data/gold/tests/` (held-out, category-mirrored to
the training suites) and `synth-data/gold/tasks_gold.json` (windows + evidence).
Both are held out of the training path by construction.

## Repo state

- 32 task histories, 34 PRs, 128 commits, spanning 2026-07-06 → 2027-02-19
- Go suite: 17 packages green · web suite: 58 tests green
- Tests were written spec-first (red) and never edited inside the fix commits

## Reproduce

```bash
cd synth-data
python3 tools/gen_streams.py        # documents (PDF/PNG/log)
python3 tools/gen_discord_jira.py   # discord + jira raw streams
python3 tools/gen_github.py         # github raw export
python3 tools/validate_streams.py   # schema + cross-stream validation

cd .. && python3 -m http.server 8777   # then open /viewer/
```

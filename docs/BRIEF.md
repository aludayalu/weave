# weave — complete project brief

A single document covering what we set out to build, why it works, every stage of the
pipeline, every artefact produced, and the design language used throughout. Written to
be the source of truth for the pitch, the deck and any code review.

---

## 1. The original framing, in the founder's words

> A company has a lot of tribal data — the way the company actually works, how processes
> happen, how legality works, how compliance works, how coordination works across teams.
>
> All of that tribal information is embedded in the chats and in indirect communication.
>
> Take a software engineering company. The procedures you have to follow when you touch
> certain parts of the code are completely different from other parts. Some parts are low
> priority. Some are much higher priority. And touching certain things in a low priority
> area can trigger something that suddenly becomes a higher priority thing.
>
> The only way you know what matters and what does not matter, over the years, is by
> understanding across tasks what has been done.
>
> Tribal data is very important. Now most companies are trying to adopt agents, and the
> problem is that as soon as you actually get an agent into the loop, the agent has no
> tribal knowledge. It can try to find some, but by itself it does not have it. So it will
> skip steps. It will make tests that do not make sense. It will just get the stuff done,
> because it does not know any better. It is not trying to deceive you. It does not know
> any better.

### The button example, which is the whole pitch

> The model gets a task to change the colour of a button. That sounds very simple. But the
> specific component they are trying to manipulate is heavily used across a hundred
> different subprojects that have imported it. So changing this behaviour spills
> unexpected behaviour across everyone's codebases.
>
> The agent does not know any better, because it does not understand your infrastructure.
> And the reason the agent did not call the vector API is that when it did initially call
> for it, the task in isolation seemed very simple, so the model did not know any better.
>
> But what if the model actually knew, intuitively, not by having to figure it out, the
> actual way that the entire company is designed — not just at the code level but also at
> the organisational level?

### The proposed mechanism

> You take all the histories of chat, and you split them into histories. By history I mean
> you split tasks by time, and you take git diffs and you take batches of Discord messages
> and you batch them together. Now you can process that all together and you can create
> synthetic data out of it. You can train the model on this synthetic data.
>
> Now the model is trained on exactly how humans have been working on this codebase, in
> this company, for the last decade or so. Because of this, when the same model is spawned
> into the same problem of just changing a button, the model knows that this category of
> buttons inside this particular subdirectory is actually shared, is used by a hundred
> different projects. And given that it was spawned in a subdirectory, it has no clue that
> this is actually a monorepo setup. But the agent actually knows intuitively, because the
> agent was trained in an environment where it knows that everything is a monorepo.

### The business model, in the founder's words

> We take your unstructured data, we put it through Databricks. First we clean it through
> three phases: bronze, silver, gold. Then we take the gold, and what we do is we generate
> synthetic data out of it — essentially what a model should have ideally done, just like
> a human, to be able to solve that particular task. And then we train the model on that
> task with that synthetically generated data. That is our platinum data, better than gold.
>
> Then what we do is we fine tune the model, and this fine-tuned model is hosted by us. And
> we send you API access for it. We also give you our own harness. The model is
> specifically trained inside our harness, so it performs exceptionally well inside our own
> harness. And our harness is also available on desktops, you can download it already.

---

## 2. The precise insight, stated so it cannot be argued with

**The problem is not retrieval. The problem is that retrieval is consulted too late.**

A vector-backed agent asks its knowledge base when the model *decides* the question is
worth asking. On a ticket that reads like a one-line style change, the model never asks.
Retrieval did not fail. It was never invoked.

You cannot fix this with a better index, because the thing that was needed was not a
document. It was a prior: *this kind of thing, in this kind of directory, is shared, and
changing its behaviour has a blast radius.*

And you cannot write that prior down, because it was never written down. It accumulated,
one reasonable decision at a time, over years.

So the fix is not a retrieval step. The fix is putting the knowledge in the weights.

| | Retrieval approach | weave |
|---|---|---|
| When knowledge arrives | at query time, if asked | at training time, always |
| Who decides to consult | the model, at inference | us, offline |
| Failure mode | silent skip on small-looking tasks | none, because there is no step to skip |
| What it can express | text chunks | priors about blast radius, org, compliance |
| Does it compound | no | yes, every run adds trajectories |

---

## 3. The pipeline

Five stages. Each one is implemented and has been run against live data. The counts below
are real, read from the database, not illustrative.

```
  sources ─▶ BRONZE ─▶ SILVER ─▶ GOLD ─▶ PLATINUM ─▶ WEIGHTS
             raw        clean       tasks    replay      LoRA
                         linked      sliced   captured
```

### Stage 01 — BRONZE: land everything

The only stage where volume is the goal. Nothing is filtered, nothing is judged.

Two connectors write into the same shape:

- **GitHub REST** — commits, pull requests, events. Paginated, resumable.
- **A Discord bot** — a long-lived websocket process that archives channels on demand,
  writing one JSON file per channel with attachments saved to disk. The bot records which
  guilds it has already seen in a small SQLite state store so it does not re-archive.

Bronze tables and the row counts we actually produced:

| table | rows |
|---|---|
| `discord_message` | 458 |
| `github_commit` | 129 |
| `github_pull_request` | 35 |
| `github_event` | 100 |
| `jira_issue` | 33 |
| `jira_changelog_history` | 143 |
| `discord_channel` | 5 |
| `document` | 65 |

### Stage 02 — SILVER: clean and link

Deduplicate, resolve identity, and build the cross-system join that makes a time window
meaningful later. Commit SHA to ticket key to Discord thread.

Rows that fail a rule go to `silver.quarantine` rather than being deleted, so the cleaning
is auditable. 37 rows are sitting in quarantine right now.

| table | rows |
|---|---|
| `discord_message` | 435 |
| `jira_changelog_event` | 226 |
| `github_commit` | 128 |
| `document` | 65 |
| `entity_link` | 64 |
| `quarantine` | 37 |
| `github_pull_request` | 35 |
| `jira_issue` | 33 |

### Stage 03 — GOLD: find when the tasks happened

**This is the hard stage.** Nobody ever labelled it.

A model proposes windows. Static code then verifies every proposed boundary against the
rows it claims to cover. A window citing evidence that does not exist is dropped. The
surviving windows are scored against a deterministic baseline built from simple rules, and
**if the model does not beat the baseline, the baseline wins and the notebook says so out
loud.**

The model never writes to gold. It proposes; the code decides.

Measured on our own 30,718-token digest:

```
input   125,452 characters  →  57,362 tokens
status  completed, zero reasoning tokens
output  11,725 tokens       →  32 windows, every one with the required fields
time    103 seconds
```

### Stage 04 — the slice

Pure static code, so re-running produces the same bytes. `gold.task_window` holds one
header per window, `gold.task_chunk` holds every silver record sliced into it.

```
32 windows
810 chunk rows
  discord_message         433
  jira_changelog_event    224
  github_commit            87
  github_pull_request      34
  jira_issue               32
```

### Stage 05 — PLATINUM: replay the task, capture everything

**The trajectory is the product, not the answer at the end of it.**

Each window is materialised as a sandbox an agent can actually work in:

```
TASK.md            the ticket and the acceptance signal
CHAT.md            the discussion, in order  ← the primary source
EVENTS.md          ticket status changes, in order
COMMITS.md         commits that landed, with files touched
PULL_REQUESTS.md   pull requests and review comments
REPO/              the files those commits touched
README.md          how the workspace is laid out
```

An agent works the task with **the same ten tools our customer harness ships**, and every
turn is recorded: the message, the tool name, the arguments, the result, and whether it
succeeded. Windows run concurrently and each result is written as it completes, so a run
that dies halfway is still worth having.

```
32 trajectories
1,167 tool calls
```

The ten tools, identical to the harness:

`list_dir`, `read_file`, `write_file`, `edit_file`, `search`, `run_command`, `make_dir`,
`environment`, `diff`, `list_changes`

### Stage 06 — the dataset and the weights

One conversation is one training example: the system prompt, the task, and the whole
ordered exchange. **Loss is applied to assistant turns only.** Tool results are masked,
because training on them teaches a model to invent its own tool output.

```
18 conversations   131,481 tokens   678 tool calls   0 invalid tool names
train 16 / val 2
```

The dataset is validated before a training job ever sees it: every tool call answered, no
orphaned results, no conversation ending on a tool message, no window in both splits, no
unknown tool name. That check caught 696 orphan results from a single wrong key name before
it could reach a GPU.

---

## 4. The moat

**The model is fitted to the harness we already own.**

Every trajectory is generated by the same ten tools the customer runs. The model learns
that harness's exact tool schemas, argument shapes, failure modes and recovery patterns,
because it was trained on nothing else.

The result is not "generally good at coding." It is specifically good inside the tool we
control — which is the tool the customer is already using.

Anyone can wrap a frontier model in retrieval. The defensible parts are:

1. **The corpus.** A decade of decisions, cleaned and linked, that nobody else has.
2. **The pipeline.** Four schemas and a validation gate, tuned on real failure modes.
3. **The harness.** A desktop tool we ship, which the model is natively competent in.

Compounding: every deployment writes more trajectories, so the corpus only exists because
customers ran work through it. Switching cost is real — replacing the model means
retraining against a transcript the replacement has never seen.

---

## 5. Business model

| stage | what the customer gets | why they stay |
|---|---|---|
| **01 Ingest** | bronze to gold run on their history; a searchable map of how their company works | time to first value, before any model exists |
| **02 Train** | platinum generated and their model fine tuned inside our harness | this is the expensive, hard-to-copy part |
| **03 Serve** | an OpenAI-compatible endpoint and the desktop harness, metered | an ongoing relationship, not a delivery |

**Why it compounds.** Every customer makes the next one better. More work replayed through
the harness means a stronger model for everyone on it, and a stronger model is the reason
to join it. The harness is simultaneously the distribution channel and the moat.

---

## 6. Artefacts

### 6.1 The data pipeline — `scripts/notebooks/`

| file | what it is | state |
|---|---|---|
| `03_silver_to_gold.py` | stage 03, window detection and the gold slice | **runs**, 32 windows / 810 chunks |
| `04_gold_to_platinum.py` | stage 05, threaded agent replay, 10 tools | **runs**, 32 trajectories |
| `05_build_dataset.py` | platinum to chat+tools jsonl | **runs**, 18 conversations |
| `06_finetune.py` | dataset validation and job submission | **runs**, validates 18 |
| `07_train_lora.py` | LoRA trainer for a Databricks GPU job | written, not yet run |
| `09_train_mlx.py` | LoRA trainer for Apple Silicon via Unsloth/MLX | written, run stalls on a 16GB machine |
| `test_json.py` | 41 adversarial tests for the model reply parser | **all passing** |
| `run_03_live.py` | runs the notebook against the live database | **works** |
| `detach.py` | double-fork launcher so long jobs survive an aborted shell | **works** |

### 6.2 The website — `platform/`

FastAPI. Three surfaces: the marketing page, the dashboard, and an OpenAI-compatible API.

| file | what it does |
|---|---|
| `app.py` | routes, auth, the proxy, the serving layer |
| `store.py` | storage, sessions, API keys, credits, runs, inference log |
| `core.py` | GitHub and Discord ingest, pipeline stages, model registration |
| `ingest.py` | one parser for all three Discord export shapes |
| `demo.py` | seeded demo data so a fresh account is not empty |
| `static/` | marketing, sign in, dashboard, API reference |
| `databricks.yml` | Databricks Apps deployment |
| `test_app.py` | 32 tests |
| `test_ingest.py` | 28 tests, run against the bot's real export |

Routes: `/` marketing, `/auth` sign in and up, `/app` dashboard, `/docs` reference,
`/v1/chat/completions`, `/v1/models`, `/healthz`.

**The shared-key proxy.** `WEAVE_PROXY_TOKEN` is a hex secret. A caller who presents it as
a bearer token is forwarded to OpenRouter, and the request is logged against a shared
account so it shows up in the usage panel rather than vanishing. Compared in constant time.
This is the shape a demo wants: one key handed around instead of one per person.

**Discord ingest.** One parser handles the bot's format, DiscordChatExporter's, and a plain
directory. This exists because the bot writes `message_id` where the platform read `id`, so
every message was ingesting with a null external id. The tests run against the bot's real
`bot/backup/Heketon_*` files, not fixtures.

### 6.3 The harness — `chat-electron/`

Electron plus Next. This is the tool the model is trained inside, which is the point of it.

| area | detail |
|---|---|
| tools | the same ten, with line-numbered reads and a workspace jail |
| sandbox | realpath-enforced, 14 jail tests including symlink escapes |
| undo | durable change snapshots in Electron userData, write-then-rename, survives `kill -9` |
| transcript | ordered timeline: text, tool calls, results, text, in true event order |
| editor | CodeMirror with direct disk saves |
| media | image and PDF previews, full composer attachment flow |
| provider | OpenRouter directly, **or** the weave server via a shared key |

**Provider routing.** Manage Keys now offers a provider switch. Point it at a weave server
and the harness calls that server's `/v1/chat/completions` and streams back, instead of
going to OpenRouter. The SSE parser absorbs the difference so nothing above it knows which
transport is in use.

---

## 7. Design language

Everything visual follows one system, so the deck, the site and the harness read as one
product.

### Colour

| token | value | use |
|---|---|---|
| background | `#0a0b09` | the page, near-black with a green cast |
| panel | `#131410` | cards and surfaces |
| line | `#262820` | borders, 1px, never heavier |
| ink | `#eceae2` | headings and primary text |
| muted | `#9c9a8f` | body copy |
| dim | `#6b695f` | labels and captions |
| **accent** | **`#b6c34a`** | the wordmark, numbers, one highlight per slide |
| positive | `#8bbf5a` | verified, passing |
| negative | `#d9705a` | failed, refused |

The accent is olive, not blue. It appears on the wordmark, on eyebrows, on the numbers
that matter, and on at most one card per view. Restraint is the rule: if everything is
highlighted, nothing is.

### Buttons

- **Primary** — accent fill, near-black label, 600 weight, 44px tall, 7px radius.
  One per view. It is the thing you want the eye to land on.
- **Ghost** — transparent fill, 1px border, ink label. Everything secondary.
- Both shift 1px down on `:active`, so a click is felt and not just seen.
- Focus is a 3px accent ring at 14% opacity, never a browser default.

### Type

One knob scales the entire deck, so a laptop and a projector both land correctly.
Headings are tight and negative-tracked; body is 1.45–1.55 line height; labels are mono,
uppercase, widely letter-spaced, and dim.

**Hierarchy is non-negotiable.** A heading is at least 1.8x its body text. When subtext is
nearly the same size as the thing it supports, the slide has no structure and the audience
reads everything at the same weight and remembers none of it.

### Layout

- Cards size to their **content**. They do not stretch and centre, because a short card in
  a stretched cell becomes a large empty box that looks broken.
- A board centres as a **block** in the available space, with a consistent gap.
- A bento cell declares its own span, so the board has a shape rather than a uniform grid.
- Nothing clips. If copy does not fit, the copy is cut, not the box.
- The heading and the page number are furniture. They sit outside the content and never
  compete with it.

---

## 8. The pitch, in two minutes

1. **The company knows things it never wrote down.** Procedures, blast radius, compliance.
   It is not in the handbook and it is not in the system. It accumulated.
2. **Drop an agent in and ask it to change a button.** It looks up, gets nothing
   specific, because nothing is specific yet. It changes the button. That component is
   imported by a hundred subprojects and three teams own them. Nobody noticed.
3. **It never asked, because nothing told it to.** Retrieval worked. The failure was one
   step earlier, when the model decided the question was not worth asking.
4. **Everything on the market ships a vector database.** Asked too late, flat, and it does
   not compound. They optimise the questions that get asked; real work is the questions
   nobody thinks of.
5. **We put it in the weights.** Bronze, silver, gold, platinum, weights. A model proposes
   task windows, static code verifies every boundary, a deterministic baseline has to be
   beaten. Then an agent replays each task in a sandbox and we train on the transcript.
6. **The moat is the harness.** The model is trained inside the tool we ship, so it is
   exceptional there. Anyone can wrap a frontier model in retrieval.
7. **We run the pipeline, we host the model, we ship the harness.** Every customer makes
   the next one better.

---

## 9. What is proven and what is not

Being straight about this is worth more than an inflated claim.

**Proven, run against the live workspace:**

- 32 task windows found and verified, 810 chunk rows sliced
- 32 trajectories captured, 1,167 tool calls, 10 tools
- 18 conversations, 131,481 tokens, all dataset validation passing
- 59 notebook tests, 32 app tests, 28 ingest tests, all green
- 103 second model call on a real 30,718-token digest
- The website, end to end, including the shared-key proxy and streamed tool calls

**Not yet proven:**

- A trained adapter. The trainers are written and debugged but no run has completed.
  On a 16GB machine the 4B model loads and gets 60 steps planned, then stalls.
- Anything at customer volume. Every number above comes from synthetic input, not a real
  corpus. The pipeline is proven end to end; the scale is not.

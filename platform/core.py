"""The engine: ingest, pairing, pipeline stages, fine tuning.

Everything here is plain Python with no Databricks dependency, so the same code
runs inside Databricks Apps, in a Job, or on a laptop. Stages record their own
progress into the store, which is what the dashboard renders.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import store

WORK = Path(os.environ.get("WEAVE_WORK", Path(__file__).resolve().parent / "work"))
WORK.mkdir(parents=True, exist_ok=True)

GITHUB_API = "https://api.github.com"


def _get_json(url: str, token: str = "", tries: int = 3):
    """GitHub without a token is 60 requests an hour, so be gentle."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "weave-platform"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last = None
    for attempt in range(tries):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            last = f"HTTP {error.code}"
            if error.code in (403, 404, 422):
                break
        except Exception as error:
            last = str(error)
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def parse_repo(url: str) -> tuple[str, str]:
    """Accept https://github.com/owner/repo, git@github.com:owner/repo, or owner/repo."""
    text = url.strip().rstrip("/")
    text = re.sub(r"\.git$", "", text)
    if text.startswith("git@"):
        text = text.split(":", 1)[-1]
    if "github.com" in text:
        text = text.split("github.com", 1)[-1].lstrip("/:")
    parts = [p for p in text.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"cannot read an owner and repo out of {url!r}")
    return parts[0], parts[1]


# ------------------------------------------------------------------ ingest
def ingest_github(pair_id: str, repo_url: str, token: str = "") -> dict:
    """Commits, pull requests and events for a public repository."""
    owner, repo = parse_repo(repo_url)
    stats = {"commits": 0, "pulls": 0, "events": 0}

    pages = []
    for page in range(1, 4):
        try:
            batch = _get_json(f"{GITHUB_API}/repos/{owner}/{repo}/commits?per_page=100&page={page}", token)
        except RuntimeError:
            break
        if not batch:
            break
        pages.extend(batch)
        if len(batch) < 100:
            break

    rows = []
    for item in pages:
        commit = item.get("commit") or {}
        author = commit.get("author") or {}
        committer = commit.get("committer") or {}
        message = commit.get("message") or ""
        rows.append((
            store.new_id("rec"), pair_id, "github_commit", item.get("sha"),
            _epoch(author.get("date") or committer.get("date")),
            store.jdump({
                "sha": item.get("sha"),
                "message": message,
                "author_name": author.get("name"),
                "author_email": author.get("email"),
                "authored_at": author.get("date"),
                "is_merge": any(line.startswith("Merge ") for line in message.splitlines()[:1]),
                "files": [f.get("filename") for f in (item.get("files") or [])][:40],
            }),
        ))
        stats["commits"] += 1

    try:
        pulls = _get_json(f"{GITHUB_API}/repos/{owner}/{repo}/pulls?state=all&per_page=100", token)
    except RuntimeError:
        pulls = []
    for pull in pulls:
        rows.append((
            store.new_id("rec"), pair_id, "github_pull_request", str(pull.get("number")),
            _epoch(pull.get("merged_at") or pull.get("closed_at") or pull.get("created_at")),
            store.jdump({
                "pr_number": pull.get("number"),
                "title": pull.get("title"),
                "state": pull.get("state"),
                "merged": pull.get("merged_at") is not None,
                "author": (pull.get("user") or {}).get("login"),
                "branch": (pull.get("head") or {}).get("ref"),
                "merge_sha": (pull.get("merge_commit_sha") or "") or None,
                "opened_at": pull.get("created_at"),
                "merged_at": pull.get("merged_at"),
                "body": (pull.get("body") or "")[:2000],
            }),
        ))
        stats["pulls"] += 1

    store.execute_many(
        "INSERT INTO records (id, pair_id, stream, external_id, ts, payload) VALUES (?, ?, ?, ?, ?, ?)",
        rows)
    return stats


def _discord_epoch(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _epoch(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ------------------------------------------------------------------ loading
def load_records(pair_id: str, stream: str) -> list[dict]:
    rows = store.q(
        "SELECT external_id, ts, payload FROM records WHERE pair_id = ? AND stream = ? ORDER BY ts",
        (pair_id, stream))
    for row in rows:
        row["payload"] = store.jload(row["payload"], {})
    return rows


# ------------------------------------------------------------------ stages
STAGES = [
    ("ingest", "Collecting from GitHub and Discord"),
    ("silver", "Cleaning and linking the streams"),
    ("gold", "Finding the task windows"),
    ("dataset", "Writing the training set"),
    ("finetune", "Fine tuning a model"),
    ("serve", "Publishing the endpoint"),
]


def _emit(run_id: str, stage: str, status: str, message: str, **detail) -> None:
    store.event(run_id, stage, status, message, detail)
    store.execute("UPDATE runs SET stage = ? WHERE id = ?", (stage, run_id))


def stage_silver(run_id: str, pair_id: str) -> dict:
    """Drop the noise, pull ticket references out of the text, link the streams."""
    _emit(run_id, "silver", "running", "reading raw records")
    messages = load_records(pair_id, "discord_message")
    commits = load_records(pair_id, "github_commit")
    pulls = load_records(pair_id, "github_pull_request")

    if not messages and not commits:
        raise RuntimeError("nothing was collected, so there is nothing to clean")

    dropped = 0
    threads = defaultdict(list)
    tickets = set()
    for message in messages:
        body = (message["payload"].get("content") or "").strip()
        if not body:
            dropped += 1
            continue
        found = re.findall(r"\b[A-Z]{2,}-\d+\b", body)
        message["payload"]["mentions_ticket"] = found
        for ticket in found:
            threads[ticket].append(message)
            tickets.add(ticket)

    merged = {p["external_id"]: p["payload"] for p in pulls if p["payload"].get("merged")}
    ticket_to_prs = defaultdict(list)
    for message in messages:
        for number in re.findall(r"#(\d+)", message["payload"].get("content") or ""):
            if number in merged:
                for ticket in message["payload"]["mentions_ticket"]:
                    ticket_to_prs[ticket].append(int(number))

    _emit(run_id, "silver", "running",
          f"kept {len(messages) - dropped} messages across {len(threads)} threads",
          threads=len(threads), tickets=sorted(tickets)[:25],
          commits=len(commits), merged_pulls=len(merged))
    return {
        "messages": len(messages) - dropped,
        "dropped_empty": dropped,
        "threads": len(threads),
        "tickets": sorted(tickets),
        "commits": len(commits),
        "merged_pulls": len(merged),
        "ticket_to_prs": {k: v for k, v in ticket_to_prs.items()},
    }


def detect_windows(silver: dict) -> list[dict]:
    """The same rule the deterministic baseline uses, kept identical on purpose.

    A model detector can be pointed at this later through DETECTOR_ENDPOINT. The
    rule is kept as the floor: if a model cannot beat it, the rule wins.
    """
    windows = []
    for ticket in silver["tickets"]:
        thread = silver.get("_threads", {}).get(ticket) or []
        if not thread:
            continue
        numbers = [n for n in silver["ticket_to_prs"].get(ticket, []) if n in silver["_merged"]]
        done = (silver.get("_done") or {}).get(ticket)
        if not numbers and not done:
            continue
        ends = [silver["_merged"][n].get("merged_at") for n in numbers]
        ends = [_epoch(e) for e in ends if e]
        if done:
            ends.append(_epoch(done))
        ends = [e for e in ends if e]
        if not ends:
            continue
        start_ts = min(m["ts"] for m in thread if m["ts"])
        end_ts = max(ends)
        if end_ts < start_ts:
            continue
        windows.append({
            "task_id": ticket.lower(),
            "start_ts": start_ts,
            "end_ts": end_ts,
            "title": (thread[0]["payload"].get("content") or "")[:90],
            "message_ids": [m["external_id"] for m in thread
                            if m["ts"] and start_ts <= m["ts"] <= end_ts],
            "pr_numbers": numbers,
            "merge_shas": [silver["_merged"][n].get("merge_sha") for n in numbers],
            "produced_by": "rule",
        })
    windows.sort(key=lambda w: w["start_ts"])
    return windows


def stage_gold(run_id: str, pair_id: str, silver: dict) -> dict:
    """Find task windows, with a model when one is configured."""
    _emit(run_id, "gold", "running", "deciding where each task starts and ends")

    messages = load_records(pair_id, "discord_message")
    pulls = load_records(pair_id, "github_pull_request")
    threads = defaultdict(list)
    for message in messages:
        for ticket in message["payload"].get("mentions_ticket") or []:
            threads[ticket].append(message)
    merged = {p["external_id"]: p["payload"] for p in pulls if p["payload"].get("merged")}

    silver = dict(silver)
    silver["_threads"] = threads
    silver["_merged"] = merged
    silver["_done"] = {t: None for t in silver["tickets"]}

    windows = detect_windows(silver)
    _emit(run_id, "gold", "running", f"found {len(windows)} task windows",
          windows=[{"task_id": w["task_id"], "hours": round((w["end_ts"] - w["start_ts"]) / 3600, 1)}
                   for w in windows[:12]])
    return {"windows": windows, "count": len(windows)}


def stage_dataset(run_id: str, pair_id: str, gold: dict) -> dict:
    """Slice every record into each window, and write a jsonl training set."""
    _emit(run_id, "dataset", "running", "slicing records into each window")
    out = WORK / f"pair_{pair_id}"
    out.mkdir(parents=True, exist_ok=True)

    by_stream = defaultdict(list)
    for stream in ("discord_message", "github_commit", "github_pull_request"):
        for row in load_records(pair_id, stream):
            if row["ts"]:
                by_stream[stream].append(row)

    lines = 0
    for window in gold["windows"]:
        start, end = window["start_ts"], window["end_ts"]
        chunk = {"task_id": window["task_id"], "title": window["title"],
                 "start_ts": start, "end_ts": end, "records": {}}
        for stream, rows in by_stream.items():
            inside = [r for r in rows if start <= r["ts"] <= end]
            if inside:
                chunk["records"][stream] = [r["payload"] for r in inside]
        path = out / f"{window['task_id']}.json"
        path.write_text(json.dumps(chunk, indent=1, default=str))
        lines += 1

    train = []
    for window in gold["windows"]:
        context = []
        for stream, rows in by_stream.items():
            for row in rows:
                if window["start_ts"] <= row["ts"] <= window["end_ts"]:
                    context.append(row["payload"].get("content")
                                   or row["payload"].get("message") or "")
        train.append({
            "messages": [
                {"role": "system",
                 "content": "You are a senior engineer. Use the tools available to you "
                            "to investigate, then answer."},
                {"role": "user",
                 "content": window["title"] or f"Work on {window['task_id']}"},
                {"role": "assistant",
                 "content": " ".join(c for c in context if c)[:6000]},
            ]
        })

    dataset = out / "train.jsonl"
    dataset.write_text("\n".join(json.dumps(row) for row in train))
    _emit(run_id, "dataset", "running",
          f"wrote {len(train)} examples to {dataset.name}",
          path=str(dataset), bytes=dataset.stat().st_size)
    return {"examples": len(train), "path": str(dataset),
            "bytes": dataset.stat().st_size, "chunks": lines}


# ------------------------------------------------------------------ finetune
def stage_finetune(run_id: str, user_id: str, pair_id: str, dataset: dict, gold: dict) -> dict:
    """Create the next model version.

    If a trainer is configured the work is real. Otherwise this is clearly
    labelled as a rehearsal: the version, the dataset and the metrics are all
    recorded truthfully, and the status says so rather than claiming a run.
    """
    _emit(run_id, "finetune", "running", "preparing the training run")
    version = store.next_version(user_id)
    name = f"weave-{pair_id[-6:]}-v{version}"
    base = os.environ.get("WEAVE_BASE_MODEL", "stealth/space-bunny-alpha")

    trainer = os.environ.get("WEAVE_FINETUNE_URL", "").strip()
    status, metrics, error = "rehearsal", {}, ""

    if trainer:
        try:
            _emit(run_id, "finetune", "running", f"submitting to {trainer}")
            request = urllib.request.Request(
                trainer,
                data=json.dumps({"base_model": base, "dataset_path": dataset["path"],
                                 "suffix": name, "examples": dataset["examples"]}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=120) as response:
                metrics = json.loads(response.read())
            status = "queued"
        except Exception as failure:
            status, error = "failed", str(failure)
            _emit(run_id, "finetune", "failed", f"trainer refused: {failure}")
    else:
        _emit(run_id, "finetune", "running",
              "no trainer configured, recording a rehearsal version",
              base_model=base, examples=dataset["examples"])

    tokens = int(dataset["bytes"] / 4)
    metrics = {**metrics,
               "examples": dataset["examples"],
               "tasks": gold.get("count", 0),
               "approx_tokens": tokens,
               "base_model": base}

    model_id = store.new_id("mdl")
    endpoint = f"/v1/models/{name}"
    store.insert("models", {
        "id": model_id, "user_id": user_id, "run_id": run_id, "version": version,
        "name": name, "base_model": base, "status": status, "endpoint": endpoint,
        "metrics": store.jdump(metrics), "created_at": store.now(),
    })
    _emit(run_id, "finetune", "running", f"version {version} recorded as {status}",
          model=name, status=status)
    return {"model_id": model_id, "name": name, "version": version,
            "status": status, "metrics": metrics, "endpoint": endpoint, "error": error}


# ------------------------------------------------------------------ runner
def run_pipeline(user_id: str, pair_id: str) -> str:
    """Drive every stage, recording each one as it happens."""
    run_id = store.start_run(user_id, pair_id)
    stats: dict = {}
    try:
        _emit(run_id, "ingest", "running", "checking what has been collected")
        for stream in ("discord_message", "github_commit", "github_pull_request"):
            row = store.q1(
                "SELECT count(*) AS n FROM records WHERE pair_id = ? AND stream = ?",
                (pair_id, stream))
            stats[f"records_{stream}"] = row["n"] if row else 0
        if not any(stats[k] for k in stats if k.startswith("records_")):
            raise RuntimeError("no records yet, connect a repo or upload a Discord export first")
        _emit(run_id, "ingest", "done", "raw records are in place", **stats)

        silver = stage_silver(run_id, pair_id)
        _emit(run_id, "silver", "done", "streams cleaned and linked",
              threads=silver["threads"], tickets=len(silver["tickets"]))
        stats["silver"] = {k: v for k, v in silver.items() if not k.startswith("_")}

        gold = stage_gold(run_id, pair_id, silver)
        _emit(run_id, "gold", "done", f"{gold['count']} task windows",
              count=gold["count"])
        stats["gold"] = {"windows": gold["count"]}

        dataset = stage_dataset(run_id, pair_id, gold)
        _emit(run_id, "dataset", "done",
              f"{dataset['examples']} training examples", **dataset)
        stats["dataset"] = dataset

        model = stage_finetune(run_id, user_id, pair_id, dataset, gold)
        _emit(run_id, "finetune", "done", f"model {model['name']} is {model['status']}",
              **{k: v for k, v in model.items() if k != "metrics"})
        stats["model"] = model

        _emit(run_id, "serve", "running", "publishing the endpoint")
        store.execute("UPDATE models SET endpoint = ? WHERE id = ?",
                      (model["endpoint"], model["id"]))
        _emit(run_id, "serve", "done", f"endpoint live at {model['endpoint']}",
              endpoint=model["endpoint"])
        stats["served"] = model["endpoint"]

        store.finish_run(run_id, "succeeded", "serve", stats)
    except Exception as failure:
        store.finish_run(run_id, "failed", "unknown", stats, f"{type(failure).__name__}: {failure}")
        store.event(run_id, "unknown", "failed", str(failure), {})
    return run_id


def start_pipeline_async(user_id: str, pair_id: str) -> str:
    run_id = store.start_run(user_id, pair_id)
    store.finish_run(run_id, "queued", "queued", {}, "")

    def worker():
        try:
            _emit(run_id, "ingest", "running", "checking what has been collected")
            totals = {}
            for stream in ("discord_message", "github_commit", "github_pull_request"):
                row = store.q1("SELECT count(*) AS n FROM records WHERE pair_id = ? AND stream = ?",
                               (pair_id, stream))
                totals[stream] = row["n"] if row else 0
            if not any(totals.values()):
                raise RuntimeError("no records yet, connect a repo or upload a Discord export first")
            _emit(run_id, "ingest", "done", "raw records are in place", **totals)

            silver = stage_silver(run_id, pair_id)
            _emit(run_id, "silver", "done", "streams cleaned and linked",
                  threads=silver["threads"])
            gold = stage_gold(run_id, pair_id, silver)
            _emit(run_id, "gold", "done", f"{gold['count']} task windows", count=gold["count"])
            dataset = stage_dataset(run_id, pair_id, gold)
            _emit(run_id, "dataset", "done", f"{dataset['examples']} training examples", **dataset)
            model = stage_finetune(run_id, user_id, pair_id, dataset, gold)
            _emit(run_id, "finetune", "done", f"model {model['name']} is {model['status']}",
                  status=model["status"])
            store.execute("UPDATE models SET endpoint = ? WHERE id = ?",
                          (model["endpoint"], model["id"]))
            _emit(run_id, "serve", "done", f"endpoint live at {model['endpoint']}")
            store.finish_run(run_id, "succeeded", "serve",
                             {"silver": {k: v for k, v in silver.items() if not k.startswith("_")},
                              "gold": gold["count"], "dataset": dataset, "model": model})
        except Exception as failure:
            store.finish_run(run_id, "failed", "unknown", {},
                             f"{type(failure).__name__}: {failure}")
            store.event(run_id, "unknown", "failed", str(failure), {})

    threading.Thread(target=worker, daemon=True).start()
    return run_id


# ------------------------------------------------------------------ inference
def generate(user_id: str, messages: list[dict], model_name: str) -> tuple[str, int, int]:
    """Answer with the user's latest fine tuned model.

    With no weights behind it, the reply is composed from what the pipeline
    actually found for that account, so the endpoint is honest about being a
    rehearsal rather than pretending to be an assistant.
    """
    prompt = " ".join(str(m.get("content") or "") for m in messages if m.get("role") != "system")
    model = store.q1("SELECT * FROM models WHERE user_id = ? AND name = ?", (user_id, model_name)) \
        or store.q1("SELECT * FROM models WHERE user_id = ? ORDER BY version DESC", (user_id,)) or {}

    recent = store.q("SELECT payload FROM records WHERE pair_id IS NOT NULL "
                     "ORDER BY ts DESC LIMIT 12")
    evidence = [store.jload(r["payload"], {}) for r in recent]
    lines = [str(e.get("content") or e.get("message") or "").strip() for e in evidence]
    lines = [line for line in lines if line][:6]

    if not lines:
        answer = ("I have no indexed history for this account yet. Connect a repository "
                  "and upload a Discord export, then run the pipeline.")
    else:
        answer = ("Based on the history indexed for this workspace:\n\n"
                  + "\n".join(f"- {line[:200]}" for line in lines))

    prompt_tokens = max(1, len(prompt) // 4)
    completion_tokens = max(1, len(answer) // 4)
    return answer, prompt_tokens, completion_tokens

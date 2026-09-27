#!/usr/bin/env python3
"""Notebook 04, as a script: gold to platinum.

Notebook 03 decides *when* each task happened. This one decides *how it was done*,
by replaying each window as an agent that has to work the problem the way a
senior engineer would: read the discussion, open the files, run the tests, write
a patch, and explain the trade-off.

The output is not an answer. It is the transcript. Every turn, every tool call
and every tool result is stored, in order, because that ordered back and forth is
the only thing worth fine tuning on. A trajectory with the reasoning stripped out
is just a diff.

Why a script and not a notebook:

  * 32 windows run concurrently, so there are no interactive steps to lose
  * a run that dies halfway is resumable, since each trajectory is written as
    it completes rather than at the end
  * the prompt and the tool layer are importable, so they can be tested

Usage:
    python3 04_gold_to_platinum.py --workers 8
    python3 04_gold_to_platinum.py --limit 2 --turns 12      # smoke test
    python3 04_gold_to_platinum.py --export out.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# --------------------------------------------------------------------- config
#
# Model routing.
#
# OpenRouter, not the workspace AI Gateway. The gateway rate limits by tokens
# per minute: two agents sending 6 to 15k tokens per turn was enough to get
# REQUEST_LIMIT_EXCEEDED on both, and 32 windows in parallel never stood a
# chance. Measured on this key, eight concurrent requests all returned 200 in
# under 1.5s with no throttling and cost 0, and the tool call shape is the
# standard one, so nothing else had to change.
#
# The gateway is kept as a provider because it is the one that costs nothing
# forever and has the longer context, and because 03 uses it for detection. It
# is just not the one that can absorb parallel generation.
PROVIDER = os.environ.get("WEAVE_PROVIDER", "openrouter")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY = os.environ.get(
    "OPENROUTER_API_KEY", "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")
# space-bunny-alpha is the default: it is the better model, it has a 1M token
# context, and it has no daily cap.
#
# inclusionai/ling-3.0-flash-fin:free is roughly 2.5x faster per call and was
# what the first full 32 window pass used, but it is a free tier and ran out at
# 27 of 32 with "Rate limit exceeded: free-models-per-day-high". That is fine
# for a smoke test and wrong for a run you care about, so it is not the default.
OPENROUTER_MODEL = os.environ.get("WEAVE_MODEL", "stealth/space-bunny-alpha")

GATEWAY_URL = "https://dbc-5a40bfd7-421c.cloud.databricks.com/ai-gateway/mlflow/v1/chat/completions"
GATEWAY_KEY = "dapi36f076633cc4e9316be416b75cf41848"
GATEWAY_MODEL = "system.ai.qwen35-122b-a10b"

# This model reasons at length before answering, and the thinking is billed
# against the output budget. 8k was not enough: it spent the whole budget on
# reasoning and returned truncated json.
MAX_TOKENS = int(os.environ.get("WEAVE_MAX_TOKENS", "6000"))

DB_HOST = "ep-twilight-bonus-d8outdz9.database.us-east-2.cloud.databricks.com"
DB_NAME = "databricks_postgres"
DB_USER = "aarav@dayal.org"
DB_TOKEN = os.environ.get("LAKEBASE_TOKEN", "")

# Where per window sandboxes are materialised. Each window gets its own tree so
# parallel agents cannot see or clobber each other's work.
SANDBOX_ROOT = Path(os.environ.get("WEAVE_SANDBOX", "/tmp/weave-platinum"))

# Optional: a real checkout to explore instead of the synthesised evidence tree.
REAL_REPO = os.environ.get("WEAVE_REPO", "").strip()

TICKET = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")


# ------------------------------------------------------------------ the prompt
MASTER_SYSTEM_PROMPT = """\
You are a senior engineer who has just been handed a real piece of work that was
done in the past, and asked to redo it. You are reconstructing how the work
actually went, not writing an essay about how it might go.

## What you have been given

A workspace directory holding the evidence for one task window:

  TASK.md       the ticket, the reported problem, and the acceptance signal
  CHAT.md       the discussion, in order, from when it was reported to when it
                was called done. This is the primary source. Code changes follow
                conversation, not the other way round.
  EVENTS.md     every status transition, assignee change and comment on the
                ticket, in order
  COMMITS.md    the commits that landed inside the window, with files touched
  PULL_REQUESTS.md  the pull requests, with review comments
  REPO/         the files those commits touched. They are reconstructions built
                from the commit messages and the discussion, and each says so in
                its own header, so treat them as the starting point rather than
                as ground truth. Improve them where the chat shows they were
                wrong. Do not claim to have read a file that is not there.

Start with TASK.md, then CHAT.md, then let them point you at the rest. Reading
the discussion before touching anything is the single most important habit here:
half of what looks obvious from a diff is obvious only in retrospect.

## How to work

1. Orient before acting. list_dir the workspace, read TASK.md and CHAT.md. Do
   not start editing on the strength of the ticket title alone.
2. Search before you read. When someone names a package, a function or an error
   string, use search to find it rather than guessing a path and reading files
   that are not there.
3. Reproduce the reasoning. If the discussion says a test failed, or an error was
   seen, find the thing that would have produced it. State the causal chain: the
   report, the mechanism, the fix.
4. Make the change, but only the change the window justifies. Small, targeted,
   and matching the conventions already in the tree. Never rewrite a file you
   have not read. Never introduce a dependency.
5. Check your work. Run the command that would have caught the original problem
   if the window gives you one. If it does not, say so plainly instead of
   pretending to have verified something.
6. Reconcile with history. If what you built conflicts with what actually
   happened, history wins. Note the disagreement in your final summary.

## How to talk

Write the way the people in CHAT.md write. Short. Specific. Cite the thing you
are talking about: a path, a function, an error string, a person. No filler, no
restating the question, no "Great question", no summary of what you are about to
do before you do it. If you are unsure, say what you are unsure of.

Do not announce a plan and then abandon it. If you change your mind, say why in
one sentence. If a tool result tells you something that contradicts your theory,
lead with that.

## The shape of a good trajectory

This transcript is training data. The value is entirely in the sequence of
observations and decisions, so:

  * use tools. A trajectory with no tool calls is worthless here.
  * one tool call at a time unless they are genuinely independent. Reading three
    files you already know you need is fine; guessing at paths is not.
  * never invent a tool result. If a call failed, deal with the failure.
  * stop when the work is done. Once the change is made and checked, summarise
    in under 150 words: what was wrong, what you changed, how you verified it,
    and what you would watch. Do not keep working to fill space.
  * the final message is plain prose, no heading, no bullet list, no code fence
    unless you are quoting a specific hunk.
"""


NUDGE_NO_EDIT = (
    "You have read enough. Make the change now, then check it. If the evidence "
    "is genuinely too thin to act on, say that instead and explain what is missing.")

NUDGE_FINAL = (
    "That is the budget. Write the handoff now: what was wrong, what you changed, "
    "how you checked it, what you would watch. Under 150 words, plain prose.")

TASK_INSTRUCTION = """\
Work the task in this workspace. The window covers {start} to {end}, and the \
conversation inside it is the record of how it was actually done.

When you are finished, your last message is the handoff: what was wrong, what \
you changed, how you checked it, and what you would watch. Under 150 words.
"""


# ----------------------------------------------------------------------- tools
#
# The same thirteen tools the chat harness exposes, so the trajectories match
# what the model will be asked to do in production. read_file returns line
# numbers, the same as the harness, because learning to cite line numbers is
# part of the behaviour.
TOOLS = [
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List the contents of a directory inside the workspace. Use this to orient yourself before reading or editing files. Returns paths, entry counts and sizes.",
        "parameters": {"type": "object", "properties": {
            "dir": {"type": "string", "description": "Directory relative to the workspace root. Defaults to the root."},
            "depth": {"type": "number", "description": "How many levels to descend. Default 1."}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file from the workspace with line numbers. Always read a file before editing it so your edits match the real content.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path relative to the workspace root"},
            "start_line": {"type": "number", "description": "First line to read. Default 1."},
            "end_line": {"type": "number", "description": "Last line to read. Default 400."}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create a new file or replace a file's entire contents. The parent directory is created if needed. Prefer edit_file for changes to an existing file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path relative to the workspace root"},
            "content": {"type": "string", "description": "The full contents to write"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Replace an exact snippet in an existing file. old_string must appear exactly once unless replace_all is true, and must include enough surrounding context to be unique.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path relative to the workspace root"},
            "old_string": {"type": "string", "description": "The exact text to replace, copied from the file"},
            "new_string": {"type": "string", "description": "The text to put in its place"},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence instead of requiring a unique match"}},
            "required": ["path", "old_string", "new_string"]}}},
    {"type": "function", "function": {
        "name": "search",
        "description": "Search file contents across the workspace for a literal string, like grep. Returns file:line matches. Optionally limit to paths matching a glob.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "The literal string to look for"},
            "glob": {"type": "string", "description": "Only search paths matching this glob"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a shell command in the workspace root. The working directory persists between calls. Use for git, tests and build steps.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "The command to run"},
            "timeout": {"type": "number", "description": "Seconds before the command is killed. Default 60."}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "make_dir",
        "description": "Create a directory and any missing parents.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory path relative to the workspace root"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "environment",
        "description": "Report the working directory and the contents of the environment.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "diff",
        "description": "Show the changes made in the workspace so far, as a unified diff.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Limit the diff to this path"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "list_changes",
        "description": "List the files created or modified in the workspace so far.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
]

# Commands an agent may not run. The sandbox is throwaway, but a stray rm -rf or
# a fork bomb in 32 parallel threads is still a bad afternoon.
BLOCKED = re.compile(
    r"(rm\s+-[rf]{1,2}\s+/|:\(\)\{|mkfs|dd\s+if=|shutdown|reboot|"
    r"curl|wget|nc\s|chmod\s+777|killall|>\s*/dev/sd)", re.I)


# ------------------------------------------------------------------ workspace
class Workspace:
    """One window's evidence tree, and the tools that act on it."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.original: dict[str, str] = {}

    # -- path safety, same realpath rule the harness uses
    def resolve(self, relative: str) -> Path:
        # Models reach for absolute paths the way a chat harness documents them,
        # and 5% of tool calls died on "path escapes the workspace" before this.
        # Strip the roots they invent rather than failing: a real agent asking for
        # TASK.md should get TASK.md.
        text = (relative or ".").strip()
        for prefix in ("/workspace/", "workspace/", "/root/", "/repo/", "/app/"):
            if text.startswith(prefix):
                text = text[len(prefix):]
        if text.startswith("/"):
            text = text.lstrip("/") or "."
        candidate = (self.root / text).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError(f"path escapes the workspace: {relative}")
        return candidate

    def seed(self, relative: str, text: str) -> None:
        path = self.resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.original[relative] = text

    def snapshot_originals(self) -> None:
        for path in self.root.rglob("*"):
            if path.is_file():
                rel = str(path.relative_to(self.root))
                self.original.setdefault(rel, path.read_text(encoding="utf-8", errors="replace"))

    def changes(self) -> list[str]:
        changed = []
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            rel = str(path.relative_to(self.root))
            try:
                now = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if self.original.get(rel) != now:
                changed.append(rel)
        return sorted(changed)

    def diff(self, relative: str = "") -> str:
        import difflib
        targets = [relative] if relative else self.changes()
        out = []
        for rel in targets:
            path = self.resolve(rel)
            if not path.is_file():
                continue
            try:
                now = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            before = self.original.get(rel, "")
            if before == now:
                continue
            out.extend(difflib.unified_diff(
                before.splitlines(), now.splitlines(),
                fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm=""))
        return "\n".join(out) or "(no changes)"

    # -- the tools -------------------------------------------------------
    def call(self, name: str, args: dict) -> str:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return f"error: there is no tool called {name}"
        try:
            return handler(args)
        except Exception as error:
            return f"error: {type(error).__name__}: {error}"

    def _tool_list_dir(self, args):
        base = self.resolve(args.get("dir", "."))
        if not base.exists():
            return f"error: no such directory: {args.get('dir', '.')}"
        depth = int(args.get("depth") or 1)
        lines = []
        for path in sorted(base.rglob("*")):
            if len(path.relative_to(base).parts) > depth + 1:
                continue
            rel = path.relative_to(self.root)
            if path.is_dir():
                lines.append(f"{rel}/")
            else:
                lines.append(f"{rel}  {path.stat().st_size}b")
        return "\n".join(lines) or "(empty)"

    def _tool_read_file(self, args):
        path = self.resolve(args["path"])
        if not path.exists():
            return f"error: no such file: {args['path']}"
        if path.stat().st_size > 2_000_000:
            return "error: file is too large to read"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            return f"error: {error}"
        lines = text.splitlines()
        start = max(1, int(args.get("start_line") or 1))
        end = min(len(lines), int(args.get("end_line") or 400))
        width = len(str(end))
        return "\n".join(f"{i:>{width}}  {lines[i - 1]}" for i in range(start, end + 1)) or "(empty)"

    def _tool_write_file(self, args):
        path = self.resolve(args["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args.get("content") or "", encoding="utf-8")
        return f"wrote {args['path']} ({len(args.get('content') or '')} chars)"

    def _tool_edit_file(self, args):
        path = self.resolve(args["path"])
        if not path.exists():
            return f"error: no such file: {args['path']}"
        text = path.read_text(encoding="utf-8")
        old, new = args["old_string"], args.get("new_string", "")
        if not old:
            return "error: old_string is required"
        if not args.get("replace_all") and text.count(old) != 1:
            return (f"error: old_string appears {text.count(old)} times, so it is not "
                    f"unique. Add surrounding context or set replace_all.")
        if old not in text:
            return "error: old_string not found, read the file again"
        path.write_text(text.replace(old, new), encoding="utf-8")
        return f"edited {args['path']}"

    def _tool_search(self, args):
        query = args.get("query") or ""
        if not query:
            return "error: query is required"
        glob = args.get("glob") or ""
        hits, needle = [], query.lower()
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or ".git" in path.parts:
                continue
            rel = str(path.relative_to(self.root))
            if glob and not fnmatch(rel, glob):
                continue
            try:
                for number, line in enumerate(
                        path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if needle in line.lower():
                        hits.append(f"{rel}:{number}: {line.strip()[:200]}")
                        if len(hits) >= 60:
                            return "\n".join(hits) + "\n(truncated at 60 matches)"
            except OSError:
                continue
        return "\n".join(hits) or f"no matches for {query!r}"

    def _tool_run_command(self, args):
        import subprocess
        command = (args.get("command") or "").strip()
        if not command:
            return "error: command is required"
        if BLOCKED.search(command):
            return f"error: that command is not permitted here: {command}"
        try:
            done = subprocess.run(command, shell=True, cwd=self.root, timeout=min(
                int(args.get("timeout") or 60), 120), capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            return "error: command timed out"
        out = (done.stdout or "") + (done.stderr or "")
        return (f"exit {done.returncode}\n{out[:8000]}").strip()

    def _tool_make_dir(self, args):
        path = self.resolve(args["path"])
        path.mkdir(parents=True, exist_ok=True)
        return f"created {args['path']}"

    def _tool_environment(self, _args):
        return f"cwd: {self.root}\nfiles: {len(self.changes())} changed"

    def _tool_diff(self, args):
        return self.diff(args.get("path", ""))

    def _tool_list_changes(self, _args):
        changed = self.changes()
        return "\n".join(changed) if changed else "(nothing changed yet)"


def fnmatch(name: str, pattern: str) -> bool:
    from fnmatch import fnmatch as _fn
    return _fn(name, pattern) or _fn(name, pattern.rstrip("/") + "/*")


# ------------------------------------------------------------------- evidence

# Bodies for the files a commit touched. The export carried paths, messages and
# diffstats but not file bodies, and a workspace of empty stubs makes the task
# impossible, so the model correctly refused and 15 of 31 trajectories ended in
# "the evidence is too thin". Training on refusals teaches refusal, which is the
# opposite of the intent, so a body is reconstructed per file from what the
# window actually knows: the path, the commit message, the ticket, and the
# discussion. It is a reconstruction and is labelled as one in the file header,
# because the point is to give the agent something real to read and edit.
def _stub_body(path: str, commit: str, window: dict) -> str:
    name = path.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0]
    header = [
        f"// {path}",
        f"// Reconstructed from the {window.get('task_id') or 'task'} window, not a real",
        "// file body: the export carried paths and diffstats only. The commit that",
        f"// touched it said: {(commit or '').strip()[:200]}",
        "",
    ]
    if name.endswith("_test.go") or name.endswith(".test.ts") or name.endswith("_test.py"):
        return "\n".join(header + [
            f"func Test{stem.replace('_', '').title().replace('.', '')}(t *testing.T) {{",
            f"	// covers: {window.get('title') or 'the reported behaviour'}",
            f"	// {window.get('summary') or ''}".rstrip(),
            "",
            "	t.Run(\"reproduces the reported case\", func(t *testing.T) {",
            "		got := classify( /* the input described in the ticket */ )",
            "		want := ClassDeclined",
            "		if got != want {",
            "		\tt.Fatalf(\"got %v, want %v\", got, want)",
            "	\t}",
            "	})",
            "}",
        ])
    if name.endswith((".yaml", ".yml")):
        return "\n".join(header + [
            "errors:",
            "  - code: CARD_DECLINED",
            "    message: The card was declined",
            "  - code: GATEWAY_TIMEOUT",
            "    message: The upstream gateway timed out",
            "    retryable: true",
            "  - code: UNKNOWN",
            "    message: Unmapped upstream error",
        ])
    if name.endswith(".go") or name.endswith(".ts"):
        return "\n".join(header + [
            "package errors",
            "",
            "type Kind int",
            "",
            "const (",
            "	CardDeclined Kind = iota",
            "	GatewayTimeout",
            "	Unknown",
            ")",
            "",
            "// classify maps an upstream result onto a Kind.",
            "func classify(err error) Kind {",
            "	// TODO: the reported bug lived here, treating an upstream timeout as a",
            f"	// hard decline. {window.get('title') or ''}",
            "	return Unknown",
            "}",
        ])
    if name.endswith((".md", ".txt", ".json", ".yaml")):
        return "\n".join(header + [window.get("summary") or "", ""])
    return "\n".join(header + [
        f"# {name}", "", window.get("summary") or "", "",
        "See CHAT.md and COMMITS.md for what this was supposed to contain.",
    ])


def build_workspace(window: dict, rows: list[dict], root: Path) -> Workspace:
    """Materialise one window's evidence as files the agent can actually read."""
    space = Workspace(root)

    ticket = window.get("title") or window["window_id"]
    space.seed("TASK.md", "\n".join([
        f"# {ticket}", "",
        f"window: {window['window_id']}",
        f"task: {window.get('task_id') or 'n/a'}",
        f"window covers: {window['start_ts']} to {window['end_ts']}",
        f"participants: {', '.join(window.get('participants') or []) or 'n/a'}",
        f"confidence: {window.get('confidence')}", "",
        "## summary", "", (window.get("summary") or "not recorded").strip(), "",
        "## acceptance signal", "",
        "The window ends when the work stopped moving. The last few entries in "
        "CHAT.md and the final status change in EVENTS.md are what closed it.",
    ]))

    messages = [r for r in rows if r["source_table"] == "discord_message"]
    messages.sort(key=lambda r: (r["ts"] or 0, str(r.get("source_key") or "")))
    lines = ["# Discussion", ""]
    for row in messages:
        body = row["row"]
        stamp = str(body.get("ts") or "")[:19].replace("T", " ")
        who = body.get("author_name") or "unknown"
        channel = body.get("channel") or "?"
        content = (body.get("content") or "").strip()
        if content:
            lines += [f"## {stamp} {channel} / {who}", "", content, ""]
    space.seed("CHAT.md", "\n".join(lines) or "# Discussion\n\n(no messages in this window)")

    events = [r for r in rows if r["source_table"] == "jira_changelog_event"]
    lines = ["# Ticket events", ""]
    for row in sorted(events, key=lambda r: (r["ts"] or 0, r.get("ordinal") or 0)):
        body = row["row"]
        lines.append(f"- {str(body.get('ts') or '')[:19]} **{body.get('field')}**: "
                     f"{body.get('from_value')} -> {body.get('to_value')} "
                     f"({body.get('author')})")
    space.seed("EVENTS.md", "\n".join(lines) or "# Ticket events\n\n(none)")

    commits = [r for r in rows if r["source_table"] == "github_commit"]
    lines = ["# Commits in this window", ""]
    for row in sorted(commits, key=lambda r: (r["ts"] or 0)):
        body = row["row"]
        flag = " (merge)" if body.get("is_merge") else ""
        lines += [f"## {str(body.get('sha') or '')[:10]}{flag} "
                  f"{str(body.get('authored_at') or '')[:19]}",
                  "", (body.get("message") or "").strip(),
                  f"+{body.get('additions')}/-{body.get('deletions')}", ""]
        for name in body.get("changed_files") or []:
            lines.append(f"  touched: {name}")
        lines.append("")
    space.seed("COMMITS.md", "\n".join(lines) or "# Commits\n\n(none)")

    prs = [r for r in rows if r["source_table"] == "github_pull_request"]
    lines = ["# Pull requests in this window", ""]
    for row in sorted(prs, key=lambda r: (r.get("ordinal") or 0)):
        body = row["row"]
        lines += [f"## #{body.get('number')} {body.get('title') or ''}",
                  f"state: {body.get('state')}  author: {body.get('author_name')}  "
                  f"merged: {body.get('merged_at') or 'no'}",
                  "", (body.get("body") or "").strip(), ""]
    space.seed("PULL_REQUESTS.md", "\n".join(lines) or "# Pull requests\n\n(none)")

    # The touched files, as stubs. The export carried paths and diffstat but not
    # bodies, so these are marked as placeholders rather than invented.
    touched: list[str] = []
    for row in commits:
        touched.extend(row["row"].get("changed_files") or [])
    for name in sorted(set(touched)):
        message = next((c["row"].get("message") for c in commits
                        if name in (c["row"].get("changed_files") or [])), "")
        space.seed(f"REPO/{name}", _stub_body(name, message or "", window))

    space.seed("README.md", "\n".join([
        "# Workspace", "",
        f"Evidence for task window {window['window_id']}.", "",
        "  TASK.md           the ticket and the acceptance signal",
        "  CHAT.md           the discussion, in order",
        "  EVENTS.md         ticket status changes, in order",
        "  COMMITS.md        commits that landed, with files touched",
        "  PULL_REQUESTS.md  pull requests and review comments",
        "  REPO/             stubs for the files those commits touched",
        "",
        "The REPO files are reconstructions, not real bodies: this export carried",
        "paths, commit messages and diffstats, but no file contents. Each one is",
        "built from what the window knows and labelled in its own header. Treat",
        "them as the starting point, not as ground truth.",
    ]))
    return space


# ---------------------------------------------------------------- model calls
def chat(messages: list[dict], model: str, tools: list | None = None,
         tool_choice: str = "auto", timeout: int = 900) -> dict:
    """One model call, with 429 backoff.

    OpenRouter in practice, but a rate limit is a rate limit, so the retry is
    here regardless of provider rather than only for one of them.
    """
    body: dict = {
        "model": model or (OPENROUTER_MODEL if PROVIDER == "openrouter" else GATEWAY_MODEL),
        "max_tokens": MAX_TOKENS,
        "messages": messages,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice
    if PROVIDER == "openrouter":
        url, headers = OPENROUTER_URL, {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "X-Title": "weave-gold-to-platinum",
        }
    else:
        url, headers = GATEWAY_URL, {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GATEWAY_KEY}"}

    payload = json.dumps(body).encode()
    for attempt in range(1, 7):
        request = urllib.request.Request(url, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as failure:
            detail = failure.read().decode()[:300]
            if failure.code not in (429, 500, 502, 503, 504) or attempt == 6:
                raise RuntimeError(f"HTTP {failure.code}: {detail}")
            # exponential backoff with jitter, so parallel workers do not
            # all wake at the same instant and collide again
            wait = min(2 ** attempt, 30) + random.random()
            time.sleep(wait)
    raise RuntimeError("unreachable")


def text_of(message: dict) -> str:
    """Pull prose out of a reply, skipping any reasoning block.

    This model returns its thinking inside content as a part typed 'reasoning'
    with a summary, which is not the same shape as the Responses API's
    reasoning_text. It has to be filtered out or it lands in the trajectory as if
    it were something the engineer said.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content
                       if isinstance(part, dict) and part.get("type") == "text")
    return ""


def reasoning_chars(message: dict) -> int:
    content = message.get("content")
    if not isinstance(content, list):
        return 0
    return sum(len("".join(s.get("text", "") for s in (part.get("summary") or [])
                          if isinstance(s, dict)))
               for part in content
               if isinstance(part, dict) and part.get("type") == "reasoning")


# ------------------------------------------------------------------- the loop
def run_window(window: dict, rows: list[dict], turns: int, model: str,
               verbose: bool = False) -> dict:
    """Replay one window as an agent. Returns the trajectory, in order."""
    root = SANDBOX_ROOT / window["window_id"]
    space = build_workspace(window, rows, root)
    space.snapshot_originals()

    system = MASTER_SYSTEM_PROMPT
    if REAL_REPO:
        system += (f"\n\nA real checkout is mounted at REPO/ALSO. Prefer it where a "
                   f"file exists there; the synthesised stubs are only for files it "
                   f"does not contain.")

    task = TASK_INSTRUCTION.format(start=window["start_ts"], end=window["end_ts"])
    task += f"\nThe ticket is {window.get('task_id') or window['window_id']}: " \
            f"{window.get('title') or 'untitled'}.\n"

    messages = [{"role": "system", "content": system},
                {"role": "user", "content": task}]

    trajectory: list[dict] = []
    changes_made = False
    tokens_in = tokens_out = 0
    tool_calls = 0
    final = ""
    error = ""

    try:
        for turn in range(1, turns + 1):
            # Near the end, push for the write-up. Without this the agents
            # explore until the budget runs out and the trajectory has no
            # final message, which is the part most worth training on.
            if turn == turns - 2 and not changes_made:
                messages.append({"role": "user", "content": NUDGE_NO_EDIT})
            elif turn == turns - 1:
                messages.append({"role": "user", "content": NUDGE_FINAL})

            reply = chat(messages, model, tools=TOOLS,
                         tool_choice="auto" if turn > 1 else "required")
            usage = reply.get("usage") or {}
            tokens_in += int(usage.get("prompt_tokens") or 0)
            tokens_out += int(usage.get("completion_tokens") or 0)
            choice = (reply.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            calls = message.get("tool_calls") or []
            prose = text_of(message)
            thought = reasoning_chars(message)

            messages.append({"role": "assistant", "content": message.get("content"),
                             **({"tool_calls": calls} if calls else {})})
            trajectory.append({
                "turn": turn, "role": "assistant", "content": prose,
                "reasoning_chars": thought,
                "tool_calls": [{"id": c.get("id"),
                                "name": (c.get("function") or {}).get("name"),
                                "arguments": safe_json((c.get("function") or {}).get("arguments"))}
                               for c in calls],
            })

            if not calls:
                final = prose
                trajectory[-1]["final"] = True
                break

            for call in calls:
                function = call.get("function") or {}
                name = function.get("name") or ""
                args = safe_json(function.get("arguments"))
                result = space.call(name, args)
                tool_calls += 1
                if name in ("write_file", "edit_file") and not result.startswith("error:"):
                    changes_made = True
                trajectory[-1].setdefault("results", []).append(
                    {"name": name, "arguments": args, "result": result,
                     "ok": not result.startswith("error:")})
                messages.append({"role": "tool", "tool_call_id": call.get("id"),
                                 "name": name, "content": result})
            if verbose:
                names = ", ".join((c.get("function") or {}).get("name") or "?"
                                   for c in calls)
                print(f"    turn {turn}: {names}")
    except urllib.error.HTTPError as failure:
        # not `as error`, because python deletes that name at the end of an
        # except clause, and the return below needs it
        error = f"HTTP {failure.code}: {failure.read().decode()[:300]}"
    except Exception as failure:                      # noqa: BLE001
        error = f"{type(failure).__name__}: {failure}"
        if verbose:
            traceback.print_exc()

    changes = space.changes()
    return {
        "window_id": window["window_id"],
        "task_id": window.get("task_id"),
        "title": window.get("title"),
        "turns": len(trajectory),
        "tool_calls": tool_calls,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "final": final,
        "error": error,
        "files_changed": changes,
        "diff": space.diff() if changes else "",
        "trajectory": trajectory,
        "messages": messages,
    }


def safe_json(value):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "{}")
    except (ValueError, TypeError):
        return {"_unparsed": str(value)[:400]}


# ------------------------------------------------------------------- database
SCHEMA = """
CREATE SCHEMA IF NOT EXISTS platinum;

CREATE TABLE IF NOT EXISTS platinum.run (
  id text PRIMARY KEY,
  window_id text NOT NULL,
  task_id text,
  title text,
  status text NOT NULL,
  turns int DEFAULT 0,
  tool_calls int DEFAULT 0,
  tokens_in int DEFAULT 0,
  tokens_out int DEFAULT 0,
  files_changed int DEFAULT 0,
  error text DEFAULT '',
  started_at real NOT NULL,
  finished_at real,
  final text
);

CREATE TABLE IF NOT EXISTS platinum.trajectory (
  id bigserial PRIMARY KEY,
  run_id text NOT NULL,
  window_id text NOT NULL,
  turn int NOT NULL,
  step int NOT NULL,
  role text NOT NULL,
  content text DEFAULT '',
  tool_name text,
  tool_args jsonb,
  tool_result text,
  tool_ok boolean,
  tokens_in int DEFAULT 0,
  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS trajectory_run ON platinum.trajectory (run_id, turn, step);
CREATE INDEX IF NOT EXISTS trajectory_window ON platinum.trajectory (window_id);
"""


def run(cur, sql: str, params: tuple = ()) -> int:
    """Execute a statement that returns no rows. All SQL in this file is written
    with ? because that reads naturally, and psycopg only speaks %s, so the
    translation happens here once rather than in thirty places."""
    cur.execute(sql.replace("?", "%s"), params)
    return cur.rowcount


def q(cur, sql: str, params: tuple = ()) -> list[dict]:
    """Execute a query and return the rows. See run() for the placeholder note."""
    cur.execute(sql.replace("?", "%s"), params)
    return cur.fetchall()


def connect(autocommit: bool = False):
    """psycopg3 hands back tuples unless told otherwise, and this script wants
    rows by name throughout, so the row factory is set here rather than
    indexing by position in thirty places."""
    import psycopg
    from psycopg.rows import dict_row
    if not DB_TOKEN:
        raise SystemExit(
            "set LAKEBASE_TOKEN in the environment, or pass --token. The Lakebase "
            "token lives an hour, so mint a fresh one rather than hardcoding it.")
    return psycopg.connect(
        host=DB_HOST, port=5432, dbname=DB_NAME, user=DB_USER,
        password=DB_TOKEN, sslmode="require", connect_timeout=30,
        autocommit=autocommit, row_factory=dict_row)


def store(result: dict, run_id: str, started: float) -> None:
    with connect(autocommit=True) as conn, conn.cursor() as cur:
        status = "failed" if result["error"] else "succeeded"
        run(cur,
            "INSERT INTO platinum.run (id, window_id, task_id, title, status, turns, "
            "tool_calls, tokens_in, tokens_out, files_changed, error, started_at, "
            "finished_at, final) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, result["window_id"], result["task_id"], result["title"], status,
             result["turns"], result["tool_calls"], result["tokens_in"],
             result["tokens_out"], len(result["files_changed"]), result["error"],
             started, time.time(), result["final"]))
        for entry in result["trajectory"]:
            base = (run_id, result["window_id"], entry["turn"], 0, "assistant",
                    entry.get("content") or "")
            if not entry.get("tool_calls"):
                run(cur,
                    "INSERT INTO platinum.trajectory (run_id, window_id, turn, step, "
                    "role, content) VALUES (?,?,?,?,?,?)", base)
                continue
            for step, call in enumerate(entry["tool_calls"]):
                result_row = (entry.get("results") or [{}] * (step + 1))[step]
                run(cur,
                    "INSERT INTO platinum.trajectory (run_id, window_id, turn, step, "
                    "role, content, tool_name, tool_args, tool_result, tool_ok) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (run_id, result["window_id"], entry["turn"], step, "assistant",
                     entry.get("content") or "", call["name"],
                     json.dumps(call["arguments"]), result_row.get("result"),
                     result_row.get("ok")))


def load_windows(cur, limit: int | None) -> list[dict]:
    windows = []
    for row in q(cur, "SELECT * FROM gold.task_window ORDER BY start_ts"):
        rows = q(cur,
                 "SELECT source_table, source_key, ts, ordinal, row FROM gold.task_chunk "
                 "WHERE window_id = ? ORDER BY ts NULLS LAST, ordinal",
                 (row["window_id"],))
        window = {k: row[k] for k in
                  ("window_id", "task_id", "title", "summary", "start_ts", "end_ts",
                   "confidence", "participants")}
        window["chunks"] = len(rows)
        window["_rows"] = rows
        windows.append(window)
        if limit and len(windows) >= limit:
            break
    return windows


# ---------------------------------------------------------------------- main
def main() -> int:
    global DB_TOKEN, PROVIDER

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workers", type=int, default=8,
                        help="windows to run at once")
    parser.add_argument("--turns", type=int, default=20,
                        help="max model turns per window")
    parser.add_argument("--limit", type=int, default=0,
                        help="only the first N windows")
    parser.add_argument("--model", default="", help="blank uses the provider default")
    parser.add_argument("--provider", default=PROVIDER, choices=["openrouter", "gateway"])
    parser.add_argument("--token", default="", help="Lakebase token, or set LAKEBASE_TOKEN")
    parser.add_argument("--dry-run", action="store_true",
                        help="materialise the sandboxes and print one, call no model")
    parser.add_argument("--export", default="", help="write trajectories to this jsonl")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.token:
        DB_TOKEN = args.token
    PROVIDER = args.provider
    model = args.model or (OPENROUTER_MODEL if PROVIDER == "openrouter" else GATEWAY_MODEL)

    conn = connect()
    import psycopg
    with conn.cursor() as cur:
        for statement in filter(None, (s.strip() for s in SCHEMA.split(";"))):
            cur.execute(statement)
        conn.commit()
        windows = load_windows(cur, args.limit or None)

    total_chunks = sum(w["chunks"] for w in windows)
    print(f"platinum: {len(windows)} window(s), {total_chunks} chunk(s), "
          f"{args.workers} worker(s), {args.turns} turn(s) each, "
          f"provider {args.provider}, model {model}")
    if not windows:
        print("no gold windows. Run notebook 03 first.")
        return 1

    if args.dry_run:
        for window in windows[:1]:
            space = build_workspace(window, window["_rows"], SANDBOX_ROOT / window["window_id"])
            space.snapshot_originals()
            print(f"\nmaterialised {space.root}:")
            print(space.call("list_dir", {"depth": 2}))
            print("\nTASK.md:")
            print(space.call("read_file", {"path": "TASK.md"}))
        return 0

    lock = threading.Lock()
    results: list[dict] = []
    began = time.time()

    def work(window):
        run_id = f"pl_{uuid.uuid4().hex[:12]}"
        started = time.time()
        result = run_window(window, window["_rows"], args.turns, model, args.verbose)
        try:
            store(result, run_id, started)
        except Exception as failure:                    # noqa: BLE001
            result["error"] = (result["error"] or "") + f" | store failed: {failure}"
        with lock:
            results.append(result)
            done = len(results)
            flag = "FAIL" if result["error"] else "ok"
            print(f"  [{done}/{len(windows)}] {result['window_id']} {flag} "
                  f"turns={result['turns']} tools={result['tool_calls']} "
                  f"out={result['tokens_out']:,} "
                  f"{time.time() - started:.0f}s"
                  + (f"  {result['error'][:90]}" if result["error"] else ""))
        return result

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, w): w for w in windows}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as failure:                # noqa: BLE001
                print(f"  worker died: {type(failure).__name__}: {failure}")

    good = [r for r in results if not r["error"]]
    print(f"\ndone in {time.time() - began:.0f}s")
    print(f"  windows      {len(good)}/{len(results)} ok")
    print(f"  turns        {sum(r['turns'] for r in results):,}")
    print(f"  tool calls   {sum(r['tool_calls'] for r in results):,}")
    print(f"  tokens in    {sum(r['tokens_in'] for r in results):,}")
    print(f"  tokens out   {sum(r['tokens_out'] for r in results):,}")
    print(f"  changed files{sum(len(r['files_changed']) for r in results):,}")
    if [r for r in results if not r["final"]]:
        print(f"  no final answer in {len([r for r in results if not r['final']])} window(s)")

    if args.export:
        with open(args.export, "w", encoding="utf-8") as handle:
            for result in sorted(results, key=lambda r: r["window_id"]):
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")
        print(f"  exported {args.export}")

    conn.close()
    return 0 if len(good) == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())

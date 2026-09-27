#!/usr/bin/env python3
"""TIMEFRAMES -> GOLD.

The detection model chose task timeframes. This dissector turns each timeframe
into the gold training material the next model consumes: the three streams
sliced to that window, the git diff for that window, the attached documents, and
a task brief assembled from the tribal evidence inside the window.

Input : detected/task_windows.json   (the model's timeframe output)
        silver/*.jsonl               (cleaned records)
        the meridian git repo        (for real diffs)
Output: gold/<task_id>/
          brief.json        problem, constraints, participants, evidence
          discord.json      the conversation inside the window
          jira.json         ticket + changelog events inside the window
          git.json          commits inside the window (all refs) + PR
          diff.patch        the actual patch produced during the window
          documents.json    attached documents + extracted text
        gold/_index.json    one row per task with sizes and coverage
"""
import json, re, subprocess, sys
from datetime import datetime
from pathlib import Path

SYN = Path("/Users/aludayalu/weave/opencode/synth-data")
REPO = SYN.parent / "meridian"
SIL, DET, GOLD = SYN / "silver", SYN / "detected", SYN / "gold"


def jsonl(p):
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def epoch(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def git(*a):
    r = subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True)
    return r.stdout.rstrip("\n") if r.returncode == 0 else ""


def extract_text(path, limit=4000):
    """Stand-in for the silver-side document extraction (pypdf / utf-8 / vision)."""
    p = SYN / path
    if not p.exists():
        return ""
    if path.endswith(".log") or path.endswith(".txt"):
        return p.read_text(errors="replace")[:limit]
    if path.endswith(".png"):
        return "[image captured; OCR text unavailable in this build]"
    return "[pdf report; extract with pypdf in the silver layer]"


def main():
    windows = json.loads((DET / "task_windows.json").read_text())["tasks"]
    catalog = {t["jira"]: t for t in json.loads((SYN / "task_catalog.json").read_text())["tasks"]}
    msgs = jsonl(SIL / "discord_messages.jsonl")
    events = jsonl(SIL / "jira_changelog_events.jsonl")
    issues = {i["issue_key"]: i for i in jsonl(SIL / "jira_issues.jsonl")}
    commits = jsonl(SIL / "github_commits.jsonl")
    prs = {p["pr_number"]: p for p in jsonl(SIL / "github_pull_requests.jsonl")}
    docs = jsonl(SIL / "documents.jsonl")

    # drop any previous dissection so re-runs are clean
    for d in GOLD.glob("mer-*"):
        if d.is_dir():
            subprocess.run(["rm", "-rf", str(d)])

    index = []
    for w in windows:
        key = w["evidence"]["jira"]["issue_keys"][0]
        tid = w["task_id"]
        s, e = epoch(w["start_ts_utc"]), epoch(w["end_ts_utc"])
        out = GOLD / tid
        out.mkdir(parents=True, exist_ok=True)

        # ---- discord slice
        seg_msgs = [m for m in msgs if s <= m["ts_epoch"] <= e]
        d_ids = {m["message_id"] for m in seg_msgs}
        (out / "discord.json").write_text(json.dumps({
            "task_id": tid, "channel": w["evidence"]["discord"]["channel"],
            "window": {"start": w["start_ts_utc"], "end": w["end_ts_utc"]},
            "message_count": len(seg_msgs), "messages": seg_msgs}, indent=2, ensure_ascii=False))

        # ---- jira slice
        seg_ev = [x for x in events if x["issue_key"] == key and s <= epoch(x["ts_utc"]) <= e]
        (out / "jira.json").write_text(json.dumps({
            "task_id": tid, "issue": issues.get(key),
            "changelog_events": seg_ev}, indent=2, ensure_ascii=False))

        # ---- the patch this PR itself brought.
        # A merge commit's parents are [main_at_merge, branch_tip], so the
        # change the PR introduced is first_parent..second_parent. Diffing the
        # branch tip against the merge result would also sweep in unrelated
        # work that landed on main while the branch was open.
        merge_sha = w["evidence"]["github"]["merge_shas"][0]
        patch, base_sha, head_sha = "", None, None
        if merge_sha:
            parents = git("rev-list", "--parents", "-n", "1", merge_sha).split()[1:]
            if len(parents) >= 2:
                # diff from the merge-base, not from the first parent: main may
                # have advanced while the branch was open, and the first parent
                # would then include that unrelated work (or invert the diff).
                base_sha = git("merge-base", parents[0], parents[1]) or parents[0]
                head_sha = parents[1]
                patch = git("diff", "--no-color", f"{base_sha}..{head_sha}")
        (out / "diff.patch").write_text(patch)

        # ---- git slice: only the commits the PR brought, not every commit
        # that happened to land inside the same wall-clock window.
        branch_commits = set(git("rev-list", f"{base_sha}..{head_sha}").split()) if head_sha else set()
        seg_commits = [c for c in commits
                       if c["sha"] in branch_commits or (not branch_commits and s <= epoch(c["authored_ts_utc"]) <= e)]
        pr = prs.get(w["evidence"]["github"]["pr_numbers"][0], {})
        (out / "git.json").write_text(json.dumps({
            "task_id": tid, "pull_request": pr, "merge_sha": merge_sha,
            "base_sha": base_sha, "head_sha": head_sha,
            "commit_count": len(seg_commits), "commits": seg_commits,
            "files": sorted({f for c in seg_commits for f in c["changed_files"]})}, indent=2))

        # ---- git slice (all refs, not just main)
        # ---- documents
        seg_docs = []
        for doc in docs:
            if doc.get("linked_issue_key") == key or doc.get("path") in {
                    a["path"] for m in seg_msgs for a in m["attachments"]}:
                seg_docs.append({**doc, "extracted_text": extract_text(doc["path"])})
        (out / "documents.json").write_text(json.dumps(seg_docs, indent=2, ensure_ascii=False))

        # ---- brief handed to the next model
        brief = {
            "task_id": tid, "jira": key, "pr": pr.get("pr_number"),
            "repository": "meridian-erp",
            "repo_revision": f"{head_sha[:10] if head_sha else merge_sha[:10]} (task branch tip)",
            "title": (catalog.get(key) or {}).get("title", w.get("title")),
            "summary": w.get("summary"),
            "start_ts_utc": w["start_ts_utc"], "end_ts_utc": w["end_ts_utc"],
            "duration_minutes": round((e - s) / 60),
            "participants": w.get("participants", []),
            "tribal_evidence": {
                "discussion": [m["content"] for m in seg_msgs][:12],
                "ticket": (issues.get(key) or {}).get("description"),
                "documents": [d["filename"] for d in seg_docs],
            },
            "coverage": {
                "discord_messages": len(seg_msgs), "jira_events": len(seg_ev),
                "commits": len(seg_commits), "patch_bytes": len(patch),
                "documents": len(seg_docs),
            },
        }
        (out / "brief.json").write_text(json.dumps(brief, indent=2, ensure_ascii=False))

        index.append({"task_id": tid, "jira": key, "pr": brief["pr"],
                      "title": brief["title"], "channel": w["evidence"]["discord"]["channel"],
                      "tribal_constraint": (catalog.get(key) or {}).get("tribal", ""),
                      "split": "eval" if key.startswith("MER-2") else "train",
                      "window": {"start": w["start_ts_utc"], "end": w["end_ts_utc"]},
                      "duration_minutes": brief["duration_minutes"],
                      "document_files": [d["filename"] for d in seg_docs],
                      "files": sorted({f for c in seg_commits for f in c["changed_files"]}),
                      **brief["coverage"]})

    index.sort(key=lambda r: r["window"]["start"])
    (GOLD / "_index.json").write_text(json.dumps({"tasks": index}, indent=2))
    tot = {"tasks": len(index),
           "messages": sum(i["discord_messages"] for i in index),
           "jira_events": sum(i["jira_events"] for i in index),
           "commits": sum(i["commits"] for i in index),
           "patch_bytes": sum(i["patch_bytes"] for i in index),
           "documents": sum(i["documents"] for i in index),
           "empty_tasks": sum(1 for i in index if i["discord_messages"] == 0)}
    print(json.dumps(tot, indent=2))


if __name__ == "__main__":
    main()

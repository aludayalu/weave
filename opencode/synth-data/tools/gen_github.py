#!/usr/bin/env python3
"""Export the full git history as GitHub-style JSON (raw, unsplit).

Mirrors the shape the real GitHub REST API returns so a Databricks bronze
ingest can parse it without custom code:
  raw/github/commits.json  -> GET /repos/{owner}/{repo}/commits
  raw/github/pulls.json    -> GET /repos/{owner}/{repo}/pulls
  raw/github/events.json   -> normalized event feed (PR opened/merged/closed)
"""
import json, re, subprocess
from pathlib import Path

ROOT = Path("/Users/aludayalu/weave/opencode")
REPO = ROOT / "meridian"
RAW = ROOT / "synth-data" / "raw" / "github"
OWNER = "meridian"
NAME = "meridian-erp"

def git(*a, ref="main"):
    return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True, check=True).stdout.rstrip("\n")

def main():
    RAW.mkdir(parents=True, exist_ok=True)

    # ---- commits (every reachable commit, all branches, chronological)
    fmt = "%H%x1f%an%x1f%ae%x1f%aI%x1f%cI%x1f%P%x1f%s"
    shas, out = [], []
    for line in git("log", "--all", "--format=" + fmt).splitlines():
        sha, an, ae, ai, ci, parents, subj = line.split("\x1f")
        h = git("show", "--numstat", "--format=", sha)
        files, add, dele = [], 0, 0
        for f in h.splitlines():
            parts = f.split("\t")
            if len(parts) == 3:
                a, d, path = parts
                add += int(a) if a.isdigit() else 0
                dele += int(d) if d.isdigit() else 0
                files.append({"filename": path, "additions": int(a) if a.isdigit() else 0,
                              "deletions": int(d) if d.isdigit() else 0})
        out.append({
            "sha": sha, "html_url": f"https://github.com/{OWNER}/{NAME}/commit/{sha}",
            "commit": {"author": {"name": an, "email": ae, "date": ai},
                       "committer": {"name": an, "email": ae, "date": ci},
                       "message": subj},
            "parents": [{"sha": p} for p in parents.split() if p],
            "stats": {"additions": add, "deletions": dele, "total": add + dele},
            "files": files,
        })
        shas.append(sha)
    out.sort(key=lambda c: c["commit"]["author"]["date"])
    (RAW / "commits.json").write_text(json.dumps(out, indent=2))

    # ---- pull requests derived from merge commits + open spikes
    pulls, events = [], []
    for line in git("log", "--merges", "--format=%H%x1f%cI%x1f%s").splitlines():
        sha, date, subject = line.split("\x1f")
        m = re.match(r"Merge pull request #(\d+) from (\S+)", subject)
        if not m:
            continue
        pr, head = int(m.group(1)), m.group(2)
        branch = head.replace(f"{OWNER}/", "")
        body = git("log", "--format=%H%x1f%aI%x1f%an%x1f%s", f"{sha}^1..{sha}^2")
        commits = [l.split("\x1f") for l in body.splitlines() if l]
        opened = commits[0][1] if commits else date
        pulls.append({
            "number": pr, "state": "closed", "title": commits[-1][3] if commits else subject,
            "user": {"login": commits[-1][2] if commits else "unknown"},
            "created_at": opened, "updated_at": date, "merged_at": date, "closed_at": date,
            "merge_commit_sha": sha, "merged": True,
            "head": {"ref": branch, "sha": commits[-1][0] if commits else sha,
                     "repo": {"full_name": f"{OWNER}/{NAME}"}},
            "base": {"ref": "main", "repo": {"full_name": f"{OWNER}/{NAME}"}},
            "html_url": f"https://github.com/{OWNER}/{NAME}/pull/{pr}",
            "body": f"{commits[0][3]}\n\nJira: see discussion in Discord for the task window." if commits else "",
            "commits": len(commits), "changed_files": len({f for c in commits for f in git("show", "--name-only", "--format=", c[0]).splitlines() if f}),
        })
        events.append({"type": "pull_request", "action": "opened", "number": pr,
                       "created_at": opened, "actor": commits[0][2] if commits else "unknown"})
        events.append({"type": "pull_request", "action": "closed", "number": pr,
                       "created_at": date, "actor": commits[-1][2] if commits else "unknown"})
        events.append({"type": "pull_request", "action": "merged", "number": pr,
                       "created_at": date, "actor": commits[-1][2] if commits else "unknown",
                       "merge_commit_sha": sha})

    # unmerged spike branches (branch tip is not a merge commit on main)
    merged_shas = {p["merge_commit_sha"] for p in pulls if p["merged"]}
    nxt = 100
    for br in subprocess.run(["git", "branch", "--format=%(refname:short)"], cwd=REPO,
                             capture_output=True, text=True, check=True).stdout.split():
        if not br.startswith("spike/"):
            continue
        tip = git("rev-parse", br)
        if tip in merged_shas:
            continue
        commit = git("log", "-1", "--format=%H%x1f%aI%x1f%an%x1f%s", br).split("\x1f")
        nxt += 1
        pulls.append({
            "number": nxt, "state": "closed", "title": commit[3], "user": {"login": commit[2]},
            "created_at": commit[1], "updated_at": commit[1], "merged_at": None,
            "closed_at": commit[1], "merge_commit_sha": None, "merged": False,
            "head": {"ref": br, "sha": commit[0], "repo": {"full_name": f"{OWNER}/{NAME}"}},
            "base": {"ref": "main", "repo": {"full_name": f"{OWNER}/{NAME}"}},
            "html_url": f"https://github.com/{OWNER}/{NAME}/pull/{nxt}",
            "body": "Spike, parked without merge.", "commits": 1, "changed_files": 1,
        })
        events.append({"type": "pull_request", "action": "opened", "number": nxt,
                       "created_at": commit[1], "actor": commit[2]})
        events.append({"type": "pull_request", "action": "closed", "number": nxt,
                       "created_at": commit[1], "actor": commit[2]})

    pulls.sort(key=lambda p: p["created_at"])
    events.sort(key=lambda e: e["created_at"])
    (RAW / "pulls.json").write_text(json.dumps(pulls, indent=2))
    (RAW / "events.json").write_text(json.dumps(events, indent=2))

    print(json.dumps({"commits": len(out), "pulls": len(pulls),
                      "merged": sum(1 for p in pulls if p["merged"]), "events": len(events)}))

if __name__ == "__main__":
    main()

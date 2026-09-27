#!/usr/bin/env python3
"""Generate raw Discord + Jira streams and attached documents for all 32
meridian-erp tasks. Git history is the source of truth for windows/commits;
this script derives conversation + changelog content from the task catalog
and the real merge commits.

Usage: python3 gen_streams.py
"""

import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import SYN, REPO  # noqa: E402
import json, os, random, subprocess, hashlib, textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAW = SYN / "raw"
ATT = RAW / "attachments"

random.seed(7)

PERSONAS = {
    "priya":  ("Priya Sharma",  "priya-sharma",  "priya@meridian-erp.dev"),
    "marcus": ("Marcus Lee",    "marcus-lee",    "marcus@meridian-erp.dev"),
    "sofia":  ("Sofia Almeida", "sofia-almeida", "sofia@meridian-erp.dev"),
    "dan":    ("Dan Levin",     "dan-levin",     "dan@meridian-erp.dev"),
}
EMAIL_TO_HANDLE = {v[2]: k for k, v in PERSONAS.items()}

CHANNELS = {
    "billing":   ("billing",   "1001"),
    "platform":  ("eng-platform", "1002"),
    "frontend":  ("eng-frontend", "1003"),
    "incidents": ("incidents", "1004"),
    "deploys":   ("deploys",   "1005"),
    "general":   ("general",   "1006"),
}
GUILD = "meridian-systems"

def git(*args):
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()

def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

def epoch(dt):
    return int(dt.timestamp())

DISCORD_EPOCH = 1420070400000  # 2015-01-01
def snowflake(dt, n):
    return str((epoch(dt) * 1000) - DISCORD_EPOCH) * 1 + str(n)[-4:].rjust(4, "0")

def load_catalog():
    return json.loads((SYN / "task_catalog.json").read_text())

def merge_commits():
    """Return {pr_number: (sha, iso_date, branch, title)} from real history."""
    out = []
    raw = git("log", "--merges", "--format=%H|%cI|%s", "main")
    for line in raw.splitlines():
        sha, date, subject = line.split("|", 2)
        m = __import__("re").match(r"Merge pull request #(\d+) from (\S+)", subject)
        if not m:
            continue
        pr = int(m.group(1))
        branch = m.group(2).replace("meridian/", "")
        out.append({"pr": pr, "sha": sha, "date": date, "branch": branch})
    return out

def pr_commits(pr):
    """Commits that landed via the merge for this PR (first-parent..second-parent)."""
    fmt = "%H|%an|%ae|%aI|%cI|%s"
    raw = git("log", "--format=" + fmt, "-1", pr["sha"] + "^2")
    rows = []
    if raw:
        h, an, ae, ai, ci, s = raw.split("|", 5)
        rows.append({"sha": h, "author": an, "email": ae, "authored": ai, "committed": ci, "message": s})
    body = git("log", "--format=" + fmt, f"{pr['sha']}^1..{pr['sha']}^2")
    for line in body.splitlines():
        h, an, ae, ai, ci, s = line.split("|", 5)
        rows.append({"sha": h, "author": an, "email": ae, "authored": ai, "committed": ci, "message": s})
    seen, uniq = set(), []
    for r in rows:
        if r["sha"] in seen:
            continue
        seen.add(r["sha"]); uniq.append(r)
    return uniq

# ---------------------------------------------------------------- documents
def pdf_bytes(lines, title):
    from PIL import Image, ImageDraw, ImageFont
    W, H = 1240, 1754
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    try:
        f_title = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 34)
        f_h = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 20)
        f_b = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 15)
    except OSError:
        f_title = f_h = f_b = ImageFont.load_default()
    d.rectangle([0, 0, W, 12], fill=(31, 78, 121))
    d.text((80, 60), "MERIDIAN ERP — CUSTOMER SUCCESS", font=f_h, fill=(31, 78, 121))
    d.text((80, 110), title, font=f_title, fill=(20, 20, 20))
    y = 200
    for kind, text in lines:
        if kind == "h":
            d.text((80, y), text, font=f_h, fill=(20, 20, 20)); y += 34
        else:
            for para in textwrap.wrap(text, 110):
                d.text((80, y), para, font=f_b, fill=(60, 60, 60)); y += 22
            y += 10
        if y > H - 140: break
    d.text((80, H - 90), "Confidential — generated for internal dataset construction.", font=f_b, fill=(120, 120, 120))
    import io
    buf = io.BytesIO()
    img.save(buf, format="PDF")
    return buf.getvalue()

def png_bytes(kind, label, lines):
    from PIL import Image, ImageDraw, ImageFont
    W, H = 1100, 700
    img = Image.new("RGB", (W, H), (245, 246, 248))
    d = ImageDraw.Draw(img)
    try:
        f_h = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 20)
        f_b = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 15)
        f_m = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 15)
    except OSError:
        f_h = f_b = f_m = ImageFont.load_default()
    d.rectangle([0, 0, W, 44], fill=(228, 230, 234))
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([16 + i * 22, 14, 30 + i * 22, 28], fill=c)
    d.text((100, 14), label, font=f_h, fill=(70, 70, 70))
    if kind == "terminal":
        d.rectangle([0, 44, W, H], fill=(24, 26, 30))
        y = 70
        for ln in lines:
            col = (120, 220, 140) if "$" in ln else ((240, 200, 120) if "error" in ln.lower() else (200, 200, 200))
            d.text((24, y), ln, font=f_b, fill=col); y += 22
    else:
        d.rectangle([40, 70, W - 40, 130], fill=(31, 78, 121))
        d.text((60, 92), lines[0], font=f_h, fill="white")
        y = 160
        for ln in lines[1:]:
            d.text((60, y), ln, font=f_m if ln.startswith("#") else f_b, fill=(40, 40, 40)); y += 28
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def log_bytes(name, lines):
    return ("\n".join(lines) + "\n").encode()

def write_att(kind, task, name, data, stream):
    d = ATT / stream / kind
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(data)
    return {"name": name, "size": len(data), "path": str(p.relative_to(SYN))}

# ---------------------------------------------------------------- main
def main():
    catalog = load_catalog()
    tasks = {t["id"]: t for t in catalog["tasks"]}
    prs = {p["pr"]: p for p in merge_commits()}

    # ---- documents per task
    docs = {}
    for tid, t in tasks.items():
        pr = prs.get(t["pr"])
        start = datetime.fromisoformat(pr["date"]) - timedelta(days=3, hours=2)
        made = []
        for spec in t["docs"]:
            if spec == "terminal-log" or spec.endswith("log"):
                lines = [
                    f"2026-01-01T00:00:00Z INFO  meridian-api booting (commit ${t['branch'] if 'branch' in t else 'main'})",
                    f"2026-01-01T00:00:01Z WARN  request slow: GET {random.choice(['/api/billing/totals','/api/audit/export','/api/tenants/harborline/invoices'])} 1240ms",
                    "2026-01-01T00:00:02Z ERROR upstream failure: context deadline exceeded",
                    "2026-01-01T00:00:02Z ERROR at internal/handler.dispatch (/srv/meridian)",
                    f"2026-01-01T00:00:03Z INFO  ticket {t['jira']} acknowledged",
                ]
                made.append(write_att("logs", tid, f"{tid}-{spec}.log", log_bytes(spec, lines), "jira"))
            elif spec.endswith("screenshot") or spec.endswith("preview-screenshot") or spec.endswith("csv-sample") or spec.endswith("network-log") or spec.endswith("locale-log") or spec.endswith("hydration-log") or spec.endswith("sse-log") or spec.endswith("browser-screenshot") or spec.endswith("workbook-screenshot") or spec.endswith("tenant-switch-screenshot") or spec.endswith("outbox-log") or spec.endswith("cache-log") or spec.endswith("payment-log") or spec.endswith("nginx-log") or spec.endswith("worker-log") or spec.endswith("worker-trace") or spec.endswith("import-log") or spec.endswith("replay-log") or spec.endswith("validation-log") or spec.endswith("startup-log") or spec.endswith("release-log") or spec.endswith("bundle-log") or spec.endswith("build-log") or spec.endswith("gateway-log") or spec.endswith("curl-log") or spec.endswith("delete-log") or spec.endswith("config-diff") or spec.endswith("event-sample"):
                made.append(write_att("images", tid, f"{tid}-{spec}.png",
                    png_bytes("terminal" if spec.endswith("log") else "screen", t["jira"],
                              [t["title"], f"ticket: {t['jira']}", f"channel: {t['channel']}",
                               f"window: {iso(start)} ..", t["problem"][:90]]), "discord"))
            else:
                body = [("h", "Summary"),
                        ("b", t["problem"]),
                        ("h", "Environment"),
                        ("b", f"Ticket {t['jira']} · repo meridian-erp · branch {t.get('branch','main')}") ,
                        ("h", "Reproduction"),
                        ("b", t["tribal"])]
                made.append(write_att("reports", tid, f"{tid}-{spec}.pdf", pdf_bytes(body, t["title"]), "jira"))
        docs[tid] = made

    (SYN / "gold" / "documents_index.json").write_text(json.dumps(
        {tid: [d["name"] for d in v] for tid, v in docs.items()}, indent=2))

    print(json.dumps({"tasks": len(tasks), "merge_commits": len(prs), "docs": sum(len(v) for v in docs.values())}))

if __name__ == "__main__":
    main()

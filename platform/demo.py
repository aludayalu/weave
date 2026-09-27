"""Demo data, so a fresh account looks like a working one.

An empty dashboard is a bad first impression and a bad test, because there is
nothing to click. This seeds one GitHub repo and one Discord guild with enough
records that the pipeline, the diff panel and the run list all have something to
show.

Two rules, both deliberate:

  * It only ever runs when WEAVE_DEMO is on. Off by default in code, on by
    default in local dev, so a real deployment does not silently show fiction.
  * The records are marked. Every seeded source has demo=true in its config and
    every seeded record carries a demo marker, so demo data is identifiable in
    the database and not just by remembering that you clicked the button.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone

import store

PEOPLE = ["Dan Levin", "Priya Sharma", "Marcus Lee", "Ada Okonkwo", "Hana Ito",
          "Ravi Menon", "Tess Bramble", "Yusuf Karim"]
CHANNELS = ["billing", "platform", "incidents", "general", "release-train"]

TOPICS = [
    ("partner payment timeouts misclassified as hard declines", "MER-101"),
    ("compliance export flag gated on tenant instead of account", "MER-102"),
    ("GDPR audit export leaking personal data", "MER-851"),
    ("tenant grace-period override falling back to zero", "MER-103"),
    ("workspace switch leaking stale invoice data", "MER-104"),
    ("billing API session invalidated on refresh", "MER-105"),
    ("retry storm from the fulfilment worker", "MER-106"),
    ("webhook signature check accepting replayed events", "MER-107"),
    ("rate limiter counting rejected requests", "MER-108"),
    ("invoice PDF missing the tax line", "MER-109"),
    ("tenant config optional field wiping defaults", "MER-110"),
    ("card vault timeout treated as a decline", "MER-111"),
]

FILES = [
    "pkg/errs/task101_gateway_timeout_test.go",
    "pkg/errs/task101_gateway_timeout.go",
    "api/errors.yaml",
    "internal/payroll/retry_policy.go",
    "internal/billing/session.go",
    "web/lib/invoice.ts",
]


def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def seed(user_id: str, days: int = 24) -> dict:
    """Create one GitHub source and one Discord guild for this account."""
    if already_seeded(user_id):
        return {"seeded": False, "reason": "this account already has demo data"}

    now = time.time()
    started = now - days * 86400
    rng = random.Random(17)          # fixed, so screenshots are reproducible

    # ------------------------------------------------------------- discord
    discord_source = store.new_id("src")
    discord_pair = store.new_id("pair")
    guild = "Heketon"
    store.insert("sources", {
        "id": discord_source, "user_id": user_id, "kind": "discord",
        "name": f"{guild} (demo)", "config": store.jdump({
            "demo": True, "guild_name": guild,
            "note": "demo data, seeded so the dashboard is not empty"}),
        "status": "ready", "created_at": now, "last_synced_at": now,
    })
    store.insert("pairs", {
        "id": discord_pair, "user_id": user_id, "source_id": discord_source,
        "guild_id": "1553523686949265598", "label": f"{guild} (demo)",
        "created_at": now})

    messages = []
    ticket = 0
    previous = None
    for index in range(180):
        subject, key = TOPICS[index % len(TOPICS)]
        if key != previous:
            ticket += 1
            previous = key
        stamp = started + (now - started) * (index + 1) / 181
        who = PEOPLE[index % len(PEOPLE)]
        channel = CHANNELS[index % len(CHANNELS)]
        if index % 11 == 0:
            body = (f"Opening {key} to track {subject}. "
                    f"Third report on this, starting now.")
        elif index % 7 == 0:
            body = (f"On {key}: the upstream call is timing out and we are treating "
                    f"that as a hard decline. That is the whole bug.")
        elif index % 5 == 0:
            body = f"Reproduced on staging for {key}. Adding a test before the fix."
        else:
            body = (f"Looking at {key} now. The {subject} path is not covered by a "
                    f"test, which is why this slipped through.")
        messages.append((store.new_id("rec"), discord_pair, "discord_message",
                         f"1524429855{index:010d}", stamp, store.jdump({
                             "message_id": f"1524429855{index:010d}",
                             "channel": channel, "channel_id": str(1000 + index),
                             "guild_id": "1553523686949265598", "guild_name": guild,
                             "author_id": str(2000 + (index % len(PEOPLE))),
                             "author_name": who, "ts_utc": _iso(stamp),
                             "content": body, "mentions_ticket": [key],
                             "attachments": [], "demo": True,
                         })))
    # a few status changes, so the timeline has structure
    for index, (subject, key) in enumerate(TOPICS[:8]):
        stamp = started + (now - started) * (index + 0.6) / 12
        for field, to_value in (("status", "In Progress"), ("status", "Done")):
            messages.append((store.new_id("rec"), discord_pair, "jira_changelog_event",
                             None, stamp, store.jdump({
                                 "id": index, "ts": _iso(stamp), "field": field,
                                 "author": PEOPLE[index % len(PEOPLE)],
                                 "from_value": "To Do" if to_value == "In Progress"
                                 else "In Progress",
                                 "to_value": to_value, "issue_key": key,
                                 "demo": True})))

    # -------------------------------------------------------------- github
    github_source = store.new_id("src")
    github_pair = store.new_id("pair")
    repo = "meridian-erp/meridian"
    store.insert("sources", {
        "id": github_source, "user_id": user_id, "kind": "github",
        "name": repo, "config": store.jdump({
            "demo": True, "owner": repo.split("/")[0], "repo": repo.split("/")[1],
            "note": "demo data, seeded so the dashboard is not empty"}),
        "status": "ready", "created_at": now, "last_synced_at": now,
    })
    store.insert("pairs", {
        "id": github_pair, "user_id": user_id, "source_id": github_source,
        "repo_url": f"https://github.com/{repo}", "label": repo,
        "created_at": now})

    for index, (subject, key) in enumerate(TOPICS):
        stamp = started + (now - started) * (index + 0.8) / 12
        messages.append((store.new_id("rec"), github_pair, "github_commit",
                         f"47df7461{index:016x}", stamp, store.jdump({
                             "sha": f"47df7461{index:016x}",
                             "message": f"fix: {key} {subject} (demo)",
                             "is_merge": index % 3 == 2, "additions": 40 + index * 3,
                             "deletions": index * 2,
                             "author_name": PEOPLE[index % len(PEOPLE)],
                             "authored_at": _iso(stamp),
                             "parent_shas": [f"{index:040x}"],
                             "changed_files": [FILES[index % len(FILES)]],
                             "demo": True})))
        messages.append((store.new_id("rec"), github_pair, "github_pull_request",
                         str(index + 1), stamp, store.jdump({
                             "number": index + 1, "title": f"{key}: {subject}",
                             "state": "MERGED", "author_name": PEOPLE[index % len(PEOPLE)],
                             "merged_at": _iso(stamp), "body": f"Fixes {key}.",
                             "demo": True})))

    store.execute_many(
        "INSERT INTO records (id, pair_id, stream, external_id, ts, payload) "
        "VALUES (?, ?, ?, ?, ?, ?)", messages)

    return {"seeded": True, "discord_messages": 180,
            "github_records": len(TOPICS) * 2,
            "records_total": len(messages),
            "sources": [discord_source, github_source],
            "pairs": [discord_pair, github_pair]}


def already_seeded(user_id: str) -> bool:
    return bool(store.q1(
        "SELECT id FROM sources WHERE user_id = ? AND config LIKE '%\"demo\": true%'",
        (user_id,)))


def clear(user_id: str) -> dict:
    """Remove demo data, so it can be toggled without leaving junk behind."""
    sources = store.q(
        "SELECT id FROM sources WHERE user_id = ? AND config LIKE '%\"demo\": true%'",
        (user_id,))
    if not sources:
        return {"cleared": 0}
    ids = [row["id"] for row in sources]
    marks = ",".join("?" * len(ids))
    pairs = store.q(f"SELECT id FROM pairs WHERE source_id IN ({marks})", tuple(ids))
    if pairs:
        pair_ids = [row["id"] for row in pairs]
        pair_marks = ",".join("?" * len(pair_ids))
        store.execute(f"DELETE FROM records WHERE pair_id IN ({pair_marks})", tuple(pair_ids))
        store.execute(f"DELETE FROM pairs WHERE id IN ({pair_marks})", tuple(pair_ids))
    store.execute(f"DELETE FROM sources WHERE id IN ({marks})", tuple(ids))
    return {"cleared": len(ids) + len(pairs)}

"""Tests for Discord ingestion, run against the bot's real export.

Run with:  python3 test_ingest.py

The fixtures are bot/backup/Heketon_*, not invented ones. That directory is what
the bot in bot/ actually produced, and it disagrees with the platform in two
ways that used to pass silently: it writes `message_id` where the platform read
`id`, and it omits the `guild` key. Both are covered here.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import zipfile

os.environ["WEAVE_DATA"] = tempfile.mkdtemp(prefix="weave-ingest-test-")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient  # noqa: E402

import app as application  # noqa: E402
import ingest  # noqa: E402
import store  # noqa: E402

BOT_BACKUP = pathlib.Path(
    os.environ.get("WEAVE_BOT_BACKUP", "/Users/aludayalu/weave/bot/backup"))
GUILD = "Heketon_1553523686949265598"

OK = 0
FAIL = 0


def check(label: str, condition: bool, extra: object = "") -> None:
    global OK, FAIL
    if condition:
        OK += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}  {extra}")


def channel_files() -> list[pathlib.Path]:
    return sorted((BOT_BACKUP / GUILD).glob("*.json"))


def busy_channel() -> pathlib.Path:
    for path in channel_files():
        if json.loads(path.read_text()).get("messages"):
            return path
    raise AssertionError(f"no channel with messages under {BOT_BACKUP / GUILD}")


def quiet_channel() -> pathlib.Path:
    for path in channel_files():
        if not json.loads(path.read_text()).get("messages"):
            return path
    return None


def zip_of_bot() -> str:
    handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    with zipfile.ZipFile(handle.name, "w") as zf:
        for path in (BOT_BACKUP / GUILD).rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(BOT_BACKUP / GUILD).as_posix())
    return handle.name


def main() -> int:
    store.init()
    print("the bot's real export")
    pair = store.new_id("pair")
    stats = ingest.ingest_discord(pair, str(BOT_BACKUP / GUILD),
                                  guild_id="1553523686949265598", guild_name="Heketon")
    check("reads the bot directory", stats["channels"] == 2, stats)
    check("every message landed", stats["messages"] == 38, stats)
    check("attachment detected", stats["with_attachments"] == 1, stats)

    rows = store.q("SELECT external_id, ts, payload FROM records WHERE pair_id = ?", (pair,))
    check("38 rows written", len(rows) == 38, len(rows))
    # the bug that started this: the bot writes message_id, the platform read id
    check("no null external_id", all(r["external_id"] for r in rows),
          [r["external_id"] for r in rows[:3]])
    check("no null timestamps", all(r["ts"] for r in rows))
    check("no duplicate external ids",
          len({r["external_id"] for r in rows}) == len(rows))

    first = json.loads(rows[0]["payload"])
    check("guild id resolved", first["guild_id"] == "1553523686949265598", first["guild_id"])
    check("author captured", bool(first["author_name"]), first["author_name"])
    check("channel captured", first["channel"] in ("general", "General"), first["channel"])

    attached = [json.loads(r["payload"]) for r in rows if json.loads(r["payload"])["attachments"]]
    check("attachment name kept", attached and attached[0]["attachments"][0]["name"] == "nono.mp3",
          attached[0]["attachments"] if attached else None)

    print("\nshapes other than the bot's")
    exported = json.loads(busy_channel().read_text())
    # DiscordChatExporter spells it id and carries guild
    dce = {"guild": {"id": "999", "name": "Other"}, **exported,
           "messages": [{**m, "id": m["message_id"]} for m in exported["messages"]]}
    dce["messages"][0].pop("message_id", None)
    pair2 = store.new_id("pair")
    stats2 = ingest.ingest_discord(pair2, json.dumps(dce).encode())
    check("DiscordChatExporter shape", stats2["messages"] == 38, stats2)
    rows2 = store.q("SELECT external_id, payload FROM records WHERE pair_id = ?", (pair2,))
    check("  its ids are read", all(r["external_id"] for r in rows2))
    check("  its guild is read", json.loads(rows2[0]["payload"])["guild_id"] == "999",
          json.loads(rows2[0]["payload"])["guild_id"])

    print("\nhostile input")
    for label, blob in [
        ("not json at all", b"hello"),
        ("json that is not a channel", b'{"hello": 1}'),
        ("a zip with nothing in it", None),
    ]:
        try:
            if blob is None:
                handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
                with zipfile.ZipFile(handle.name, "w"):
                    pass
                blob = pathlib.Path(handle.name).read_bytes()
            ingest.ingest_discord(store.new_id("pair"), blob)
            check(f"rejects {label}", False, "accepted it")
        except Exception as error:
            check(f"rejects {label}", True, type(error).__name__)

    print("\nthrough the http endpoint")
    client = TestClient(application.app)
    client.post("/api/auth/signup", json={"email": "ingest@weave.dev", "password": "longenough1"})

    response = client.post("/api/sources/discord",
                           files={"file": ("heketon.zip", open(zip_of_bot(), "rb"), "application/zip")},
                           data={"guild_name": "Heketon"})
    check("zip upload accepted", response.status_code == 200,
          f"{response.status_code} {response.text[:160]}")
    if response.status_code == 200:
        got = response.json()["ingested"]
        check("  38 messages", got["messages"] == 38, got)

    response = client.post("/api/sources/discord",
                           files={"file": (busy_channel().name,
                                           open(busy_channel(), "rb"), "application/json")})
    check("single channel json accepted", response.status_code == 200,
          f"{response.status_code} {response.text[:160]}")

    quiet = quiet_channel()
    if quiet is not None:
        response = client.post("/api/sources/discord",
                               files={"file": (quiet.name, open(quiet, "rb"), "application/json")})
        check("empty channel refused rather than half imported", response.status_code == 400,
              response.status_code)
        check("  and it says why", "no messages" in response.json().get("detail", ""),
              response.json().get("detail"))

    response = client.post("/api/sources/discord",
                           files={"file": ("notes.txt", b"just text", "text/plain")})
    check("junk refused", response.status_code == 400, response.status_code)
    check("  with a useful message", "zip" in response.json().get("detail", "").lower(),
          response.json().get("detail"))

    response = client.post("/api/sources/discord", data={})
    check("no file refused", response.status_code == 400, response.status_code)

    stranger = TestClient(application.app)
    response = stranger.post("/api/sources/discord",
                             files={"file": ("x.zip", b"PK", "application/zip")})
    check("unauthenticated refused", response.status_code == 401, response.status_code)

    response = client.get("/api/sources")
    sources = response.json()["sources"]
    check("sources listed with pair ids", all(s.get("pair_id") for s in sources), sources[:1])
    check("record counts reported", any(s["records"] > 0 for s in sources),
          [(s["name"], s["records"]) for s in sources])

    print(f"\n{OK} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

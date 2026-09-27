"""Turning Discord history into records, whatever shape it arrives in.

Three producers exist and they do not agree on field names:

  1. the bot in bot/, which writes one JSON file per channel with
     `message_id` and no guild key
  2. DiscordChatExporter, which writes a zip with `id` and a `guild` key
  3. a plain directory of JSON files, which is what the bot leaves on disk

Reading each shape with its own ad hoc parser is how `external_id` ended up
None for every message the bot produced. So there is one parser here, it is
told which shape it is looking at, and it is tested against the real export in
bot/backup rather than against a fixture someone invented.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path
from typing import Iterator

import store

TICKET = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
ATTACHMENT_SUFFIXES = {".mp3", ".mp4", ".png", ".jpg", ".jpeg", ".gif", ".webp",
                       ".pdf", ".txt", ".md", ".json", ".csv", ".log", ".zip", ".wav", ".m4a"}


# ------------------------------------------------------------------ shapes
def _first(payload: dict, *names: str):
    """Return the first present key, so both spellings of a field work."""
    for name in names:
        if payload.get(name) not in (None, ""):
            return payload[name]
    return None


def _epoch(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        # Discord snowflakes are not timestamps, so only treat short numbers as epochs
        return float(text) if len(text) <= 11 else None
    try:
        from datetime import datetime, timezone
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def normalize_channel(payload: dict, guild_id: str = "", guild_name: str = "") -> Iterator[dict]:
    """Yield one canonical record per message, whatever the export called things."""
    channel = payload.get("channel") or {}
    guild = payload.get("guild") or {}
    channel_name = channel.get("name") or "unknown"
    channel_id = str(channel.get("id") or "")
    resolved_guild = str(guild.get("id") or guild_id or "")

    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        author = message.get("author") or {}
        if isinstance(author, str):
            author = {"name": author}
        content = message.get("content") or ""
        attachments = message.get("attachments") or []
        filenames = [
            a.get("original_filename") or a.get("filename") or ""
            for a in attachments if isinstance(a, dict)
        ]
        record_id = _first(message, "message_id", "id", "ID")
        if record_id is None:
            continue
        yield {
            "stream": "discord_message",
            "external_id": str(record_id),
            "ts": _epoch(_first(message, "timestamp", "Timestamp", "ts")),
            "payload": {
                "message_id": str(record_id),
                "channel": channel_name,
                "channel_id": channel_id,
                "guild_id": resolved_guild or None,
                "guild_name": guild_name or guild.get("name") or None,
                "author_id": str(author.get("id") or ""),
                "author_name": author.get("name") or author.get("nickname") or "",
                "ts_utc": _first(message, "timestamp", "Timestamp"),
                "content": content,
                "edited": message.get("edited"),
                "jump_url": message.get("jump_url"),
                "mentions_ticket": sorted(set(TICKET.findall(content))),
                "attachments": [
                    {"name": n, "size": a.get("size"), "type": a.get("content_type")}
                    for n, a in zip(filenames, [x for x in attachments if isinstance(x, dict)])
                ],
            },
        }


# ------------------------------------------------------------------ inputs
def _read_json_blob(blob: bytes) -> dict | None:
    try:
        payload = json.loads(blob)
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def iter_payloads(archive: bytes | str | Path) -> Iterator[dict]:
    """Yield every channel payload from a zip, a directory, or one JSON blob."""
    if isinstance(archive, (str, Path)):
        path = Path(archive)
        if path.is_dir():
            for child in sorted(path.rglob("*.json")):
                payload = _read_json_blob(child.read_bytes())
                if payload and "messages" in payload:
                    yield payload
            return
        if path.is_file():
            payload = _read_json_blob(path.read_bytes())
            if payload and "messages" in payload:
                yield payload
            return
        raise FileNotFoundError(path)

    if isinstance(archive, str):
        archive = archive.encode()

    try:
        zf = zipfile.ZipFile(io.BytesIO(archive))
    except zipfile.BadZipFile:
        payload = _read_json_blob(archive)
        if payload and "messages" in payload:
            yield payload
            return
        raise ValueError("that file is neither a zip nor a single channel export")

    with zf:
        for name in zf.namelist():
            if not name.lower().endswith(".json"):
                continue
            payload = _read_json_blob(zf.read(name))
            if payload and "messages" in payload:
                yield payload


def ingest_discord(pair_id: str, archive: bytes | str | Path,
                   guild_id: str = "", guild_name: str = "") -> dict:
    """Write every message in the archive to records. Returns counts."""
    stats = {"channels": 0, "messages": 0, "with_attachments": 0, "with_tickets": 0,
             "earliest": None, "latest": None}
    rows: list[tuple] = []
    for payload in iter_payloads(archive):
        stats["channels"] += 1
        for record in normalize_channel(payload, guild_id, guild_name):
            stats["messages"] += 1
            if record["payload"]["attachments"]:
                stats["with_attachments"] += 1
            if record["payload"]["mentions_ticket"]:
                stats["with_tickets"] += 1
            stamp = record["ts"]
            if stamp is not None:
                if stats["earliest"] is None or stamp < stats["earliest"]:
                    stats["earliest"] = stamp
                if stats["latest"] is None or stamp > stats["latest"]:
                    stats["latest"] = stamp
            rows.append((store.new_id("rec"), pair_id, record["stream"],
                         record["external_id"], stamp, store.jdump(record["payload"])))

    # Refuse rather than write nothing. A silent zero here looks identical to a
    # successful import of an empty server, which is how a broken export goes
    # unnoticed until someone asks why the dashboard is blank.
    if not stats["channels"]:
        raise ValueError(
            "no channel exports in that input. Expected a zip of per channel json "
            "files, a directory of them, or a single channel json.")
    if not stats["messages"]:
        raise ValueError(
            f"found {stats['channels']} channel file(s) but no messages in them")

    if rows:
        store.execute_many(
            "INSERT INTO records (id, pair_id, stream, external_id, ts, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)", rows)
    return stats


def scan_archive(archive: bytes | str | Path) -> dict:
    """Count what is in an archive without writing anything."""
    stats = {"channels": 0, "messages": 0, "bytes": 0, "filenames": []}
    if isinstance(archive, (str, Path)) and Path(archive).is_dir():
        for child in sorted(Path(archive).rglob("*")):
            if child.is_file():
                stats["bytes"] += child.stat().st_size
                if child.suffix.lower() in ATTACHMENT_SUFFIXES:
                    stats["filenames"].append(child.name)
    elif isinstance(archive, bytes):
        stats["bytes"] = len(archive)
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as zf:
                for info in zf.infolist():
                    if Path(info.filename).suffix.lower() in ATTACHMENT_SUFFIXES:
                        stats["filenames"].append(Path(info.filename).name)
        except zipfile.BadZipFile:
            pass
    for payload in iter_payloads(archive):
        stats["channels"] += 1
        stats["messages"] += len(payload.get("messages") or [])
    return stats

"""Storage for the platform.

One thin layer over two backends so the same code runs on a laptop and on
Databricks Apps:

  * sqlite  - local development, zero setup, a file
  * postgres - Lakebase, when DATABASE_URL is set, which is what Databricks Apps
               injects

Nothing above this module knows which one it is talking to.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get("WEAVE_DATA", Path(__file__).resolve().parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

_local = threading.local()


# ------------------------------------------------------------------ backends
def _sqlite():
    if not hasattr(_local, "conn"):
        conn = sqlite3.connect(DATA_DIR / "weave.db", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        _local.conn = conn
    return _local.conn


_pg = None


def _postgres():
    global _pg
    if _pg is None:
        import psycopg
        from psycopg.rows import dict_row

        _pg = psycopg.connect(DATABASE_URL, autocommit=False, connect_timeout=20,
                               row_factory=dict_row)
    return _pg


def backend() -> str:
    return "postgres" if DATABASE_URL else "sqlite"


@contextmanager
def cursor():
    """Yield a cursor that takes dict rows, whichever backend is live."""
    if backend() == "postgres":
        conn = _postgres()
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    else:
        conn = _sqlite()
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


def q(sql: str, params: tuple = ()) -> list[dict]:
    """All SQL here is written with ? placeholders.

    SQLite takes those natively. psycopg wants %s, so that is the only place a
    translation happens. Translating for SQLite instead produced
    `near "%": syntax error` on the very first query.
    """
    if backend() == "postgres":
        return _run(sql.replace("?", "%s"), params)
    return _run(sql, params)


def q1(sql: str, params: tuple = ()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def _run(sql: str, params: tuple) -> list[dict]:
    with cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return []
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]


def execute(sql: str, params: tuple = ()) -> None:
    q(sql, params)


def execute_many(sql: str, rows: list[tuple]) -> None:
    if not rows:
        return
    if backend() == "postgres":
        sql = sql.replace("?", "%s")
    with cursor() as cur:
        for row in rows:
            cur.execute(sql, row)


def insert(table: str, values: dict) -> None:
    keys = list(values)
    placeholders = ", ".join(["?"] * len(keys))
    columns = ", ".join(keys)
    execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            tuple(values[k] for k in keys))


def jdump(value) -> str:
    return json.dumps(value, default=str)


def jload(value, fallback=None):
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


# ------------------------------------------------------------------ schema
# Written once, tolerant of both dialects, so a fresh database needs no migration.
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
        name TEXT, created_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at REAL NOT NULL,
        expires_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS api_keys (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT,
        key_hash TEXT UNIQUE NOT NULL, prefix TEXT, created_at REAL NOT NULL,
        revoked INTEGER DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS sources (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL,
        name TEXT NOT NULL, config TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL, last_synced_at REAL, status TEXT DEFAULT 'pending',
        detail TEXT)""",
    """CREATE TABLE IF NOT EXISTS pairs (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, source_id TEXT NOT NULL,
        guild_id TEXT, repo_url TEXT, label TEXT, created_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS records (
        id TEXT PRIMARY KEY, pair_id TEXT NOT NULL, stream TEXT NOT NULL,
        external_id TEXT, ts REAL, payload TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, pair_id TEXT,
        status TEXT NOT NULL, stage TEXT, started_at REAL NOT NULL,
        finished_at REAL, error TEXT, stats TEXT DEFAULT '{}')""",
    """CREATE TABLE IF NOT EXISTS run_events (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT, status TEXT,
        message TEXT, detail TEXT, at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS models (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, run_id TEXT, version INTEGER NOT NULL,
        name TEXT NOT NULL, base_model TEXT, status TEXT, endpoint TEXT,
        metrics TEXT DEFAULT '{}', created_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS wallet (
        user_id TEXT PRIMARY KEY, credits REAL NOT NULL DEFAULT 0,
        spent REAL NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS ledger (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, at REAL NOT NULL,
        amount REAL NOT NULL, kind TEXT NOT NULL, note TEXT)""",
    """CREATE TABLE IF NOT EXISTS inference_log (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, model_id TEXT, key_id TEXT,
        at REAL NOT NULL, prompt_tokens INTEGER DEFAULT 0,
        completion_tokens INTEGER DEFAULT 0, credits REAL NOT NULL DEFAULT 0,
        status TEXT, latency_ms INTEGER, detail TEXT)""",
]


def init() -> None:
    for statement in SCHEMA:
        execute(statement)
    if backend() == "sqlite":
        for table, column, ddl in (
            ("records", "ts", "CREATE INDEX IF NOT EXISTS records_pair_ts ON records(pair_id, ts)"),
            ("records", "stream", "CREATE INDEX IF NOT EXISTS records_pair_stream ON records(pair_id, stream)"),
            ("run_events", "run_id", "CREATE INDEX IF NOT EXISTS run_events_run ON run_events(run_id, at)"),
            ("inference_log", "user_id", "CREATE INDEX IF NOT EXISTS inference_user ON inference_log(user_id, at)"),
            ("models", "user_id", "CREATE INDEX IF NOT EXISTS models_user ON models(user_id, version)"),
        ):
            execute(ddl)
        _ = column


# ------------------------------------------------------------------ helpers
def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


# ------------------------------------------------------------------ auth
def hash_password(password: str, salt: str | None = None) -> str:
    """PBKDF2, because a demo still should not store a password in the clear."""
    import hashlib

    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    import hashlib

    try:
        salt, _ = stored.split("$", 1)
        expected = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
        return secrets.compare_digest(expected, stored.split("$", 1)[1])
    except (ValueError, AttributeError):
        return False


def hash_key(key: str) -> str:
    import hashlib

    return hashlib.sha256(key.encode()).hexdigest()


def create_user(email: str, password: str, name: str = "") -> dict:
    user_id = new_id("usr")
    execute(
        "INSERT INTO users (id, email, password_hash, name, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, email.strip().lower(), hash_password(password), name or email.split("@")[0], now()),
    )
    execute("INSERT INTO wallet (user_id, credits, spent) VALUES (?, 0, 0)", (user_id,))
    return q1("SELECT * FROM users WHERE id = ?", (user_id,)) or {}


def get_user(email: str) -> dict | None:
    return q1("SELECT * FROM users WHERE email = ?", (email.strip().lower(),))


def start_session(user_id: str, hours: int = 24 * 14) -> str:
    token = secrets.token_urlsafe(32)
    execute("INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now(), now() + hours * 3600))
    return token


def user_for_token(token: str) -> dict | None:
    row = q1(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token = ? AND s.expires_at > ?",
        (token, now()),
    )
    return row


def issue_api_key(user_id: str, name: str) -> tuple[str, dict]:
    raw = "wk-" + secrets.token_urlsafe(30)
    key_id = new_id("key")
    execute(
        "INSERT INTO api_keys (id, user_id, name, key_hash, prefix, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (key_id, user_id, name, hash_key(raw), raw[:11], now()),
    )
    return raw, q1("SELECT * FROM api_keys WHERE id = ?", (key_id,)) or {}


def user_for_api_key(raw: str) -> dict | None:
    row = q1("SELECT * FROM api_keys WHERE key_hash = ? AND revoked = 0", (hash_key(raw),))
    if not row:
        return None
    return q1("SELECT * FROM users WHERE id = ?", (row["user_id"],))


# ------------------------------------------------------------------ credits
def credit(user_id: str, amount: float, kind: str, note: str = "") -> None:
    execute("INSERT INTO ledger (id, user_id, at, amount, kind, note) VALUES (?, ?, ?, ?, ?, ?)",
            (new_id("led"), user_id, now(), amount, kind, note))
    if kind == "grant":
        execute("UPDATE wallet SET credits = credits + ? WHERE user_id = ?", (amount, user_id))
    elif kind == "spend":
        execute("UPDATE wallet SET spent = spent + ? WHERE user_id = ?", (amount, user_id))


def wallet(user_id: str) -> dict:
    return q1("SELECT * FROM wallet WHERE user_id = ?", (user_id,)) or {"credits": 0.0, "spent": 0.0}


# ------------------------------------------------------------------ runs
def start_run(user_id: str, pair_id: str | None) -> str:
    run_id = new_id("run")
    execute(
        "INSERT INTO runs (id, user_id, pair_id, status, stage, started_at, stats) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, user_id, pair_id, "running", "queued", now(), jdump({})),
    )
    return run_id


def event(run_id: str, stage: str, status: str, message: str, detail=None) -> None:
    execute(
        "INSERT INTO run_events (id, run_id, stage, status, message, detail, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (new_id("evt"), run_id, stage, status, message, jdump(detail or {}), now()),
    )


def finish_run(run_id: str, status: str, stage: str, stats: dict | None = None, error: str = "") -> None:
    execute(
        "UPDATE runs SET status = ?, stage = ?, finished_at = ?, stats = ?, error = ? WHERE id = ?",
        (status, stage, now(), jdump(stats or {}), error, run_id),
    )


def run_events(run_id: str) -> list[dict]:
    rows = q("SELECT * FROM run_events WHERE run_id = ? ORDER BY at", (run_id,))
    for row in rows:
        row["detail"] = jload(row.get("detail"), {})
    return rows


def next_version(user_id: str) -> int:
    row = q1("SELECT COALESCE(MAX(version), 0) AS v FROM models WHERE user_id = ?", (user_id,))
    return int(row["v"]) + 1 if row else 1

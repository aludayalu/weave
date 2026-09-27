import sqlite3
import json
import threading
import weakref
import os

_connection_locks = {}
_registry_lock = threading.RLock()


def _get_lock(conn):
    conn_id = id(conn)

    with _registry_lock:
        lock = _connection_locks.get(conn_id)

        if lock is None:
            lock = threading.RLock()
            _connection_locks[conn_id] = lock

        return lock


class Connection:
    def __init__(self, conn):
        self.conn = conn
        self.lock = _get_lock(conn)

        weakref.finalize(
            self,
            lambda cid=id(conn): _connection_locks.pop(cid, None)
        )

    def set(self, key, val):
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO main (x, y)
                VALUES (?, ?)
                ON CONFLICT(x)
                DO UPDATE SET y = excluded.y
                """,
                (key, json.dumps(val))
            )
            self.conn.commit()

    def get(self, key, default = None):
        with self.lock:
            row = self.conn.execute(
                "SELECT y FROM main WHERE x = ?",
                (key,)
            ).fetchone()

            if row is None or len(row) == 0:
                return default

            return json.loads(row[0])

    def get_all(self, n=-1):
        with self.lock:
            if n == -1:
                rows = self.conn.execute(
                    "SELECT x, y FROM main ORDER BY rowid DESC"
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT x, y FROM main ORDER BY rowid DESC LIMIT ?",
                    (n,)
                ).fetchall()

            return [(x, json.loads(y)) for x, y in rows]

    def delete(self, key):
        with self.lock:
            self.conn.execute(
                "DELETE FROM main WHERE x = ?",
                (key,)
            )
            self.conn.commit()

    def count_all(self):
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM main"
            ).fetchone()[0]

    def close(self):
        with _registry_lock:
            _connection_locks.pop(id(self.conn), None)

        self.conn.close()


def get_conn(name):
    try:os.mkdir("databases")
    except:pass

    conn = sqlite3.connect(
        f"databases/{name}.db",
        check_same_thread=False
    )

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS main (
            x TEXT PRIMARY KEY,
            y TEXT
        )
    """)

    return Connection(conn)
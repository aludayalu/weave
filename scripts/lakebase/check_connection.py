#!/usr/bin/env python3
"""Check that Lakebase is reachable and see what is already in it.

    python3 check_connection.py

Prints credentials (masked), connects, reports the server version, which of
our schemas/tables exist, and current row counts. Safe to run repeatedly.
"""
import sys

from credentials import describe, resolve_dsn, config


def main() -> int:
    print("== credentials")
    print(describe())

    c = config()
    if not c["shape"]:
        print("\nNothing to connect to yet. See the instructions printed above.")
        return 1

    print("\n== driver")
    try:
        import psycopg
        print(f"psycopg {psycopg.__version__}")
    except ImportError:
        print("psycopg not installed -> pip install 'psycopg[binary]'")
        return 2

    print("\n== connecting")
    try:
        dsn = resolve_dsn()
    except SystemExit as e:
        print(e)
        return 1

    try:
        with psycopg.connect(dsn, connect_timeout=15) as conn, conn.cursor() as cur:
            cur.execute("select version(), current_database(), current_user")
            ver, db, user = cur.fetchone()
            print("connected")
            print(f"  database : {db}")
            print(f"  user     : {user}")
            print(f"  server   : {ver.split(',')[0]}")

            cur.execute("""
                select table_schema, table_name from information_schema.tables
                 where table_schema in ('ingest','bronze','silver','detected','gold','ops')
                   and table_type = 'BASE TABLE'
                 order by 1, 2""")
            rows = cur.fetchall()
            if not rows:
                print("\n  no pipeline tables yet -> run:")
                print("    psql \"$LAKEBASE_DSN\" -f schema.sql")
                print("    python3 load_lakebase.py")
                return 0

            by_schema = {}
            for s, t in rows:
                by_schema.setdefault(s, []).append(t)
            print(f"\n== schema ({len(rows)} tables)")
            for s in sorted(by_schema):
                print(f"  {s:<9} {len(by_schema[s]):>2} tables")

            print("\n== row counts")
            for s, t in sorted(rows):
                try:
                    cur.execute(f'select count(*) from "{s}"."{t}"')
                    n = cur.fetchone()[0]
                except Exception:
                    conn.rollback()
                    n = "err"
                print(f"  {s + '.' + t:<38} {n}")

            try:
                cur.execute("select stream, last_ts from ingest.watermark order by stream")
                print("\n== watermarks")
                for stream, ts in cur.fetchall():
                    print(f"  {stream:<9} {ts}")
            except Exception:
                conn.rollback()

    except Exception as e:
        print(f"\nFAILED: {type(e).__name__}: {e}")
        print("\nChecklist:")
        print("  - instance is running and reachable from this network")
        print("  - the DSN uses sslmode=require")
        print("  - the user has CREATE on the target database")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

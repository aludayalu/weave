#!/usr/bin/env python3
"""Credential loader for Lakebase.

Reads credentials from (in order): process env -> ./.env -> ~/.databrickscfg.
Never prints a secret; every value is masked. Exposes resolve_dsn() for
load_lakebase.py and answers `check_connection.py`.

Two auth shapes:
  A  Databricks SDK: DATABRICKS_HOST + LAKEBASE_INSTANCE + DATABRICKS_TOKEN.
     The SDK mints a short-lived database password, so no long-lived secret
     is stored anywhere.
  B  Plain DSN: LAKEBASE_DSN=postgresql://user:pass@host:5432/db?sslmode=require
"""
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent


def mask(value: str, keep: int = 4) -> str:
    if not value:
        return "(unset)"
    if "://" in value:
        scheme, rest = value.split("://", 1)
        if "@" in rest:
            creds, host = rest.rsplit("@", 1)
            user = creds.split(":", 1)[0]
            return f"{scheme}://{user}:••••••@{host}"
        return f"{scheme}://••••••"
    if len(value) <= keep:
        return "•" * len(value)
    return value[:keep] + "…" + "•" * 6


def load_dotenv(path: Path = None) -> dict:
    """Minimal .env reader; does not overwrite real env vars."""
    path = path or (HERE / ".env")
    out = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip("'\"")
        if v:
            out[k] = v
    for k, v in out.items():
        os.environ.setdefault(k, v)
    return out


def config() -> dict:
    """Resolve credentials from env/.env, with a Databricks profile fallback."""
    file_env = load_dotenv()
    host = os.environ.get("DATABRICKS_HOST")
    instance = os.environ.get("LAKEBASE_INSTANCE")
    token = os.environ.get("DATABRICKS_TOKEN")
    dsn = os.environ.get("LAKEBASE_DSN")

    if not token:
        # last resort: a databricks CLI/auth profile
        cfg = Path.home() / ".databrickscfg"
        if cfg.exists():
            text = cfg.read_text()
            for key, var in (("host", "DATABRICKS_HOST"), ("token", "DATABRICKS_TOKEN")):
                m = re.search(rf"^{key}\s*=\s*(.+)$", text, re.M)
                if m and not os.environ.get(var):
                    os.environ[var] = m.group(1).strip()

    return {
        "shape": "sdk" if (host and instance and (token or Path.home().joinpath(".databrickscfg").exists()))
                 else ("dsn" if dsn else None),
        "host": os.environ.get("DATABRICKS_HOST"),
        "instance": os.environ.get("LAKEBASE_INSTANCE"),
        "token": os.environ.get("DATABRICKS_TOKEN"),
        "dsn": dsn,
        "from_dotenv": bool(file_env),
        "dotenv_path": str(HERE / ".env"),
    }


def resolve_dsn() -> str:
    """Return a Postgres DSN, minting one via the SDK when configured that way."""
    c = config()
    if c["shape"] == "dsn":
        return c["dsn"]
    if c["shape"] == "sdk":
        try:
            from databricks.sdk import WorkspaceClient
        except ImportError as e:
            raise SystemExit(
                "Databricks SDK not installed. Either:\n"
                "  pip install databricks-sdk      (then re-run)\n"
                "or set LAKEBASE_DSN=... in "
                f"{c['dotenv_path']}\n({e})")
        w = WorkspaceClient(host=c["host"], token=c["token"]) if c["token"] \
            else WorkspaceClient(host=c["host"])
        inst = w.database.get_database_instance(name=c["instance"])
        return inst.read_write_dsn
    raise SystemExit(
        "No Lakebase credentials found.\n"
        f"Copy {c['dotenv_path'] + '.example'} -> {c['dotenv_path']} and fill in ONE of:\n"
        "  A) DATABRICKS_HOST + LAKEBASE_INSTANCE + DATABRICKS_TOKEN  (preferred)\n"
        "  B) LAKEBASE_DSN=postgresql://user:pass@host:5432/db?sslmode=require\n"
        "Then run: python3 check_connection.py")


def describe() -> str:
    c = config()
    lines = [f"credential shape : {c['shape'] or 'NOT CONFIGURED'}",
             f".env present     : {c['from_dotenv']} ({c['dotenv_path']})"]
    if c["shape"] == "sdk":
        lines += [f"workspace host   : {c['host']}",
                  f"lakebase instance: {c['instance']}",
                  f"token            : {mask(c['token'] or '(from ~/.databrickscfg)')}"]
    elif c["shape"] == "dsn":
        lines.append(f"dsn              : {mask(c['dsn'])}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())

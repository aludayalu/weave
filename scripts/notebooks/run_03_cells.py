#!/usr/bin/env python3
"""Run 03_silver_to_gold.ipynb cell by cell against the local Postgres.

Same discipline as run_notebook_cells.py: each cell executes separately against
one shared namespace, so a definition the builder dropped shows up as a
NameError here rather than on the cluster.

The only things faked are the two that cannot exist locally: the Databricks SDK
(which is redirected at 127.0.0.1) and the model call, which is served from a
recorded response so the run costs nothing and is repeatable.
"""
import json
import pathlib
import sys
import types
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
NOTEBOOK = HERE / "03_silver_to_gold.ipynb"
RECORDED = pathlib.Path("/tmp/recorded_model_response.json")

WIDGETS = {
    "instance": "local",
    "db_user": "postgres",
    "db_name": "tribal",
    "db_host": "127.0.0.1",
    "db_port": "55432",
    "bronze_db": "bronze",
    "silver_db": "silver",
    "gold_db": "gold",
    "window_source": "auto",
    "reference_windows": "",
    "reference_base": "/Users/aludayalu/weave/opencode/synth-data",
    "model_provider_service": "",
    "serving_endpoint": "",          # blank, so the direct route is used
    "fallback_model": "stealth/space-bunny-alpha",
    "max_output_tokens": "48000",
}


# ---------------------------------------------------------------- shims
def make_dbutils():
    m = types.ModuleType("dbutils")
    m.widgets = types.SimpleNamespace(
        text=lambda *a, **k: None,
        get=lambda n="": WIDGETS.get(n, ""),
        getAll=lambda: [],
    )
    m.fs = types.SimpleNamespace()
    m.secrets = types.SimpleNamespace(
        get=lambda **k: (_ for _ in ()).throw(Exception("no secret")))
    m.notebook = lambda: types.SimpleNamespace(context=types.SimpleNamespace(
        userName="local@example.com"))
    m.library = types.SimpleNamespace(restartPython=lambda: None)
    return m


class FakeCredential:
    token = "not-a-real-token"
    expire_time = "local"


class FakeInstance:
    read_write_dns = "127.0.0.1"


class FakeDatabase:
    def list_database_instances(self):
        return [types.SimpleNamespace(name="local")]

    def get_database_instance(self, name=None):
        return FakeInstance()

    def generate_database_credential(self, **kwargs):
        return FakeCredential()


def install_shims():
    sdk = types.ModuleType("databricks.sdk")
    db_mod = types.ModuleType("databricks.sdk.database")

    class WorkspaceClient:
        def _get_secret(self, scope, key):
            import os
            if key == "openrouter":
                return types.SimpleNamespace(
                    value=os.environ.get("ORK", "unused"), value_or_raise=lambda: "unused")
            raise Exception(f"no secret {scope}/{key}")

        def __init__(self, *a, **k):
            self.database = FakeDatabase()
            self.config = types.SimpleNamespace(
                host="local", auth=types.SimpleNamespace(token="not-a-real-token"))

        @property
        def secrets(self):
            return types.SimpleNamespace(get_secret=self._get_secret)

    sdk.WorkspaceClient = WorkspaceClient
    sys.modules["databricks.sdk"] = sdk
    sys.modules["databricks.sdk.database"] = db_mod

    # the OpenRouter key, so the notebook takes the direct route
    dbutils = make_dbutils()
    sys.modules["dbutils"] = dbutils

    def secret_get(scope, key):
        if key == "openrouter":
            import os
            return types.SimpleNamespace(value=os.environ.get("ORK", "unused"))
        raise Exception(f"no secret {scope}/{key}")

    dbutils.secrets.get_secret = secret_get

    # serve the model from a recording, so the run is free and repeatable
    recorded = json.loads(RECORDED.read_text())
    real_urlopen = urllib.request.urlopen

    def fake_urlopen(request, *a, **k):
        url = getattr(request, "full_url", str(request))
        if "openrouter.ai" in url or "ai-gateway" in url:
            body = json.dumps(recorded).encode()
            return types.SimpleNamespace(
                read=lambda: body, __enter__=lambda s: s, __exit__=lambda s, *x: None)
        return real_urlopen(request, *a, **k)

    urllib.request.urlopen = fake_urlopen

    # Lakebase requires TLS and the notebook rightly insists on it. The local
    # Postgres has no SSL, so relax it here rather than weakening the notebook.
    import psycopg
    real_connect = psycopg.connect

    def local_connect(*args, **kwargs):
        kwargs["sslmode"] = "disable"
        return real_connect(*args, **kwargs)

    psycopg.connect = local_connect

    return dbutils


# ---------------------------------------------------------------- run
def main():
    if not RECORDED.exists():
        raise SystemExit(f"missing {RECORDED}; record a model reply first")

    notebook = json.loads(NOTEBOOK.read_text())
    dbutils = install_shims()
    scope = {"__name__": "__main__", "dbutils": dbutils}

    code_cells = [c for c in notebook["cells"] if c["cell_type"] == "code"]
    print(f"=== {NOTEBOOK.name}: {len(notebook['cells'])} cells, {len(code_cells)} code\n")

    for index, cell in enumerate(notebook["cells"]):
        if cell["cell_type"] != "code":
            continue
        source = "".join(cell["source"])
        if not source.strip():
            continue
        label = source.strip().splitlines()[0][:66]
        try:
            exec(compile(source, f"cell{index}", "exec"), scope)
            print(f"  ok    cell {index:>2}  {label}")
        except Exception as error:
            print(f"  FAIL  cell {index:>2}  {label}")
            print(f"        {type(error).__name__}: {error}")
            for line in source.splitlines()[:10]:
                print("        |", line)
            return 1
    print("\nall cells ran")
    return 0


if __name__ == "__main__":
    sys.exit(main())

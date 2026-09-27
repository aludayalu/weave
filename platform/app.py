"""The weave web app: one FastAPI process for the site, the dashboard and the API.

Run locally with:  uvicorn app:app --reload
Run on Databricks Apps with:  databricks apps deploy (see databricks.yml)

Everything the product needs is behind three surfaces:

  /                        the marketing page, no auth
  /app                     the dashboard, session cookie auth
  /v1/chat/completions      OpenAI shaped inference, API key auth

Databricks Apps notes:

  * the app is stateless, so all state goes in the database. DATABASE_URL is
    supplied by Apps and points at the app's own Lakebase instance. Without it
    store.py falls back to SQLite under WEAVE_DATA, which is what local dev uses.
  * Apps injects the workspace client as a service principal, so pipeline work
    that needs the serving API can use WorkspaceClient() directly.
  * Port is read from the PORT env var, which Apps always sets.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                              RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import core
import demo
import ingest
import store

# FastAPI claims /docs for its own Swagger UI by default, which would shadow the
# hand written reference page. Swagger moves to /swagger, where developers can
# find it, and /docs stays human readable.
app = FastAPI(title="weave", version="0.1.0", docs_url="/swagger", redoc_url=None)

STATIC = Path(__file__).resolve().parent / "static"

# Demo data seeds a new account with a connected repo and guild so the
# dashboard has something to show. Off unless WEAVE_DEMO is set, so a real
# deployment never shows fiction by accident.
DEMO = os.environ.get("WEAVE_DEMO", "").strip().lower() in ("1", "true", "yes", "on")

# A shared secret that also authenticates against /v1. When it is set, a caller
# can use it as a bearer token and the request is forwarded to OpenRouter
# instead of being served by the account's own model. Useful for a demo where
# one key is handed out rather than a per user key. Compared in constant time.
PROXY_TOKEN = os.environ.get("WEAVE_PROXY_TOKEN", "").strip()
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
PROXY_MODEL = os.environ.get("WEAVE_PROXY_MODEL", "stealth/space-bunny-alpha")

# Proxy calls arrive with the shared secret and no account behind them, so they
# are attributed to one service account. Without this they bypass
# inference_log entirely and the usage panel silently shows nothing, which is
# worse than not having the panel at all.
PROXY_ACCOUNT_EMAIL = os.environ.get(
    "WEAVE_PROXY_ACCOUNT", "shared@weave.local")
PROXY_ACCOUNT_NAME = "shared key"


def proxy_account() -> dict:
    """The account shared key traffic is billed and logged against."""
    email = PROXY_ACCOUNT_EMAIL
    existing = store.get_user(email)
    if existing:
        return existing
    # created with an unusable random password: this account is never signed
    # into through the web, only used as a label on proxied requests
    user = store.create_user(email, secrets.token_urlsafe(32), PROXY_ACCOUNT_NAME)
    store.credit(user["id"], 0.0, "grant", "shared key account")
    return user
SESSION_COOKIE = "weave_session"
FREE_STARTING_CREDITS = 25.0


# Tables are created at import rather than on the startup event, which is
# deprecated and is skipped entirely by some test clients. init() is
# CREATE TABLE IF NOT EXISTS, so calling it more than once is harmless.
store.init()


# --------------------------------------------------------------------- auth
class Signup(BaseModel):
    email: str
    password: str
    name: str = ""


class Login(BaseModel):
    email: str
    password: str


def current_user(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        raise HTTPException(401, "sign in first")
    user = store.user_for_token(token)
    if not user:
        raise HTTPException(401, "session expired, sign in again")
    return user


def bearer(authorization: str) -> str:
    raw = (authorization or "").strip()
    return raw[7:].strip() if raw.lower().startswith("bearer ") else raw


def api_key_user(authorization: str = Header(default="")) -> dict:
    """Auth for /v1, which follows the OpenAI convention of a bearer token."""
    raw = bearer(authorization)
    if not raw:
        raise HTTPException(401, "missing API key")
    user = store.user_for_api_key(raw)
    if not user:
        raise HTTPException(401, "that API key is not valid")
    return user


def api_or_proxy(authorization: str = Header(default="")) -> dict | None:
    """Accept either a per account key or the shared secret.

    Returns the user for an account key, or None when the shared secret was
    presented. Raising here rather than in the handler is what stops the
    dependency from rejecting the proxy before the handler ever sees it.
    """
    raw = bearer(authorization)
    if not raw:
        raise HTTPException(401, "missing API key")
    if PROXY_TOKEN and secrets.compare_digest(raw, PROXY_TOKEN):
        return None
    user = store.user_for_api_key(raw)
    if not user:
        raise HTTPException(401, "that API key is not valid")
    return user


def is_proxy_token(authorization: str) -> bool:
    """True when the caller presented the shared secret rather than a user key.

    compare_digest so the comparison does not leak the secret's length or a
    prefix through timing.
    """
    if not PROXY_TOKEN:
        return False
    return secrets.compare_digest(bearer(authorization), PROXY_TOKEN)


def _tools_of(body) -> list | None:
    """Tool schemas the caller sent, if any."""
    return getattr(body, "tools", None)


def usage_from(frames: list[bytes]) -> tuple[int, int]:
    """Pull the usage frame out of the bytes that were just streamed.

    OpenRouter sends usage on a final chunk when include_usage is set, so the
    last data frame that carries a usage object wins. Frames may arrive split
    across reads, so they are joined before parsing.
    """
    prompt = completion = 0
    blob = b"".join(frames).decode("utf-8", "replace")
    for line in blob.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            frame = json.loads(payload)
        except ValueError:
            continue
        usage = frame.get("usage") or {}
        if usage:
            prompt = int(usage.get("prompt_tokens") or prompt)
            completion = int(usage.get("completion_tokens") or completion)
    return prompt, completion


def log_proxy(owner: dict, model: str, started: float, prompt: int,
              completion: int) -> None:
    try:
        store.insert("inference_log", {
            "id": store.new_id("inf"), "user_id": owner["id"], "model_id": None,
            "key_id": None, "at": store.now(), "prompt_tokens": prompt,
            "completion_tokens": completion,
            "credits": float(prompt + completion) / 100000.0,
            "status": "ok", "latency_ms": int((time.time() - started) * 1000),
            "detail": store.jdump({"via": "proxy", "model": model,
                                   "upstream": "openrouter", "streamed": True}),
        })
    except Exception as error:                          # noqa: BLE001
        print(f"note: could not log a proxied call: {type(error).__name__}: {error}")


def openrouter_body(messages, model, max_tokens, temperature, tools=None,
                    stream=False, reasoning=None) -> dict:
    body: dict = {"model": model or PROXY_MODEL, "max_tokens": max_tokens,
                  "messages": messages}
    if temperature is not None:
        body["temperature"] = temperature
    if tools:
        body["tools"] = tools
    if reasoning:
        body["reasoning"] = reasoning
    if stream:
        body["stream"] = True
        # without this a streamed reply carries no usage at all, so the request
        # panel would show a call with zero tokens
        body["stream_options"] = {"include_usage": True}
    return body


def stream_openrouter(body: dict, timeout: int = 300):
    """Open a streamed upstream reply and hand back the raw response.

    Returns the urllib response so the caller can pass the body through as it
    arrives, rather than buffering a whole completion to find out it was 4KB of
    data: lines.
    """
    if not OPENROUTER_KEY:
        raise HTTPException(503, "this proxy has no OpenRouter key configured")
    request = urllib.request.Request(
        OPENROUTER_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {OPENROUTER_KEY}",
                 "X-Title": "weave"})
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        detail = error.read().decode()[:400]
        raise HTTPException(502, f"openrouter returned {error.code}: {detail}")
    except urllib.error.URLError as error:
        raise HTTPException(502, f"could not reach openrouter: {error}")


def call_openrouter(messages: list[dict], model: str, max_tokens: int,
                    temperature: float | None) -> dict:
    """Forward to OpenRouter and return the upstream reply verbatim.

    The response is passed through as it came back, so the caller sees the same
    shape OpenRouter gives them directly, including the model actually served
    and its usage.
    """
    if not OPENROUTER_KEY:
        raise HTTPException(503, "this proxy has no OpenRouter key configured")
    body: dict = {"model": model or PROXY_MODEL, "max_tokens": max_tokens,
                  "messages": messages}
    if temperature is not None:
        body["temperature"] = temperature
    request = urllib.request.Request(
        OPENROUTER_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {OPENROUTER_KEY}",
                 "X-Title": "weave"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode()[:400]
        raise HTTPException(502, f"openrouter returned {error.code}: {detail}")
    except urllib.error.URLError as error:
        raise HTTPException(502, f"could not reach openrouter: {error}")


@app.post("/api/auth/signup")
def signup(body: Signup, response: JSONResponse):
    email = body.email.strip().lower()
    if "@" not in email or len(body.password) < 8:
        raise HTTPException(400, "use a real email and a password of at least 8 characters")
    if store.get_user(email):
        raise HTTPException(409, "that email is already registered")
    user = store.create_user(email, body.password, body.name.strip() or email.split("@")[0])
    store.credit(user["id"], FREE_STARTING_CREDITS, "grant", "welcome credits")
    token = store.start_session(user["id"])
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=14 * 24 * 3600)
    seeded = None
    if DEMO:
        try:
            seeded = demo.seed(user["id"])
        except Exception as error:                    # noqa: BLE001
            seeded = {"seeded": False, "error": f"{type(error).__name__}: {error}"}
    return {"user": {"email": user["email"], "name": user["name"]},
            "credits": FREE_STARTING_CREDITS, "demo": seeded}


@app.post("/api/auth/login")
def login(body: Login, response: JSONResponse):
    user = store.get_user(body.email.strip().lower())
    if not user or not store.verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "that email and password do not match")
    token = store.start_session(user["id"])
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=14 * 24 * 3600)
    return {"user": {"email": user["email"], "name": user["name"]}, "wallet": store.wallet(user["id"])}


@app.post("/api/auth/logout")
def logout(response: JSONResponse):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return {"user": {"email": user["email"], "name": user["name"]}, "wallet": store.wallet(user["id"])}


# ------------------------------------------------------------------- sources
class GitHubSource(BaseModel):
    repo_url: str
    token: str = ""


class KeyRequest(BaseModel):
    name: str = "default"


@app.post("/api/demo/seed")
def seed_demo(user: dict = Depends(current_user)):
    """Fill this account with a connected repo and guild."""
    try:
        return demo.seed(user["id"])
    except Exception as error:                        # noqa: BLE001
        raise HTTPException(500, f"could not seed demo data: {type(error).__name__}: {error}")


@app.delete("/api/demo")
def clear_demo(user: dict = Depends(current_user)):
    """Remove the demo data again."""
    return demo.clear(user["id"])


@app.get("/api/sources")
def list_sources(user: dict = Depends(current_user)):
    rows = store.q(
        "SELECT * FROM sources WHERE user_id = ? ORDER BY created_at DESC", (user["id"],))
    for row in rows:
        row["config"] = store.jload(row.get("config"), {})
        pair = store.q1("SELECT id FROM pairs WHERE source_id = ?", (row["id"],))
        row["pair_id"] = pair["id"] if pair else None
        row["records"] = store.q1(
            "SELECT count(*) AS n FROM records WHERE pair_id = ?", (row["pair_id"],))["n"]
    return {"sources": rows}


@app.post("/api/sources/github")
def add_github(body: GitHubSource, user: dict = Depends(current_user)):
    owner, repo = core.parse_repo(body.repo_url)
    source_id = store.new_id("src")
    pair_id = store.new_id("pair")
    store.insert("sources", {
        "id": source_id, "user_id": user["id"], "kind": "github",
        "name": f"{owner}/{repo}", "config": store.jdump({"owner": owner, "repo": repo}),
        "status": "pending", "created_at": store.now(),
    })
    store.insert("pairs", {
        "id": pair_id, "user_id": user["id"], "source_id": source_id,
        "repo_url": body.repo_url, "label": f"{owner}/{repo}", "created_at": store.now(),
    })
    result = core.ingest_github(pair_id, body.repo_url, body.token)
    store.execute("UPDATE sources SET status = ?, last_synced_at = ?, detail = ? WHERE id = ?",
                  ("ready", store.now(), store.jdump(result), source_id))
    return {"source_id": source_id, "pair_id": pair_id, "ingested": result}


@app.post("/api/sources/discord")
async def add_discord(request: Request, user: dict = Depends(current_user)):
    """Accept a Discord export as a zip, a single channel json, or a directory.

    The export the bot in bot/ writes is the same shape DiscordChatExporter
    produces, apart from the field name for a message id, so one endpoint
    takes all of them rather than asking the user which they have.
    """
    form = await request.form()
    upload = form.get("file")
    guild_name = str(form.get("guild_name") or "").strip()
    label = str(form.get("label") or "").strip()

    if upload is None:
        raise HTTPException(400, "attach a file")
    if not hasattr(upload, "read"):
        raise HTTPException(400, "that field is not a file")

    blob = await upload.read()
    if len(blob) > 400 * 1024 * 1024:
        raise HTTPException(413, "that export is over 400 MB")

    # report what is inside before writing anything, so a wrong file fails
    # with a useful message rather than a partial import
    try:
        preview = ingest.scan_archive(blob)
    except Exception as error:
        raise HTTPException(400, f"could not read that export: {type(error).__name__}: {error}")
    if not preview["channels"]:
        raise HTTPException(400, "no channel exports found in that file. "
                                 "Expected a zip of json files, or one channel json.")
    if not preview["messages"]:
        raise HTTPException(400, f"found {preview['channels']} channel files but no messages")

    source_id = store.new_id("src")
    pair_id = store.new_id("pair")
    name = label or upload.filename or "discord export"
    store.insert("sources", {
        "id": source_id, "user_id": user["id"], "kind": "discord", "name": name,
        "config": store.jdump({"guild_name": guild_name, "filename": upload.filename,
                               "channels": preview["channels"], "files": preview["filenames"][:50]}),
        "status": "pending", "created_at": store.now(),
    })
    store.insert("pairs", {
        "id": pair_id, "user_id": user["id"], "source_id": source_id,
        "guild_id": guild_name, "label": name, "created_at": store.now(),
    })

    try:
        stats = ingest.ingest_discord(pair_id, blob, guild_name=guild_name)
    except Exception as error:
        store.execute("UPDATE sources SET status = ?, detail = ? WHERE id = ?",
                      ("failed", store.jdump({"error": str(error)}), source_id))
        raise HTTPException(400, f"import failed: {type(error).__name__}: {error}")

    store.execute("UPDATE sources SET status = ?, last_synced_at = ?, detail = ? WHERE id = ?",
                  ("ready", store.now(), store.jdump(stats), source_id))
    return {"source_id": source_id, "pair_id": pair_id, "ingested": stats}


@app.get("/api/sources/{source_id}")
def source_detail(source_id: str, user: dict = Depends(current_user)):
    row = store.q1("SELECT * FROM sources WHERE id = ? AND user_id = ?", (source_id, user["id"]))
    if not row:
        raise HTTPException(404, "no such source")
    row["config"] = store.jload(row.get("config"), {})
    return row


# ---------------------------------------------------------------------- runs
@app.post("/api/runs")
def create_run(body: dict, user: dict = Depends(current_user)):
    pair_id = body.get("pair_id", "")
    if not pair_id:
        raise HTTPException(400, "pair_id is required")
    owned = store.q1("SELECT id FROM pairs WHERE id = ? AND user_id = ?", (pair_id, user["id"]))
    if not owned:
        raise HTTPException(404, "no such source for this account")
    return {"run_id": core.start_pipeline_async(user["id"], pair_id)}


@app.get("/api/runs")
def list_runs(user: dict = Depends(current_user)):
    runs = store.q("SELECT * FROM runs WHERE user_id = ? ORDER BY started_at DESC LIMIT 50",
                   (user["id"],))
    for run in runs:
        run["stats"] = store.jload(run.get("stats"), {})
    return {"runs": runs}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str, user: dict = Depends(current_user)):
    run = store.q1("SELECT * FROM runs WHERE id = ? AND user_id = ?", (run_id, user["id"]))
    if not run:
        raise HTTPException(404, "no such run")
    run["stats"] = store.jload(run.get("stats"), {})
    run["events"] = store.run_events(run_id)
    return run


# -------------------------------------------------------------------- models
@app.get("/api/models")
def list_models(user: dict = Depends(current_user)):
    models = store.q("SELECT * FROM models WHERE user_id = ? ORDER BY version DESC", (user["id"],))
    for model in models:
        model["metrics"] = store.jload(model.get("metrics"), {})
    return {"models": models}


@app.post("/api/keys")
def create_key(body: KeyRequest, user: dict = Depends(current_user)):
    raw, row = store.issue_api_key(user["id"], body.name)
    return {"key": raw, "id": row["id"], "prefix": row.get("prefix", "")}


@app.get("/api/keys")
def list_keys(user: dict = Depends(current_user)):
    return {"keys": store.q(
        "SELECT id, name, prefix, created_at, revoked FROM api_keys WHERE user_id = ? "
        "ORDER BY created_at DESC", (user["id"],))}


# -------------------------------------------------------------------- wallet
@app.get("/api/wallet")
def get_wallet(user: dict = Depends(current_user)):
    return {"wallet": store.wallet(user["id"]),
            "ledger": store.q(
                "SELECT amount, kind, note, at FROM ledger WHERE user_id = ? "
                "ORDER BY at DESC LIMIT 50", (user["id"],))}


@app.get("/api/inference")
def inference_log(user: dict = Depends(current_user)):
    rows = store.q(
        "SELECT at, model_id, prompt_tokens, completion_tokens, credits, status, latency_ms "
        "FROM inference_log WHERE user_id = ? ORDER BY at DESC LIMIT 100", (user["id"],))
    totals = store.q1(
        "SELECT COALESCE(SUM(credits),0) AS credits, COUNT(*) AS calls, "
        "COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, "
        "COALESCE(SUM(completion_tokens),0) AS completion_tokens "
        "FROM inference_log WHERE user_id = ?", (user["id"],))
    return {"log": rows, "totals": totals}


# ----------------------------------------------------------------- inference
class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str = "weave"
    messages: list[ChatMessage] = Field(default_factory=list)
    max_tokens: int | None = None
    temperature: float | None = None
    stream: bool = False
    # passed through untouched, because the harness sends the same tool schemas
    # the trajectories were generated with and rewriting them here would make
    # the served model disagree with the one it was trained against
    tools: list[dict] | None = None
    reasoning: dict | None = None


@app.post("/v1/chat/completions")
def chat_completions(body: ChatRequest, request: Request,
                     user: dict | None = Depends(api_or_proxy)):
    """OpenAI shaped, so an OpenAI client can point at this unchanged.

    Two ways in. A per account API key serves that account's own fine tuned
    model. The shared secret, when one is configured, forwards straight to
    OpenRouter instead, which is the shape you want for a demo where a single
    key gets handed around rather than one per person.
    """
    if not body.messages:
        raise HTTPException(400, "messages is empty")

    if user is None:
        # the shared secret. Logged against the shared account so the request
        # shows up in the usage panel rather than vanishing, then forwarded
        # upstream and handed back exactly as it came, so the caller keeps the
        # OpenAI shape they already expect.
        started = time.time()
        owner = proxy_account()

        if body.stream:
            # The harness streams, so the reply has to arrive as it is produced.
            # Buffering it to log first would defeat the point, so the upstream
            # body is piped straight through and logged once it ends, with the
            # usage OpenRouter sends on the final frame.
            upstream = stream_openrouter(openrouter_body(
                [m.model_dump() for m in body.messages], body.model,
                body.max_tokens or 4096, body.temperature,
                tools=body.tools, stream=True, reasoning=body.reasoning))

            def pump(source):
                # The frames are teed as they go past. A urllib response cannot
                # be rewound, so reading it again for the usage frame returns
                # nothing, which is why the first version logged zero tokens on
                # every streamed call.
                tail: list[bytes] = []
                try:
                    for raw in source:
                        tail.append(raw)
                        if len(tail) > 400:
                            tail.pop(0)
                        yield raw
                finally:
                    log_proxy(owner, body.model, started, *usage_from(tail))
                    source.close()

            return StreamingResponse(
                pump(upstream), media_type="text/event-stream",
                headers={"Cache-Control": "no-cache",
                         "X-Accel-Buffering": "no"})

        
        status, detail, usage = "ok", {}, {}
        try:
            reply = call_openrouter([m.model_dump() for m in body.messages],
                                    body.model, body.max_tokens or 4096,
                                    body.temperature)
            usage = reply.get("usage") or {}
        except HTTPException as failure:
            status = "error"
            detail = {"error": str(failure.detail)[:400]}
            raise
        finally:
            credits = float(usage.get("total_tokens") or 0) / 100000.0
            store.insert("inference_log", {
                "id": store.new_id("inf"), "user_id": owner["id"], "model_id": None,
                "key_id": None, "at": store.now(),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "credits": credits, "status": status,
                "latency_ms": int((time.time() - started) * 1000),
                "detail": store.jdump({"via": "proxy", "model": body.model,
                                       "upstream": "openrouter", **detail}),
            })
        return reply
    started = time.time()
    model_id = None
    status = "ok"
    prompt_tokens = completion_tokens = 0
    credits = 0.0
    try:
        text, prompt_tokens, completion_tokens = core.generate(
            user["id"], [m.model_dump() for m in body.messages], body.model)
        credits = float(completion_tokens) / 1000.0
        store.credit(user["id"], -credits, "spend", f"{body.model} completion")
    except Exception as failure:
        status = "error"
        text = f"generation failed: {type(failure).__name__}: {failure}"
    finally:
        store.insert("inference_log", {
            "id": store.new_id("inf"), "user_id": user["id"], "model_id": model_id,
            "key_id": None, "at": store.now(), "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens, "credits": credits, "status": status,
            "latency_ms": int((time.time() - started) * 1000),
            "detail": store.jdump({"model": body.model}),
        })
    if status == "error":
        raise HTTPException(502, text)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
    }


@app.get("/v1/models")
def list_models_openai(user: dict = Depends(api_key_user)):
    models = store.q("SELECT name, version FROM models WHERE user_id = ? ORDER BY version DESC",
                     (user["id"],))
    return {"object": "list", "data": [
        {"id": m["name"], "object": "model", "owned_by": "weave",
         "created": int(store.now())} for m in models]}


# ---------------------------------------------------------------------- site
#
# The three pages are plain files in static/. They are served rather than built
# here, so the markup can be edited and reloaded without touching Python.


def _page(name: str) -> FileResponse:
    return FileResponse(STATIC / name, media_type="text/html")


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


@app.get("/")
def marketing():
    return _page("index.html")


@app.get("/auth")
def auth_page(request: Request):
    """Signed in visitors have no business on the sign in page."""
    if request.cookies.get(SESSION_COOKIE):
        return RedirectResponse("/app", status_code=302)
    return _page("auth.html")


@app.get("/app")
def dashboard(request: Request):
    """The dashboard is a static page that fetches its own data.

    Unauthenticated visitors get the sign in page instead of a shell that would
    only render a "not signed in" message.
    """
    if not request.cookies.get(SESSION_COOKIE):
        return RedirectResponse("/auth", status_code=302)
    return _page("app.html")


@app.get("/docs")
def docs_page():
    return _page("docs.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")

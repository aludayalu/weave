"""Smoke test for the web app. Run with:  python3 test_app.py

Uses a throwaway SQLite file, so it never touches real data. Covers the auth
boundary, API key issuance, the OpenAI shaped endpoint and the empty states the
dashboard renders.
"""

from __future__ import annotations

import os
import sys
import tempfile

os.environ["WEAVE_DATA"] = tempfile.mkdtemp(prefix="weave-test-")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient  # noqa: E402

import app as application  # noqa: E402

OK = 0
FAIL = 0


def check(label: str, condition: bool, extra: object = "") -> None:
    global OK, FAIL
    if condition:
        OK += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label} {extra}")


def main() -> int:
    client = TestClient(application.app)

    print("site")
    response = client.get("/healthz")
    check("healthz", response.status_code == 200 and response.text == "ok", response.text)
    response = client.get("/")
    check("marketing page", response.status_code == 200 and "turned into a model" in response.text)
    check("marketing has a signup path", 'href="/app"' in response.text)
    response = client.get("/auth")
    check("auth page", response.status_code == 200 and "Create account" in response.text)
    response = client.get("/auth")
    check("auth page has a real form", 'id="auth-form"' in response.text)
    response = client.get("/docs")
    check("docs page", response.status_code == 200 and "chat/completions" in response.text)
    response = client.get("/static/app.css")
    check("stylesheet served", response.status_code == 200 and "--accent" in response.text)
    response = client.get("/app", follow_redirects=False)
    check("dashboard redirects when signed out", response.status_code in (302, 307),
          response.status_code)
    check("dashboard redirects to /auth", "/auth" in response.headers.get("location", ""),
          response.headers.get("location"))

    print("\nauth")
    response = client.post("/api/auth/signup", json={"email": "a@b.co", "password": "short"})
    check("weak password rejected", response.status_code == 400, response.status_code)
    response = client.post("/api/auth/signup", json={"email": "a@b.co", "password": "longenough1"})
    check("signup", response.status_code == 200, response.text[:200])
    check("session cookie set", "weave_session" in client.cookies)
    response = client.get("/api/me")
    check("me with cookie", response.status_code == 200, response.text[:150])
    check("welcome credits", response.json()["wallet"]["credits"] == 25.0, response.json().get("wallet"))
    response = client.get("/app", follow_redirects=False)
    check("dashboard serves when signed in", response.status_code == 200, response.status_code)
    response = client.post("/api/auth/signup", json={"email": "a@b.co", "password": "longenough1"})
    check("duplicate email rejected", response.status_code == 409, response.status_code)
    response = client.post("/api/auth/login", json={"email": "a@b.co", "password": "wrong"})
    check("bad password rejected", response.status_code == 401, response.status_code)
    response = client.post("/api/auth/login", json={"email": "a@b.co", "password": "longenough1"})
    check("login", response.status_code == 200, response.status_code)

    print("\napi keys")
    response = client.post("/api/keys", json={"name": "test"})
    check("key issued", response.status_code == 200 and response.json()["key"].startswith("wk-"),
          response.text[:150])
    raw = response.json()["key"]
    response = client.post("/v1/chat/completions",
                           json={"messages": [{"role": "user", "content": "hi"}]},
                           headers={"Authorization": f"Bearer {raw}"})
    check("completions with key", response.status_code in (200, 502), response.text[:200])
    response = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    check("completions without key is 401", response.status_code == 401, response.status_code)
    response = client.post("/v1/chat/completions", json={"messages": []},
                           headers={"Authorization": f"Bearer {raw}"})
    check("empty messages is 400", response.status_code == 400, response.status_code)
    response = client.get("/v1/models", headers={"Authorization": f"Bearer {raw}"})
    check("v1/models", response.status_code == 200 and response.json()["object"] == "list",
          response.text[:150])

    print("\nauth isolation")
    stranger = TestClient(application.app)
    check("stranger is 401 on me", stranger.get("/api/me").status_code == 401)
    check("stranger is 401 on runs", stranger.get("/api/runs").status_code == 401)

    print("\nempty states")
    for path in ("/api/sources", "/api/runs", "/api/models", "/api/wallet",
                 "/api/inference", "/api/keys"):
        response = client.get(path)
        check(f"GET {path}", response.status_code == 200, f"{response.status_code} {response.text[:80]}")
    response = client.post("/api/runs", json={"pair_id": "nope"})
    check("run on unknown pair is 404", response.status_code == 404, response.status_code)

    print(f"\n{OK} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

import { test } from "node:test";
import assert from "node:assert/strict";
import { authorizeSSE, isUsableQueryToken } from "./sseAuth.ts";

const token = "aB3dEfGh1jKlMn0pQrS";

test("legacy server accepts a short-lived query token", () => {
  assert.equal(authorizeSSE("legacy", { queryToken: token }), true);
});

test("app-router SSE still rejects query tokens", () => {
  assert.equal(authorizeSSE("app", { queryToken: token }), false);
});

test("session cookie authorizes both servers", () => {
  assert.equal(authorizeSSE("legacy", { sessionCookie: "s" }), true);
  assert.equal(authorizeSSE("app", { sessionCookie: "s" }), true);
});

test("missing credentials are rejected everywhere", () => {
  assert.equal(authorizeSSE("legacy", {}), false);
  assert.equal(authorizeSSE("app", {}), false);
});

test("guest or malformed tokens are refused", () => {
  assert.equal(isUsableQueryToken("guest_token_1234567890"), false);
  assert.equal(isUsableQueryToken("short"), false);
  assert.equal(isUsableQueryToken(undefined), false);
});

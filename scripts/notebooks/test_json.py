"""Adversarial tests for the model reply parser.

Run with:  python3 test_json.py

Every case here is a way these models have actually answered: fences, prose
around the answer, a worked example printed before the real one, fences with
attributes, comments, trailing commas, smart quotes, truncation, and refusal.
The point is that the parser must pick the right candidate rather than the
first one it finds.
"""

from __future__ import annotations

import json
import pathlib
import sys

SOURCE = pathlib.Path(__file__).with_name("03_silver_to_gold.py")
sys.path.insert(0, str(SOURCE.parent))

NAMES = [
    "ZERO_WIDTH", "SMART", "FENCE", "_clean", "_strip_comments", "_drop_trailing_commas",
    "_scan_value", "_close_open", "_loads", "_try_all", "_shape_score", "extract_json",
]

text = SOURCE.read_text()
start = text.index("ZERO_WIDTH = dict.fromkeys")
end = text.index("def call_model(")
namespace = {"json": json, "re": __import__("re")}
exec(compile(text[start:end], "<parser>", "exec"), namespace)
extract_json = namespace["extract_json"]

OK = 0
FAIL = 0
FAILURES: list[str] = []


def check(label: str, condition: bool, extra: object = "") -> None:
    global OK, FAIL
    if condition:
        OK += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        FAILURES.append(label)
        print(f"  FAIL  {label}  {extra}")


WANT = {"windows": [{"task_id": "mer-101", "start_ts": "2026-07-08T15:00:00Z"}]}


def windows(**overrides):
    payload = {"windows": [{"task_id": "mer-101", "start_ts": "2026-07-08T15:00:00Z"}]}
    payload.update(overrides)
    return payload


print("clean replies")
check("plain json", extract_json(json.dumps(WANT)) == WANT)
check("json with markdown fence",
      extract_json("```json\n" + json.dumps(WANT) + "\n```") == WANT)
check("bare fence, no language",
      extract_json("```\n" + json.dumps(WANT) + "\n```") == WANT)
check("prose before and after",
      extract_json("Sure! Here you go:\n" + json.dumps(WANT) + "\nHope that helps."))
check("python fence tag",
      extract_json("```Python\n" + json.dumps(WANT) + "\n```") == WANT)

print("\nthe hard cases: more than one candidate")
check("example before the real answer is skipped",
      extract_json(
          "Example:\n```json\n{\"windows\": [{\"task_id\": \"EXAMPLE-1\"}]}\n```\n"
          "Actual:\n```json\n" + json.dumps(WANT) + "\n```"
      ) == WANT, "picked the example")
check("prose example then the answer, no fences",
      extract_json('Example: {"windows": [{"task_id": "EXAMPLE-1"}]}\n'
                   'Real: ' + json.dumps(WANT)) == WANT, "picked the example")
check("fence carries attributes",
      extract_json('```json title="result"\n' + json.dumps(WANT) + "\n```") == WANT)
check("two fences where only the second is valid json",
      extract_json("```json\nnot json at all\n```\n```json\n" + json.dumps(WANT) + "\n```")
      == WANT)

print("\nsloppy json")
check("trailing commas",
      extract_json('{"windows": [{"task_id": "mer-101", "start_ts": "2026-07-08T15:00:00Z"},],}')
      == WANT)
check("trailing comma in an array",
      extract_json('{"windows": [{"task_id": "mer-101"},]}'))
check("line comments inside",
      extract_json('{\n  // the windows\n  "windows": [{"task_id": "mer-101"}]\n}')["windows"]
      == [{"task_id": "mer-101"}])
check("block comment inside",
      extract_json('{/* note */ "windows": [{"task_id": "mer-101"}]}')["windows"]
      == [{"task_id": "mer-101"}])
check("comment markers inside a string are not stripped",
      extract_json('{"windows": [{"task_id": "mer-101", "note": "// not a comment"}]}')
      ["windows"][0]["note"] == "// not a comment")
check("braces inside string values",
      extract_json('{"windows": [{"task_id": "mer-101", "summary": "fix {the} parser"}]}')
      ["windows"][0]["summary"] == "fix {the} parser")
check("escaped quote inside a string",
      extract_json(r'{"windows": [{"task_id": "mer-101", "summary": "say \"hi\" now"}]}')
      ["windows"][0]["summary"] == 'say "hi" now')
check("backslash at the end of a string",
      extract_json(r'{"windows": [{"task_id": "mer-101", "path": "C:\\"}]}')
      ["windows"][0]["path"] == "C:\\")

print("\nencoding damage")
check("smart quotes used as delimiters",
      extract_json('\u201cwindows\u201d: [{\u201ctask_id\u201d: \u201cmer-101\u201d}]')
      == {"windows": [{"task_id": "mer-101"}]},
      extract_json('\u201cwindows\u201d: [{\u201ctask_id\u201d: \u201cmer-101\u201d}]'))
check("zero width characters",
      extract_json('{"win\u200bdows": [{"task_id": "mer-101"}]}') is not None)
check("byte order mark", extract_json("\ufeff" + json.dumps(WANT)) == WANT)
check("windows line endings",
      extract_json(json.dumps(WANT).replace('", "', '",\r\n "')) == WANT)

print("\ntruncated output")
TRUNC = {"windows": [{"task_id": "mer-101", "start_ts": "2026-07-08T15:00:00Z"}]}
check("missing closing brace",
      extract_json(json.dumps(TRUNC)[:-1]) == TRUNC, extract_json(json.dumps(TRUNC)[:-1]))
check("missing closing brace and bracket",
      extract_json(json.dumps(TRUNC)[:-2]) == TRUNC, extract_json(json.dumps(TRUNC)[:-2]))
check("unterminated fence and object",
      extract_json("```json\n" + json.dumps(WANT)[:-1]) == WANT)
check("cut off mid value keeps what parsed",
      "windows" in extract_json('{"windows": [{"task_id": "mer-1'))

print("\nshape tolerance")
check("case insensitive key",
      extract_json('{"Windows": [{"task_id": "mer-101"}]}') is not None)
check("bare list of windows is wrapped",
      extract_json(json.dumps(WANT["windows"])) == WANT,
      extract_json(json.dumps(WANT["windows"])))
check("empty windows list",
      extract_json('{"windows": []}') == {"windows": []})
check("extra keys alongside windows are kept",
      "notes" in extract_json('{"windows": [], "notes": "hello"}'))

print("\nunusable input, and it must say why")
for label, payload in [
    ("empty string", ""),
    ("only prose", "I am not able to help with that request."),
    ("only a fence with no body", "```json\n```"),
    ("html instead of json", "<html><body>502 Bad Gateway</body></html>"),
    ("a bare number", "42"),
]:
    try:
        value = extract_json(payload)
        check(f"rejects {label}", False, f"returned {value!r}")
    except ValueError as error:
        check(f"rejects {label}", True)
        if not str(error):
            check(f"{label} explains itself", False, "empty message")

print("\nidempotence and speed")
big = windows() | {"windows": [{"task_id": f"MER-{i}"} for i in range(400)]}
import time
began = time.time()
for _ in range(20):
    extract_json("```json\n" + json.dumps(big) + "\n```")
elapsed = time.time() - began
check(f"400 windows x20 parses in {elapsed:.2f}s", elapsed < 5.0, f"{elapsed:.2f}s")

print("\nend to end through the call path")
import types, urllib.request, urllib.error

def grab(name):
    i = text.index(f"def {name}(")
    return text[i:text.index("\ndef ", i + 1)]


def gateway_reply(answer, reasoning=0):
    """Shaped exactly like a real gateway reply: output blocks, no choices."""
    blocks = []
    if reasoning:
        blocks.append({"type": "message", "role": "assistant",
                       "content": [{"type": "reasoning_text", "text": "r" * reasoning}]})
    blocks.append({"type": "message", "role": "assistant", "id": "msg_1",
                   "status": "completed",
                   "content": [{"type": "output_text", "text": answer}]})
    return {"model": "qwen35-122b-a10b", "object": "response", "status": "completed",
            "id": "resp_1", "incomplete_details": None, "error": None,
            "usage": {"input_tokens": 30718, "output_tokens": 512, "total_tokens": 31230},
            "output": blocks}


class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


SENT = {}


class FakeUrlopen:
    def __init__(self, replies):
        self.replies = replies

    def __call__(self, request, timeout=None):
        SENT["body"] = json.loads(request.data.decode())
        SENT["url"] = request.full_url
        SENT["headers"] = dict(request.header_items())
        return FakeResponse(self.replies.pop(0))


REPLIES = []
env = {
    "json": json, "re": __import__("re"), "os": __import__("os"),
    "urllib": urllib.request, "urllib.error": urllib.error, "time": __import__("time"),
    "GATEWAY_HOST": "https://example.databricks.com",
    "GATEWAY_PATH": "/ai-gateway/mlflow/v1/responses",
    "GATEWAY_MODEL": "system.ai.qwen35-122b-a10b", "GATEWAY_TOKEN": "dapi-test",
    "REASONING_EFFORT": {"off": "none", "head": "low", "full": "medium"},
    "REASONING_VERBOSITY": "off", "REASONING_CHARS": {"off": 0, "head": 2000, "full": 100000},
    "widget_get": lambda n, d=None: d, "print": lambda *a, **k: None,
    "show_reasoning": lambda *a, **k: None,
}
exec(compile(text[start:text.index("def call_model(")], "<parser>", "exec"), env)
for fn in ("ask_endpoint", "read_reply", "call_serving_endpoint"):
    exec(compile(grab(fn), f"<{fn}>", "exec"), env)
env["urllib"] = types.SimpleNamespace(
    request=types.SimpleNamespace(urlopen=FakeUrlopen(REPLIES),
                                  Request=urllib.request.Request))
call = env["call_serving_endpoint"]

for label, content in [
    ("fenced answer", "Here you go:\n```json\n" + json.dumps(WANT) + "\n```"),
    ("prose wrapped", "Sure! " + json.dumps(WANT) + " Let me know."),
    ("example then answer",
     "Example:\n```json\n{\"windows\": [{\"task_id\": \"EX-1\"}]}\n```\n"
     "Actual:\n```json\n" + json.dumps(WANT) + "\n```"),
]:
    REPLIES[:] = [gateway_reply(content, reasoning=300)]
    try:
        got, reply, rcount = call("system.ai.qwen35-122b-a10b", "s", "u", "m", attempts=2)
        check(f"gateway: {label}", got == WANT, f"got {got!r}")
        check(f"  usage mapped: {label}",
              reply["prompt_tokens"] == 30718 and reply["completion_tokens"] == 512, reply)
        check(f"  reasoning counted: {label}", rcount == 300, rcount)
    except SystemExit as e:
        check(f"gateway: {label}", False, str(e)[:110])

check("url is the gateway responses route",
      SENT["url"].endswith("/ai-gateway/mlflow/v1/responses"), SENT.get("url"))
body = SENT["body"]
check("body uses input, not messages", "input" in body and "messages" not in body, sorted(body))
check("body uses max_output_tokens", "max_output_tokens" in body, sorted(body))
check("body carries reasoning effort", body.get("reasoning") == {"effort": "none"},
      body.get("reasoning"))
check("input turns are input_text", body["input"][0]["content"][0]["type"] == "input_text",
      body["input"][0])
check("token sent as bearer", any("dapi-test" in str(v) for v in SENT["headers"].values()),
      SENT["headers"])

# the retired bare name must never reach the gateway
for stale in ("qwen35", "Qwen35", "qwen3.5", "qwen35-122b-a10b", "", None):
    REPLIES[:] = [gateway_reply(json.dumps(WANT))]
    env["call_serving_endpoint"](stale, "s", "u", "m", attempts=1)
    check(f"stale name {stale!r} is not sent",
          SENT["body"]["model"] == "system.ai.qwen35-122b-a10b", SENT["body"]["model"])

# a real custom endpoint name must be passed through untouched
REPLIES[:] = [gateway_reply(json.dumps(WANT))]
env["call_serving_endpoint"]("my-custom-model", "s", "u", "m", attempts=1)
check("custom name passes through", SENT["body"]["model"] == "my-custom-model",
      SENT["body"]["model"])
check("default effort is none", SENT["body"]["reasoning"] == {"effort": "none"},
      SENT["body"]["reasoning"])

try:
    env["read_reply"]({"status": "incomplete",
                       "incomplete_details": {"reason": "max_output_tokens"}, "output": []})
    check("incomplete reply raises", False, "returned silently")
except ValueError as e:
    check("incomplete reply raises", "cut off" in str(e), str(e)[:70])

print(f"\n{OK} passed, {FAIL} failed")
if FAILURES:
    for name in FAILURES:
        print(f"  - {name}")
raise SystemExit(1 if FAIL else 0)

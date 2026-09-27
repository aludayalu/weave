# Synthetic RL run generation — prompt, harness, and output contract

For each window in `gold.task_windows`, run this generation in parallel (Databricks job
with a task per window, or `map` over the window table).

## Model input

```
REPOSITORY (read-only, at the window START commit)
  full source tree; the model explores with shell commands only

TASK BRIEF
  title, summary, problem statement, and the tribal constraints the team relied on
  (extracted from the window's chat, ticket, and attached documents)

DOCUMENTS (extracted text, may be truncated)
  from silver.documents for the window — client reports, log captures, screenshots

CONSTRAINTS
  the harness has one tool; commands time out; you see partial output; think between steps
```

## Harness contract

```json
{
  "name": "shell",
  "description": "Run one shell command in the repository and return its output.",
  "parameters": {
    "type": "object",
    "properties": {
      "command": { "type": "string", "description": "shell command to run" },
      "timeout_s": { "type": "integer", "description": "hard timeout in seconds", "default": 30 }
    },
    "required": ["command"]
  }
}
```

Tool result schema returned to the model:

```json
{
  "completed": true,
  "timed_out": false,
  "exit_code": 0,
  "stdout": "...",
  "stderr": "...",
  "duration_ms": 421
}
```

Rules the harness enforces:

1. **Hard timeout, default 30s** (40s ceiling). On timeout the control returns to the
   model immediately with `completed: false, timed_out: true`, plus the output captured
   so far. The model can see a partially finished install or a still-running server and
   decide to wait.
2. **Waiting is a normal move.** `sleep 20 && tail -f build.log` works, and the harness
   will time it out at the limit while still streaming the tail output.
3. **The model must always know which of the two happened** — completion vs timeout.
   A silent return is a bug, not a shortcut.
4. **No other tools.** No web, no direct file API. The repo is the world.
5. The model may run read-only commands (`rg`, `cat`, `git log`, `go test`) and write
   commands; the harness records the final diff as the candidate patch.

## Output format

One JSON array per generated run, byte-compatible with Databricks Model Gateway
messages for a fine-tuned model hosted the same way:

```json
[
  {"role": "system", "content": "You are a senior engineer working in a large Go + Next.js ERP codebase..."},
  {"role": "user", "content": "<task brief + document excerpts + repo revision>"},
  {"role": "assistant",
   "reasoning_content": "The client report mentions an upstream timeout misclassified as a decline. The payment codes are numeric and contractual, so I need to find where the 3000 range is defined before adding anything. grep first.",
   "tool_calls": [{"id": "call_1", "type": "function",
                   "function": {"name": "shell", "arguments": "{\"command\":\"grep -rn 'RangePayment' pkg/errs\",\"timeout_s\":10}"}}]},
  {"role": "tool", "tool_call_id": "call_1", "content": "{\"completed\":true,\"timed_out\":false,\"exit_code\":0,\"stdout\":\"pkg/errs/code.go:...\",\"stderr\":\"\",\"duration_ms\":96}"},
  {"role": "assistant",
   "reasoning_content": "Codes are declared per module in separate iota blocks and the client hardcodes the values, so the new code must be appended at the end of the payment block and the generated tables regenerated. The generated table is a build artifact, so I edit the YAML and run make gen.",
   "tool_calls": [{"id": "call_2", "type": "function",
                   "function": {"name": "shell", "arguments": "{\"command\":\"sed -n '30,60p' api/errors.yaml\",\"timeout_s\":10}"}}]},
  {"role": "tool", "tool_call_id": "call_2", "content": "{\"completed\":true,\"timed_out\":false,\"exit_code\":0,\"stdout\":\"modules:\\n  - name: payment ...\",\"stderr\":\"\",\"duration_ms\":31}"},
  {"role": "assistant",
   "reasoning_content": "Payment block ends with rate_limited. Appending gateway_timeout gives 3003, which matches the partner contract. Now regenerate the Go and TS tables.",
   "tool_calls": [{"id": "call_3", "type": "function",
                   "function": {"name": "shell", "arguments": "{\"command\":\"go run ./tools/gendocs && go test ./pkg/errs/\",\"timeout_s\":40}"}}]},
  {"role": "tool", "tool_call_id": "call_3", "content": "{\"completed\":true,\"timed_out\":false,\"exit_code\":0,\"stdout\":\"ok github.com/meridian-erp/meridian/pkg/errs 0.106s\",\"stderr\":\"\",\"duration_ms\":3820}"},
  {"role": "assistant",
   "reasoning_content": "Generated tables are in sync and the package tests pass. The existing partner codes are unchanged, which is the actual contract I needed to protect.",
   "content": "Appended payment.gateway_timeout = 3003 after rate_limited in api/errors.yaml, added the named constant, and regenerated the message table and TS range map. Existing codes 3000-3002 are unchanged."}
]
```

Requirements on generation:

- `reasoning_content` is the thinking trace. It must reference the actual evidence
  (the report, the log line, the ticket decision), because that is precisely the
  behavior the fine-tune is teaching.
- Tool calls must be a plausible exploration of this repo: locate the convention,
  read the generated artifact's source, then edit, then regenerate, then test.
- A run that edits the generated file instead of its source, or that "fixes" the wrong
  layer, is a valid *negative* sample and should be kept for the dataset.
- The final assistant message summarizes the change without over-claiming.

# Held-out scoring suites (eval split e01–e07)

These suites are the scoring instruments for the held-out tasks. They are
category-mirrored to the training suites (same assertion styles, different
fixtures and file targets) so generalization is measured, not recall.

| file | task | categories |
|---|---|---|
| `eval201_test.go` | e01 payroll column order | regression (reserved positions), convention (append-only + schema bump), cross-language |
| `eval202_test.go` | e02 bulk soft-delete | spec (audit note required), regression (single-row path) |
| `eval203_sse.test.ts` | e03 SSE auth | spec (legacy query token), regression (cookie on both), security (app-router rejects token) |
| `eval204_paylabels.test.ts` | e04 pay label alignment | cross-language (Go registry vs TS mirror), regression (labels, duplicates) |
| `eval205_test.go` | e05 overdue timezone | spec (tenant-local boundary), regression (format) |
| `eval206_test.go` | e06 idempotency | spec (retry reuses key), regression (attempt scoping) |
| `eval207_test.go` | e07 audit v1 shim | spec (legacy field mapping), regression (v2 unaffected) |

Scoring is weighted partial credit, not pass/fail: each suite is worth points
split across spec / regression / convention assertions, and an untrained model
is expected to land mid-range while a model fine-tuned on the 25 training tasks
lands high. The suites exist in the repo tree only so the repository looks
realistic; at evaluation time they are removed from the candidate's tree and
injected by the scorer.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { PAY_COMPONENTS } from "./payComponents.ts";

const goOrder = ["BASE","OT","BONUS","COMM","HEALTH","RETIRE","MEAL","UNIFORM","SHIFT_DIFF"];

test("frontend component order mirrors the Go export registry", () => {
  assert.deepEqual(PAY_COMPONENTS.map((c) => c.code), goOrder);
});

test("CSV export header positions stay aligned with the mirror", () => {
  const src = readFileSync(fileURLToPath(new URL("../../internal/payroll/export.go", import.meta.url)), "utf8");
  const match = src.match(/PAY_COMPONENT_ORDER = \[\]string\{([^}]*)\}/);
  assert.ok(match, "component registry not found in export.go");
  const parsed = [...match[1]!.matchAll(/"([A-Z_]+)"/g)].map((m) => m[1]);
  assert.deepEqual(parsed, goOrder);
});

test("every component exposes a preview label", () => {
  for (const c of PAY_COMPONENTS) assert.ok(c.label.length > 0, `${c.code} missing label`);
});

test("no duplicate component codes", () => {
  const codes = PAY_COMPONENTS.map((c) => c.code);
  assert.equal(new Set(codes).size, codes.length);
});

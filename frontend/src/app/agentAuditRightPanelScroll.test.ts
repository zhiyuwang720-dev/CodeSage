import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

const sourceRoot = resolve(import.meta.dirname, "..");

test("agent audit right panel keeps the stats pane scrollable", () => {
  const agentAuditSource = readFileSync(resolve(sourceRoot, "pages/AgentAudit/index.tsx"), "utf8");

  assert.match(
    agentAuditSource,
    /<div className="relative ml-4 flex min-h-0 w-\[32%\] flex-col overflow-hidden/
  );
  assert.match(
    agentAuditSource,
    /<div className="flex min-h-0 basis-\[34%\] flex-col border-b border-border overflow-hidden/
  );
  assert.match(
    agentAuditSource,
    /<div className="min-h-0 flex-1 overflow-y-auto p-4 custom-scrollbar bg-card">/
  );
  assert.doesNotMatch(agentAuditSource, /Bottom section - Stats \*\/\s*<div className="flex-shrink-0 p-4 bg-card">/);
});

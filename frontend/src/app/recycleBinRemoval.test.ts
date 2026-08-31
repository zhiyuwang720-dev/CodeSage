import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

const sourceRoot = resolve(import.meta.dirname, "..");

test("routes no longer expose the recycle-bin page", () => {
  const routesSource = readFileSync(resolve(sourceRoot, "app/routes.tsx"), "utf8");

  assert.doesNotMatch(routesSource, /RecycleBin/);
  assert.doesNotMatch(routesSource, /\/recycle-bin/);
  assert.equal(existsSync(resolve(sourceRoot, "pages/RecycleBin.tsx")), false);
});

test("routes no longer expose the prompt management page", () => {
  const routesSource = readFileSync(resolve(sourceRoot, "app/routes.tsx"), "utf8");
  const sidebarSource = readFileSync(resolve(sourceRoot, "components/layout/Sidebar.tsx"), "utf8");

  assert.doesNotMatch(routesSource, /PromptManager/);
  assert.doesNotMatch(routesSource, /\/prompts/);
  assert.doesNotMatch(sidebarSource, /\/prompts/);
  assert.equal(existsSync(resolve(sourceRoot, "pages/PromptManager.tsx")), false);
});

test("routes no longer expose the flow debugger page", () => {
  const routesSource = readFileSync(resolve(sourceRoot, "app/routes.tsx"), "utf8");
  const sidebarSource = readFileSync(resolve(sourceRoot, "components/layout/Sidebar.tsx"), "utf8");

  assert.doesNotMatch(routesSource, /FlowDebugger/);
  assert.doesNotMatch(routesSource, /\/flow-debugger/);
  assert.doesNotMatch(sidebarSource, /\/flow-debugger/);
  assert.equal(existsSync(resolve(sourceRoot, "pages/FlowDebugger.tsx")), false);
});

test("project deletion uses the permanent delete endpoint", () => {
  const databaseApiSource = readFileSync(resolve(sourceRoot, "shared/api/database.ts"), "utf8");

  assert.match(databaseApiSource, /async deleteProject\(id: string\): Promise<void> \{\s*await apiClient\.delete\(`\/projects\/\$\{id\}\/permanent`\);\s*\}/m);
});

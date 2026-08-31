import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

const sourceRoot = resolve(import.meta.dirname, "..");

test("home route renders the cover page instead of the agent audit entry", () => {
  const routesSource = readFileSync(resolve(sourceRoot, "app/routes.tsx"), "utf8");

  assert.match(routesSource, /import HomeCover from ['"]@\/pages\/HomeCover['"]/);
  assert.match(routesSource, /path: '\/', element: <HomeCover \/>/);
  assert.doesNotMatch(routesSource, /path: '\/', element: <AgentAudit \/>/);
});

test("home cover CTA 进入 PR 审查, 不再指向已删除的一键 CVE", () => {
  const homeSource = readFileSync(resolve(sourceRoot, "pages/HomeCover.tsx"), "utf8");

  assert.match(homeSource, /\/Homepage\.png/);
  assert.match(homeSource, /to="\/projects"/);
  assert.match(homeSource, /aria-label="进入 PR 审查"/);
  assert.doesNotMatch(homeSource, /one-click-cve/);
});

test("routes no longer expose the one-click cve page", () => {
  const routesSource = readFileSync(resolve(sourceRoot, "app/routes.tsx"), "utf8");
  const sidebarSource = readFileSync(resolve(sourceRoot, "components/layout/Sidebar.tsx"), "utf8");

  assert.doesNotMatch(routesSource, /OneClickCVE/);
  assert.doesNotMatch(routesSource, /one-click-cve/);
  assert.doesNotMatch(sidebarSource, /one-click-cve/);
  assert.equal(existsSync(resolve(sourceRoot, "pages/OneClickCVE.tsx")), false);
});

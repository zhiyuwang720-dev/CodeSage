import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

const sourceRoot = resolve(import.meta.dirname, "..");

test("/ redirects to the dashboard and no longer mounts the home cover page", () => {
  const routesSource = readFileSync(resolve(sourceRoot, "app/routes.tsx"), "utf8");

  assert.match(routesSource, /import \{ Navigate \} from ['"]react-router-dom['"]/);
  assert.match(routesSource, /path: '\/', element: <Navigate to="\/dashboard" replace \/>/);
  assert.doesNotMatch(routesSource, /import HomeCover /);
  assert.doesNotMatch(routesSource, /element: <HomeCover \/>/);
});

test("home entry is hidden from the sidebar so users land on the dashboard", () => {
  const routesSource = readFileSync(resolve(sourceRoot, "app/routes.tsx"), "utf8");

  // '/' 只作为内部重定向, 不进侧栏(visible: false)
  assert.match(routesSource, /path: '\/', element: <Navigate to="\/dashboard" replace \/>, visible: false/);
  assert.doesNotMatch(routesSource, /path: '\/', element: <HomeCover \/>/);
});

test("home cover page source was removed with its route", () => {
  assert.equal(existsSync(resolve(sourceRoot, "pages/HomeCover.tsx")), false);
});

test("sidebar brand points at the dashboard instead of the removed home cover", () => {
  const sidebarSource = readFileSync(resolve(sourceRoot, "components/layout/Sidebar.tsx"), "utf8");

  assert.match(sidebarSource, /to="\/dashboard"/);
});

test("routes no longer expose the one-click cve page", () => {
  const routesSource = readFileSync(resolve(sourceRoot, "app/routes.tsx"), "utf8");
  const sidebarSource = readFileSync(resolve(sourceRoot, "components/layout/Sidebar.tsx"), "utf8");

  assert.doesNotMatch(routesSource, /OneClickCVE/);
  assert.doesNotMatch(routesSource, /one-click-cve/);
  assert.doesNotMatch(sidebarSource, /one-click-cve/);
  assert.equal(existsSync(resolve(sourceRoot, "pages/OneClickCVE.tsx")), false);
});

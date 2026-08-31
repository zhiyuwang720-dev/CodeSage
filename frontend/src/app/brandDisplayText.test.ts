import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

const sourceRoot = resolve(import.meta.dirname, "..");
const oldBrandPattern = new RegExp(["AI" + " Audit", "Audit" + "AI", "AI" + "Audit"].join("|"));

test("login screen shows CodeSage brand text and icon metadata", () => {
  const loginSource = readFileSync(resolve(sourceRoot, "pages/Login.tsx"), "utf8");

  assert.match(loginSource, />CodeSage</);
  assert.match(loginSource, /\\u767b\\u5f55 CodeSage/);
  assert.match(loginSource, /alt="CodeSage"/);
  assert.match(loginSource, /\/codesage_icon\.svg/);
  assert.doesNotMatch(loginSource, oldBrandPattern);
});

test("home shell shows CodeSage in the sidebar brand", () => {
  const sidebarSource = readFileSync(resolve(sourceRoot, "components/layout/Sidebar.tsx"), "utf8");

  assert.match(sidebarSource, />CodeSage</);
  assert.match(sidebarSource, /alt="CodeSage"/);
  assert.match(sidebarSource, /\/codesage_icon\.svg/);
  assert.doesNotMatch(sidebarSource, oldBrandPattern);
});

test("home splash screen uses the requested visible runtime labels", () => {
  const splashSource = readFileSync(resolve(sourceRoot, "pages/AgentAudit/components/SplashScreen.tsx"), "utf8");

  assert.match(splashSource, /Loading CodeSage Core/);
  assert.match(splashSource, /root@codesage:~#/);
  assert.match(splashSource, />Auto\s*<\/span>\s*<span[^>]*>CVE</);
  assert.doesNotMatch(splashSource, oldBrandPattern);
  assert.doesNotMatch(splashSource, new RegExp("root@" + "ai" + "audit:~#"));
});
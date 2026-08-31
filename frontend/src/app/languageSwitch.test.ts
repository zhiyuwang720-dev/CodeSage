import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

const sourceRoot = resolve(import.meta.dirname, "..");

test("language switch runtime is disabled for the current Chinese-only build", () => {
  const mainSource = readFileSync(resolve(sourceRoot, "app/main.tsx"), "utf8");
  const appSource = readFileSync(resolve(sourceRoot, "app/App.tsx"), "utf8");
  const sidebarSource = readFileSync(resolve(sourceRoot, "components/layout/Sidebar.tsx"), "utf8");
  const loginSource = readFileSync(resolve(sourceRoot, "pages/Login.tsx"), "utf8");
  const registerSource = readFileSync(resolve(sourceRoot, "pages/Register.tsx"), "utf8");

  assert.doesNotMatch(mainSource, /shared\/i18n/);
  assert.doesNotMatch(appSource, /useAutoTranslateDom/);
  assert.doesNotMatch(sidebarSource, /LanguageSwitcher/);
  assert.doesNotMatch(sidebarSource, /useTranslation/);
  assert.match(sidebarSource, /const routeLabel = route\.name/);
  assert.doesNotMatch(loginSource, /LanguageSwitcher/);
  assert.doesNotMatch(registerSource, /LanguageSwitcher/);
});

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

const sourceRoot = resolve(import.meta.dirname, "..");

test("skills manager removes path actions and shows host paths", () => {
  const source = readFileSync(resolve(sourceRoot, "pages/SkillsManager.tsx"), "utf8");

  assert.doesNotMatch(source, /打开文件夹/);
  assert.doesNotMatch(source, /toFileHref/);
  assert.doesNotMatch(source, /复制路径/);
  assert.doesNotMatch(source, /!skill\.is_system/);
  assert.match(source, /host_storage_path/);
  assert.match(source, /host_file_path/);
  assert.match(source, /\[项目根目录\]\/skill_library/);
});

test("remove current agent binding disables instead of deleting", () => {
  const source = readFileSync(resolve(sourceRoot, "pages/SkillsManager.tsx"), "utf8");

  assert.doesNotMatch(source, /deleteSkillBinding/);
  assert.match(source, /updateSkillBinding\(skill\.id,\s*binding\.id,\s*\{\s*enabled:\s*false\s*\}\)/);
  assert.match(source, /binding\?\.enabled\s*&&\s*<Button[\s\S]*?removeBinding\(skill\)/);
});

test("binding toggle uses optimistic local updates without full resync", () => {
  const source = readFileSync(resolve(sourceRoot, "pages/SkillsManager.tsx"), "utf8");
  const toggleStart = source.indexOf("const toggleBinding = async");
  const removeStart = source.indexOf("const removeBinding = async");
  const syncStart = source.indexOf("const syncSkillDirectory = async");
  const toggleBody = source.slice(toggleStart, removeStart);
  const removeBody = source.slice(removeStart, syncStart);

  assert.match(toggleBody, /applyBindingUpdate/);
  assert.match(removeBody, /applyBindingUpdate/);
  assert.doesNotMatch(toggleBody, /syncLocalLibraries/);
  assert.doesNotMatch(toggleBody, /resyncSkills/);
  assert.doesNotMatch(toggleBody, /loadSkillsPage/);
  assert.doesNotMatch(removeBody, /loadSkillsPage/);
});

test("skill deletion requires a confirmation dialog before deleting", () => {
  const source = readFileSync(resolve(sourceRoot, "pages/SkillsManager.tsx"), "utf8");

  assert.match(source, /AlertDialog/);
  assert.match(source, /pendingDeleteSkill/);
  assert.match(source, /requestDeleteSkill/);
  assert.match(source, /confirmDeleteSkill/);
  assert.match(source, /确认删除 Skill/);
  assert.match(source, /此操作会删除 Skill 文件夹/);
  assert.doesNotMatch(source, /onClick=\{\(\) => removeSkill\(skill\)\}/);
  assert.match(source, /onClick=\{\(\) => requestDeleteSkill\(skill\)\}/);
});

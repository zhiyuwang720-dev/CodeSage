import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

const sourceRoot = resolve(import.meta.dirname, "../..");

test("model config form exposes editable temperature control", () => {
  const source = readFileSync(resolve(sourceRoot, "components/system/SystemConfig.tsx"), "utf8");

  assert.match(
    source,
    /<Label[^>]*>Temperature<\/Label>[\s\S]*?<Input[\s\S]*?type="number"[\s\S]*?min=\{0\}[\s\S]*?max=\{2\}[\s\S]*?step="0\.1"[\s\S]*?value=\{\(isGlobalScope \? globalConfig\.llmTemperature : activeAgentConfig\?\.llmTemperature\) \?\? ''\}/,
  );
  assert.match(
    source,
    /updateActiveConfig\(\{\s*llmTemperature: event\.target\.value === '' \? null : Number\(event\.target\.value\),\s*\}\)/,
  );
  assert.match(
    source,
    /testGlobalModel\(\{[\s\S]*?temperature: globalConfig\.llmTemperature,/,
  );
  assert.match(source, /placeholder="自动（留空）"/);
  assert.match(
    source,
    /<Label[^>]*>Top P<\/Label>[\s\S]*?value=\{\(isGlobalScope \? globalConfig\.llmTopP : activeAgentConfig\?\.llmTopP\) \?\? ''\}/,
  );
  assert.match(source, /topP: globalConfig\.llmTopP,/);
});

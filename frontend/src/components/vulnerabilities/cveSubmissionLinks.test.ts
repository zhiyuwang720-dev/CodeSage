import assert from "node:assert/strict";
import test from "node:test";

import {
  buildGitHubRepositoryTarget,
  parseGitHubRepository,
} from "./cveSubmissionLinks.ts";

test("parseGitHubRepository extracts owner and repo from browser GitHub URLs", () => {
  assert.deepEqual(parseGitHubRepository("https://github.com/Tautulli/Tautulli"), {
    owner: "Tautulli",
    repo: "Tautulli",
  });
  assert.deepEqual(parseGitHubRepository("https://github.com/Tautulli/Tautulli/issues/123"), {
    owner: "Tautulli",
    repo: "Tautulli",
  });
});

test("parseGitHubRepository extracts owner and repo from SSH clone URLs", () => {
  assert.deepEqual(parseGitHubRepository("git@github.com:Tautulli/Tautulli.git"), {
    owner: "Tautulli",
    repo: "Tautulli",
  });
});

test("parseGitHubRepository rejects empty and non-GitHub project links", () => {
  assert.equal(parseGitHubRepository(""), null);
  assert.equal(parseGitHubRepository(null), null);
  assert.equal(parseGitHubRepository("https://gitlab.com/Tautulli/Tautulli"), null);
});

test("buildGitHubRepositoryTarget creates the requested repository target URL", () => {
  assert.equal(
    buildGitHubRepositoryTarget("https://github.com/Tautulli/Tautulli.git", "security"),
    "https://github.com/Tautulli/Tautulli/security",
  );
  assert.equal(
    buildGitHubRepositoryTarget("git@github.com:Tautulli/Tautulli.git", "pulls"),
    "https://github.com/Tautulli/Tautulli/pulls",
  );
});

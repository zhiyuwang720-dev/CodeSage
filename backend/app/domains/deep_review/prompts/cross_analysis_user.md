# Cross Analysis input for this run

Follow the investigation procedure in the system message. This input contains
every Reviewer's claim once, plus only diff hunks that can be matched to a
reported head-file line. A missing hunk is a navigation gap, not a verdict.

## Fixed snapshot and publishing scope

`base_commit` and `head_commit` fix the comparison. `review_paths` is the
complete set of permissible primary Finding paths; other non-secret head
files may still be read as supporting context. `max_turns` includes the final
`FinalizeReview` call.

<run_constraints_json>
{{run_constraints_json}}
</run_constraints_json>

## Outstanding cross-dimension leads

These are repaired Plan hints, already mapped to final dimension names. They
are hypotheses to check against code, not evidence and not extra Findings.
No SemanticBrief, Plan narrative, or reviewer transcript is repeated here.

<cross_hints_json>
{{cross_hints_json}}
</cross_hints_json>

## Incomplete Reviewer coverage

Only failed, degraded, or deferred dimensions are listed. A path without a
successful Reviewer has not been proved clean. Do not silently perform a new
coverage loop or turn a coverage gap into a defect.

<coverage_gaps_json>
{{coverage_gaps_json}}
</coverage_gaps_json>

## All Candidate claims

The `index` is the fixed zero-based Candidate index for this call. `title`,
`body`, and `evidence` are the Reviewer's *unverified claim*, not source-code
facts. Every index needs a keep/drop decision, even when it lacks a line or
matched excerpt. `severity` is the claim's starting severity, not your verdict.

`hunk_status=matched_hunk` means a supplied hunk covers the reported head
line, not that the defect is real or that the line itself changed. For
`no_line`, `outside_changed_hunks`, `deleted_file`, `no_text_diff`,
`binary_diff`, or `excerpt_unavailable`, follow `location_explanation` and
use the appropriate read/search tool before deciding. In particular, never
infer that a file-level claim is false because no hunk was preselected.

<candidate_claims_json>
{{candidate_claims_json}}
</candidate_claims_json>

## Accurately matched, deduplicated diff hunks

Each hunk is real patch text for its path and is shared by the listed
`candidate_indices`; this avoids sending the same patch for multiple claims.
It is a locator, not a verified failure path. If `excerpt_truncated=true`,
read the file's patch before drawing a conclusion that depends on omitted text.
Candidates absent from this block have no automatically matched hunk.

<matched_hunks_json>
{{matched_hunks_json}}
</matched_hunks_json>

## Selective repository reading and terminal action

Use `file_read` for bounded fixed-head lines, `file_read_diff` for a changed
file's patch (`start_line` pages through *diff-text* lines, not head-file lines),
`code_search` for a specific symbol, caller, guard, or contract,
and `file_find` when a path is uncertain. A deleted file has no head content:
use its diff. Read only when the answer can change a Candidate decision or
establish an independent cross-file mechanism. The last two turns belong to
decision reconciliation and `FinalizeReview`; finalize with explicit residual
uncertainty rather than exhausting the budget on secondary searches.

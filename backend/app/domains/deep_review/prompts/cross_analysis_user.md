# Cross Analysis input for this run

Follow the operating procedure in the system message. The blocks below have different authority; do not treat a hypothesis or a Reviewer's prose as verified code. The indices in the Candidate and Evidence blocks refer to the same fixed, zero-based list.

## Fixed snapshot and scope (authoritative boundary)

The base/head commits and `review_paths` determine the audit scope. Read the fixed head only. New findings must point to a review path. `max_turns` is the complete investigation and termination budget.

<run_constraints_json>
{{run_constraints_json}}
</run_constraints_json>

## Semantic interpretation (low-confidence lead)

This is the earlier agent's interpretation of intent and change. Use it to form questions, then verify with code. Its `source` and `confidence` describe provenance, not truth.

<semantic_json>
{{semantic_json}}
</semantic_json>

## Repaired Plan relations (unverified leads)

These hints use the **final** dimension names after Plan Repair. A name map explains provenance; it does not create another issue. `internalized_hints_already_assigned_to_reviewer` were handed to a Reviewer. Anatomy related paths provide navigation, not risk evidence. Unresolved Plan risks remain coverage limits.

<plan_relations_json>
{{plan_relations_json}}
</plan_relations_json>

## Reviewer execution coverage (operational record)

`succeeded` includes a valid zero-finding result. `failed`, `deferred` or `degraded` leaves uncertainty. Do not turn a coverage gap into a negative finding, and do not claim you reran that dimension.

<reviewer_coverage_json>
{{reviewer_coverage_json}}
</reviewer_coverage_json>

## All Reviewer candidates (claims to adjudicate)

Each object has a stable `index`, source dimension, changed-file location, claimed severity, title, body, Reviewer-authored evidence and suggested fix. These are assertions, not findings. Inspect every index; do not sample by severity, path or priority.

<candidate_summaries_json>
{{candidate_summaries_json}}
</candidate_summaries_json>

## Programmatically extracted evidence (locator, not verdict)

Each `package` has the matching index and may contain code, diff, callers and related snippets. `evidence_empty` means no useful excerpt was obtained; `truncated` means some excerpt was clipped. Read missing decisive context with the tools before making a confident conclusion. Do not equate the Reviewer's `evidence` field with this block.

<evidence_packages_json>
{{evidence_packages_json}}
</evidence_packages_json>

## Tool choice and terminal action

Use `file_read` for a bounded head-file passage, `file_read_diff` for another changed file's patch, `file_find` to locate an uncertain path, and `code_search` for a specific caller, guard or contract. All four are read-only and bounded. Choose a tool only when its answer can change a decision or establish a compound chain. The remaining two turns belong to decision reconciliation and `FinalizeReview`; finalize even if some residual risks remain.

Produce the SemanticBriefDraft needed by the investigation planner.

Use the PR metadata only to identify *claimed* intent. Use the bounded patches
to identify *visible* implementation. Anatomy statistics and clusters provide
orientation, not proof of a behavior. The `patch_truncated_files` list marks
files whose full change is not visible to you; do not infer absence from it.

<untrusted_pr_context>
{{context_json}}
</untrusted_pr_context>

You cannot read more files in this stage. Separate observations from questions
for later verification and return only the SemanticBriefDraft JSON object.

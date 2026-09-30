<div align="center">
  <img src="assets/codesage-icon.svg" alt="CodeSage icon" width="80" height="80">
  <h1>CodeSage</h1>
  <p><strong>Code review findings grounded in code and evidence.</strong></p>
  <p>Pull request review · Reliable task execution · Multi-perspective deep review</p>
  <p><a href="README.md">简体中文</a> · English</p>
</div>

---

[![CodeSage 15-second showreel: task delivery, lease fencing, Planner, parallel Reviewers, Cross, and AACR results](assets/media/codesage-showreel.gif)](assets/media/codesage-showreel.mp4)

<div align="center">
  <a href="assets/media/codesage-showreel.mp4">▶ Watch the full 1080p video with sound</a> ·
  <a href="assets/media/codesage-showreel/README.md">Explore the editable motion project</a>
</div>

## What is CodeSage?

CodeSage is an AI-powered code review tool with a local CLI and a companion web product. Its **CodeSage Deep** workflow goes beyond summarizing a Git diff: it investigates the intent of a change, builds a review plan, runs focused Reviewers in parallel, and verifies their findings against code evidence and cross-file behavior before returning structured, code-located comments.

Agents can read complete files, inspect other changes, search the repository, and follow the surrounding code to understand a potential issue. Plans assign investigation work rather than prescribe how many bugs to find. Final verification challenges candidate findings, considers benign explanations, and checks duplicates and compound risks across files to reduce unsupported feedback.

CodeSage aims to balance review quality, speed, and cost. Deterministic code handles filtering, evidence extraction, and result assembly; models handle interpretation and reasoning. Configure a model endpoint to get started. Alongside the final findings, stage outputs and tool-call records show how each conclusion was reached.

Deep Review currently runs independently through the CLI; integration with the web product's Workers remains in progress. The video and chart below show one CodeSage Deep GLM-5.2 evaluation on AACR-Bench.

## Quick start

Use Docker Desktop in Linux-container mode. Workers mount the Docker socket for sandbox tools. From the repository root:

```powershell
Copy-Item .env.example .env
# Set secure POSTGRES_PASSWORD and SECRET_KEY values in .env; real model reviews also need LLM_*
docker compose up -d --build --wait
```

Open the [web UI](http://127.0.0.1:3000), [API](http://127.0.0.1:8000), or [Phoenix](http://127.0.0.1:6006). Run `docker compose ps` to inspect health and `docker compose down` to stop while preserving volumes. The optional `eval` profile has separate PostgreSQL, Redis, and Workers; see [docker-compose.yml](docker-compose.yml) for service configuration.

To inspect Deep Review's deterministic preparation without a model call:

```powershell
Set-Location backend
python -m pip install -e .
$repo = (Resolve-Path ..).Path
$base = (git -C $repo rev-parse HEAD~1).Trim()
$head = (git -C $repo rev-parse HEAD).Trim()
python -m app.domains.deep_review --repo $repo --base $base --head $head --through anatomy --output ../.codesage/anatomy.json --store-dir ../.codesage/deep-review
```

For a full review, configure `LLM_*` in `backend/.env` or the process environment and replace `--through anatomy` above with `--through final --allow-model-calls`. This incurs model costs. Each run writes `result.json`, a stage-by-stage `process_report.json`, and `events.jsonl` under `<store-dir>/<run_id>/`; Harness sessions are stored in `sessions.sqlite3` beside the requested output. Run `python -m app.domains.deep_review --help` for CLI options.

## AACR-Bench: current evaluation

![CodeSage Deep GLM-5.2 AACR-Bench evaluation: precision 52.8%, recall 23.4%, F1 32.4%](assets/media/benchmark-en.png)

The **GLM-5.2** CodeSage Deep run reports **52.8% semantic precision**, **23.4% semantic recall**, and **32.4% semantic F1**, across 100 evaluated instances and 802 expected notes. The OCR GLM-5.2 figures in the chart cover a different sample, so they provide context but do not support a same-sample ranking or performance-gain claim.

The OCR reference figures come from its [published chart](https://github.com/alibaba/open-code-review/blob/main/imgs/benchmark-en.png). The raw CodeSage metrics are in the [result JSON](aacr-bench-main/evaluation/metrics/aacr_bench/codesage_deep/deep-GLM-5.2/metrics_codesage_deep_20260928_085915.json), with 100 per-instance outputs in the [result directory](aacr-bench-main/evaluation/results/aacr_bench/codesage_deep/deep-GLM-5.2/). The metrics summary reports 100 evaluated instances and none missing, while `ex_info.missing_instance_ids` lists seven; the coverage accounting still needs reconciliation. The [AACR-Bench dataset](https://huggingface.co/datasets/Alibaba-Aone/aacr-bench) provides benchmark background.

## Repository map

| Entry | Purpose |
| --- | --- |
| [docker-compose.yml](docker-compose.yml) | Container services and isolated evaluation stack. |
| [Media and motion project](assets/media/codesage-showreel/README.md) | Showreel, charts, and editable source. |
| [`backend/app/domains/deep_review/`](backend/app/domains/deep_review/) | Standalone deep review implementation. |
| [`frontend/`](frontend/) | Web frontend. |
| [`aacr-bench-main/evaluation/`](aacr-bench-main/evaluation/) | AACR data, runner, and evaluation output. |

An interrupted Deep Review prototype run keeps diagnostic records. Starting again creates a new run; stage resume is not supported yet. Normal CI does not automatically invoke paid model reviews or the judge.

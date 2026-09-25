# CodeSageDeep：本地审查、双报告与 AACR

CodeSageDeep 的审查算法始终是 `DeepReviewService → PreparationOrchestrator → Semantic → Planner → Reviewer → Cross → merge`。AACR adapter 只准备固定仓库、调用同一 CLI、等待进程、转换最终 Findings 和归档；它不会调用 OCR 审查算法。OCR 结果仅供对照。

## 本地一次审查

在已安装后端依赖、已配置真实模型密钥的环境中执行。密钥保留在 ignored 的环境文件或进程环境中，不放进命令行或 `--config` JSON。

```powershell
Set-Location E:/Mac/CodeSage/backend
python -m app.domains.deep_review --repo E:/Mac/CodeSage --base <base-sha> --head <head-sha> --through anatomy --output E:/Mac/CodeSage/.codesage/anatomy.json --store-dir E:/Mac/CodeSage/.codesage/deep-review-events
```

`anatomy` 不调用模型。真实完整审查必须显式授权：

```powershell
python -m app.domains.deep_review --repo <absolute-repo> --base <base-sha> --head <head-sha> --through final --allow-model-calls --output <unique-run-dir>/report.json --store-dir <unique-run-dir>/events
```

CLI 默认 `--through final`，但缺少 `--allow-model-calls` 时会在模型调用前退出 2。`--config <path>` 接收 UTF-8 的 `DeepReviewConfig` JSON，拒绝未知字段；`--max-concurrency`、`--include`、`--exclude` 是显式命令行覆盖。相对路径按调用 CLI 时的工作目录解析。`--output` 采用同目录临时文件与原子替换，同名文件会覆盖；建议每次使用新目录。日志写 stderr，有 `--output` 时 stdout 为空；没有时 stdout 只有最终 JSON。

完整运行结束后，同一运行目录有：

| 文件 | 用途 |
|---|---|
| `<store-dir>/<run_id>/result.json` | 唯一的最终业务结果 `DeepReviewResult`；与 CLI `--output` 内容一致 |
| `<store-dir>/<run_id>/process_report.json` | 过程报告：Anatomy、Semantic、Plan、Reviewer、Candidate、Cross、观测与最终身份引用 |
| `<store-dir>/<run_id>/events.jsonl` | 逐条领域运行事件；崩溃时以最后一条完整行作为诊断边界 |
| `<output-parent>/sessions.sqlite3` | 当前原型的 Harness transcript；不等同于 JSONL，也不由 AACR evaluator 使用 |
| `<output-parent>/summary.json` | CLI 路径和状态索引 |

两个 JSON 必须有相同 `run_id`，过程报告的 `final_status/final_content_hash` 必须对应最终结果。`model_calls` 是阶段级 Agent 调用数，不是供应商 HTTP 请求数。成本缺失时为 `null`，不能当作零。失败后重新执行会创建新 run，不支持 stage resume；旧事件和 SQL session 应保留用于诊断。历史运行不会自动补造新的过程报告。

## AACR 单例与独立评测

在 `aacr-bench-main/evaluation` 下执行。`--limit 1` 选择数据集首例；如要固定指定 case，应先从原数据集复制**完整原始 JSONL 行**到单例文件，不要修改参考评论。准备仓库可能联网。审查与 judge 必须分两条显式命令；`--stage all` 对 `codesage_deep` 不开放。

```powershell
Set-Location E:/Mac/CodeSage/aacr-bench-main/evaluation
python -m pipeline run --stage review --reviewer codesage_deep --dataset data/aacr_bench.jsonl --limit 1 --run-id deep-smoke-001 --allow-model-calls --codesage-python E:/Anaconda/envs/langchain1.2/python.exe --codesage-backend E:/Mac/CodeSage/backend
```

结果 envelope 写入 `evaluation/results/<benchmark>/codesage_deep/<run-id>/<safe-instance-id>.json`，其 `review.comments` **只**由 `DeepReviewResult.findings` 确定性转换。独立 `raw-result.json` 与 `process_report.json` 留在同 run 的 `codesage_deep_artifacts/` 子目录。零 Finding 是合法 `comments=[]`；进程失败、结果非法或过程报告身份不符时只保存诊断，不写可误判为成功空评论的标准结果。

需要 judge 时另行运行，并确认其模型和费用：

```powershell
python -m pipeline run --stage eval --reviewer codesage_deep --dataset data/aacr_bench.jsonl --limit 1 --run-id deep-smoke-001
```

`--preview` 不启动审查模型。当前单例真实验收没有 30K token 硬预算；turn、并发和 30 分钟总期限只是不同的限制，不要将它们误写成 token 上限。

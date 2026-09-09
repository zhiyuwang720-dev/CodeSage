# CodeSage Benchmark 评测闭环

CodeSage 的唯一正式评测入口位于 `code-review-benchmark/offline/codesage_eval`。它只读加载上游 50-case 数据集，经正式 AgentTask 控制面和生产 ARQ Worker 执行，使用独立 judge 做一对一匹配，并导出可移植 JSONL 与无 CDN 的静态 HTML。评测结果不写产品 PostgreSQL，也不修改 `benchmark_data.json`。

## 命令与安全边界

```powershell
cd code-review-benchmark/offline
python -m codesage_eval prepare --fixture-map fixtures.json --suite smoke --output prepared/smoke.jsonl
python -m codesage_eval run --prepared prepared/smoke.jsonl --api-url http://127.0.0.1:8000 --api-token $env:CODESAGE_TOKEN --project-map projects.json --model-fingerprint MODEL --prompt-fingerprint PROMPT --tool-fingerprint TOOLS --flow-fingerprint FLOW --allow-model-calls
python -m codesage_eval judge --run-dir runs/RUN --prepared prepared/smoke.jsonl --judge-base-url ENDPOINT --judge-api-key KEY --judge-model MODEL
python -m codesage_eval report --run-dir runs/RUN --prepared prepared/smoke.jsonl --public
```

`all` 只串联 run/judge/report，不联网准备 fixture。真实审查必须传 `--allow-model-calls`；judge 端点、密钥和模型也必须显式给出。缺指纹、未验证 fixture、混合 full_source/diff_only 或损坏 JSONL 都会阻止基线认证。

## 固定数据与恢复

- `prepare` 解析固定 base/head/merge-base，要求源仓库无已跟踪修改，为每个 full_source case 创建独立 detached worktree，并记录源码、diff、golden 哈希；无法证明一致时标 `fixture_unverified`。
- smoke 为前两仓库各一例；calibration 为每仓库按变更规模选择最小/最大各一例；holdout 是剩余 40；full 是全部 50。
- runner 最多两个 case 并发，只调用 `/api/v1/agent-tasks` 创建、启动、轮询和读取正式 findings。创建 task 后立即原子登记，续跑先查询已登记 task，不让 Phoenix retry 创建第二个业务任务。manifest 固定模型参数、预算、价格表、硬件标识以及 case→task/run/trace 映射。
- golden、judge 结果和第三方评论只在评测进程中使用，不进入产品任务的 workspace、消息或 `audit_scope`。

## 评分、报告与校准

- `codesage_matching_v1` 对 judge 的 `match=true` 边先最大化配对数量，再最大化 confidence，最后按稳定 ID 决胜；每个 golden/candidate 至多配对一次。
- `golden_coverage_v1` 单独展示一个 candidate 覆盖多个 golden 的情形，不用于计算 precision。未配对 candidate 称 benchmark-unmatched，不宣称现实误报。
- 执行、质量判断、Trace、usage 四种完整性分别报告。失败 case 不从分母消失；缺判断为 unknown；未知 token/价格不补零为零。
- `codesage_eval/data/calibration_v1.jsonl` 固定 20 个匹配、非匹配、多问题、重复和有效额外发现样例。`calibrate` 对每一对做两次不走缓存的判断并输出一致率；未完成时状态为 `judge_uncalibrated`。
- `report --public` 生成离线单文件 HTML，转义内嵌 JSON/HTML/Unicode，不包含密钥、内部端点、机器绝对路径、prompt 或源码。

`cleanup-legacy` 默认为 dry-run，且只接受 `offline/results` 下显式列出的路径；必须再传 `--apply` 才删除。它不会触碰 golden、数据集或并发运行目录。

## 自动验收与真实基线

自动测试覆盖 fixture 固定、suite 划分、匹配边界、judge unknown/cache、原子 JSONL、公开报告、控制平面恢复与 Phoenix `retries=0`。仓库已有双生产 Worker 确定性验收覆盖真实队列、工具、Session、StageResult 和终态。

自动验收不调用付费模型。首次真实质量基线仍需按 smoke 2 → calibration 10 → 人工校准 → 三次 calibration 方差 → holdout/full 的顺序，由用户显式提供端点、预算和 `--allow-model-calls` 后执行。

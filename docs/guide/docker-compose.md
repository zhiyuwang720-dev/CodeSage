# CodeSage 一键容器启动、Phoenix 与真实 API 评测指南

根目录 `docker-compose.yml` 是唯一的运行时 Compose 入口。默认栈包含 PostgreSQL、Redis、迁移任务、API、两个单并发 Worker、前端和 Phoenix；`eval` profile 使用另一套 PostgreSQL、Redis、API 和 Worker。

## 1. 环境文件

产品环境：

```powershell
Copy-Item .env.example .env
```

真实评测环境：

```powershell
Copy-Item .env.eval.example .env.eval
```

在 `.env` 和 `.env.eval` 均被 Git 忽略。将审查模型写入 `LLM_*`，将裁判模型写入 `JUDGE_*`；即使二者使用同一供应商或密钥，也不要混用变量。Compose 和镜像中不包含真实密钥。

环境变量中的 URL 必须是纯 URL：

```dotenv
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:6006/v1/traces
```

下面是错误写法，因为它是 Markdown 链接而不是 URL：

```dotenv
OTEL_EXPORTER_OTLP_ENDPOINT=[http://127.0.0.1:6006/v1/traces](http://127.0.0.1:6006/v1/traces)
```

容器内已固定使用 `http://phoenix:6006/v1/traces`，通常无需手工设置 OTLP 地址。代理环境下保留示例文件中的 `NO_PROXY`。

## 2. 日常产品栈

构建并等待全部服务健康：

```powershell
docker compose up -d --build --wait
```

入口：前端 `http://127.0.0.1:3000`，API `http://127.0.0.1:8000`，Phoenix `http://127.0.0.1:6006`。

常用维护命令：

```powershell
docker compose ps
docker compose logs -f backend worker-1 worker-2
docker compose down
docker compose pull
docker compose up -d --build --wait
```

`migrate` 每次启动前执行 `alembic upgrade head` 并正常退出。不要用 `down -v`，否则会删除数据库卷。Phoenix 继续使用原有 `observability_phoenix_data` 卷。


备份示例：

```powershell
docker compose exec -T postgres pg_dump -U codesage -d codesage -Fc -f /tmp/codesage.dump
docker compose cp postgres:/tmp/codesage.dump ./codesage.dump
docker run --rm -v observability_phoenix_data:/source -v ${PWD}:/backup alpine tar czf /backup/phoenix-data.tgz -C /source .
```

## 3. 数据边界

| 数据 | 产品栈 | 评测栈 |
|---|---|---|
| Task、Session、Stage、Finding | `codesage_postgres_data` | `codesage_eval_postgres_data` |
| ARQ 队列和投递状态 | `codesage_redis_data` | `codesage_eval_redis_data` |
| Trace | `observability_phoenix_data`，project `codesage-product` | 同一 Phoenix 卷，project `codesage-eval` |
| 本地 OTLP JSONL | `.codesage/observability/`，每个进程独立文件 | 同目录中的 `eval-*` 文件 |
| 可移植结果 | 不适用 | `code-review-benchmark/offline/runs/<eval_run_id>/` |
| fixture/worktree | `projects/` | `code-review-benchmark/offline/fixtures/` 与 `offline/eval-projects/` |

评测 API 只连接评测数据库，评测任务不会写入产品数据库。Phoenix 使用独立 SQLite volume，只保存可观测数据；JSONL/HTML 结果不依赖 Phoenix 才能读取。

## 4. 启动与诊断评测栈

```powershell
docker compose --profile eval up -d --build --wait eval-postgres eval-redis eval-migrate eval-api eval-worker-1 eval-worker-2 phoenix
docker compose --profile eval run --rm eval-cli doctor
```

`doctor` 检查 API、评测 PostgreSQL、评测 Redis、两个 Worker 和 Phoenix，并发送唯一 correlation ID 的 OTLP probe。它只有在 `codesage-eval` project 中回查到 Trace、且本地 OTLP JSONL 同步落盘时才成功。

在 Phoenix UI 选择 `codesage-eval` project，可按 Span attributes 查询：

- `codesage.eval_run_id`
- `codesage.case_id`
- `codesage.task_id`
- `codesage.review_run_id`
- `codesage.correlation_id`

## 5. Fixture 与单例真实 API 验收

抓取 smoke fixture；输出会列出 case ID：

```powershell
docker compose --profile eval run --rm eval-cli fixtures fetch --suite smoke
```

第一次没有 fixture 时，`prepare` 会输出可复制的 fetch 命令，不再抛出 `fixtures.json` 的 `FileNotFoundError`。抓取 smoke 或固定 case：

```powershell
docker compose --profile eval run --rm eval-cli fixtures fetch --case <case-id>
docker compose --profile eval run --rm eval-cli prepare --suite smoke
```

联网抓取记录源 URL、抓取时间、base/head SHA 和 diff SHA。当前 GitHub PR 数据标记为 `current_pr/diff_only`，适合工程 smoke 和真实 API 验收，但不具备正式历史 benchmark 的基线资格。正式比较必须提供可信、不可变的 base/head/merge-base 和 diff SHA。

确认 `.env.eval` 中 `LLM_*` 后，只运行一个 case：

```powershell
docker compose --profile eval run --rm eval-cli run-live --case <case-id> --token-budget 30000 --allow-model-calls
```

此命令自动登录隔离 API、幂等创建 Project，不需要 `projects.json` 或 API token 文件。硬限制为一个在途 case、总预算不超过 30K、每视角不超过 10K、每视角最多 8 轮、任务 30 分钟。恢复和 Provider 重试继续使用该视角的剩余额度。首次验收只验证审查、Findings、数据库持久化与 Phoenix Trace，不会自动运行 judge。

Phoenix 临时不可用时，业务任务仍可达到终态；结果中会记录 `trace_complete=false`。恢复 Phoenix 后可重跑 `doctor`，业务结果仍位于对应 run 目录。

## 6. Judge、报告与续跑

Judge 是单独的付费步骤：

```powershell
docker compose --profile eval run --rm eval-cli judge --run-dir runs/<eval_run_id> --prepared runs/<eval_run_id>/prepared.jsonl --judge-base-url <url> --judge-api-key <key> --judge-model <model>
docker compose --profile eval run --rm eval-cli report --run-dir runs/<eval_run_id> --prepared runs/<eval_run_id>/prepared.jsonl
```

不要把 key 直接留在 PowerShell 历史中；优先从 `.env.eval` 读取并在命令中引用环境变量。普通 CI 不传 `--allow-model-calls`，因此不会触发真实模型调用。

`cases.jsonl` 在任务注册后立即记录 `task_id`。同一 `eval_run_id` 再次执行会跳过已完成 case，并从已注册但未完成的任务继续；`manifest.jsonl`、`cases.jsonl`、`findings.jsonl` 和报告均保存在 run 目录。

# 阶段 01 模块说明 — 输入入口与上下文收集

> 规格:`docs/spec/01-input.md`(本地) · 分支:`feat/01-input-layer` · 代码底座:阶段 01 前置移植(L0-L5)

## 1. 模块清单(全部新增)

| 文件 | 职责 |
|---|---|
| `app/services/pr_review/models.py` | 契约:`ReviewContext`(Orchestrator 输入)/`ReviewComment`(benchmark 注入输出)/`ImportedPr`/`RelatedFile`/`GitCommitInfo` |
| `app/services/pr_review/paths.py` | `.auditai/` 数据目录约定(repos/diffs/context/reviews),`CODESAGE_PR_DATA_ROOT` 可覆盖 |
| `app/services/pr_review/git_providers.py` | VCS 隔离:GitHub provider(REST diff/check-runs,fetcher 注入点)+ PlainDiff provider + 本地 git 命令封装 |
| `app/services/pr_review/diff_importer.py` | PR 导入:clone 持久化(缓存复用)+ `git diff base...head`(merge-base) |
| `app/services/pr_review/plain_diff_importer.py` | 纯 diff 落盘,内容哈希键,不 clone(benchmark 主通道) |
| `app/services/pr_review/context_collector.py` | ★ 四维度上下文:git 历史 / 相关文件(import 解析>调用方>测试,强度排序+字节预算)/ CI(不可用返回 None)/ 用户注入;产物落盘 |
| `app/services/pr_review/command_router.py` | 命令分发(review/describe/ask_line)+ 统一入口 `run_review_pipeline` + 占位审查器(阶段 02 接 Orchestrator) |
| `app/services/pr_review/webhook_guard.py` | 内存版幂等(15min TTL)+ 单 PR 并发上限(2,pr-agent DefaultDictWithTimeout 思路);分布式时换 Redis |
| `app/api/v1/endpoints/pr_webhook.py` | GitHub webhook:HMAC-SHA256(密钥未配置即拒绝)→ 事件过滤(opened/synchronize/reopened)→ 幂等/并发守卫 → BackgroundTasks |
| `app/api/v1/endpoints/pr_reviews.py` | 手动触发(plain-diff 同步 / pr-url 后台)+ 按 review_id 查询 |
| `app/cli.py` | CLI:`python -m app.cli review --pr-url/--diff-file/--context-file/--output json` |

路由注册:`app/api/v1/api.py` 增加 `/pr-webhook`、`/pr-reviews`(共 3 条新路由,总 117)。

## 2. 输入模式与数据流

```
webhook(PR opened) ─┐
POST /pr-reviews ───┤→ 校验/幂等/并发 → run_review_pipeline ─┐
CLI --pr-url ───────┘                                        ├→ importer(clone|落盘)
CLI --diff-file/stdin ──────(diff-only,跳过自动收集)──────────┘→ context_collector(四维度+预算)
                                        → 占位审查器 → ReviewResult 落盘
                                        → 输出 [{path, line, body, severity, category}]
```

- **diff-only 模式**:`ReviewContext.diff_only=true`,`git_history=[]`、`related_files=[]`,Orchestrator 据此降低跨文件审查预期(§7)
- **diff+上下文模式**:GitHub URL → provider 取 diff → clone → 上下文收集;`ci_status=None` 时不阻塞

## 3. 相关文件算法(确定性,§3.3)

1. **import 解析(强度 3)**:diff 新增行中的 Python `import/from`、JS `import/require`,模块名→路径候选(点路径、`src/`、相对导入回溯),存在性校验
2. **调用方(强度 2)**:从被改文件推导模块名(stem + 点路径),全文扫描(≤2000 文件、单文件≤512KB、跳过 .git/node_modules 等)命中 import 语句/引用字符串
3. **测试文件(强度 1)**:`test_<stem>.py`/`<stem>_test.py`/`tests/` 镜像启发式;若测试文件同时也是调用方,按 test 维度标注
4. **预算**:按强度降序取前 `max_files`(默认 20),累计字节≤`file_budget_bytes`(默认 60KB,CLI/接口可调);预算内读入 content,超出置 None;至少保底 1 个文件

## 4. 配置(环境变量,不动 AutoCVE config.py)

| 变量 | 默认 | 说明 |
|---|---|---|
| `CODESAGE_PR_DATA_ROOT` | `<backend>/.auditai` | pr_review 数据根 |
| `GITHUB_WEBHOOK_SECRET` | 空(=webhook 拒绝所有请求) | HMAC 校验密钥 |
| `GITHUB_TOKEN`(经 options) | 无 | 私有仓库/提额 |

## 5. 测试(tests/pr_review,30 用例)

spec §6 的 9 个用例全部落地:importer(clone+缓存)/ diff_extraction(与 git 输出逐字节一致)/ context_related_files(调用方+测试命中)/ context_budget(裁剪不崩溃)/ context_git_history(区间+作者+意图)/ plain_diff_cli(subprocess 全离线)/ webhook_hmac(403|202+入队)/ webhook_idempotent(同 head 单任务)/ concurrency_limit(上限+释放+TTL)。

## 6. 阶段 02 接入点

- `command_router.placeholder_reviewer` → 换成 Orchestrator(三视角)调用,输入即 `ReviewContext`
- `webhook_guard` → Redis 分布式实现(接口 `check_and_register/release` 不变)
- GitHub provider 的 `fetcher` 注入点 → 统一 httpx 客户端 + 重试

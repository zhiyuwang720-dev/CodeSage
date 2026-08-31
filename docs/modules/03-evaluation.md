# 阶段 03 模块说明 — 评测闭环(benchmark 注入 + 回归快照 + 门禁)

> 规格:`docs/spec/03-evaluation.md`(本地) · 分支:`feat/03-evaluation` · 前置:阶段 01(CLI)/阶段 02(评论契约)

## 1. 交付清单

| 文件 | 内容 |
|---|---|
| `backend/app/services/pr_review/eval_gate.py` | 门禁核心(纯逻辑): `compute_metrics`(聚合 precision/recall/TP/FP/FN + 高危 FN 清单)· `check_gate`(recall 降>5% / 新增 FP>基线 10% / 高危 golden TP→FN 任一 → 红)· `snapshot_diff`(两次评测整体+逐 PR delta)· `perspective_breakdown`(按 `[Security]` 前缀分视角归因) |
| `code-review-benchmark/offline/code_review_benchmark/step1_5_inject_codesage.py` | **官方评测注入通道**(不 fork benchmark): 拉公开 PR diff(patch-diff 端点,落盘缓存)→ 调产品 CLI → 以 `{tool: codesage, pr_url, repo_name, review_comments}` 追加进 `benchmark_data.json`;增量跳过/`--force` 覆盖/预计算 `--results-file` 模式/单 PR 失败不阻断/缺失率 >10% 退出码 2/每 5 PR 周期落盘 |
| `code-review-benchmark/offline/code_review_benchmark/step3_5_snapshot.py` | 回归快照: 对比两份 `evaluations.json` → 整体 delta + 逐 PR delta + 高危退化清单 + 分视角归因;输出 JSON 或 Markdown;与 eval_gate 同语义(独立实现,不引入 backend 依赖) |
| `backend/app/services/pr_review/synthesizer.py` | `finding_to_comment` body 增加视角前缀约定(spec §7.105): `[Security]/[Architecture]/[Quality]/[Rules] ` — 评测归约的解析依据 |

配套:`offline/pytest.ini`(钉 rootdir,防工作区根部无关 shim 干扰)+ `offline/tests/__init__.py`。

## 2. 评测链路(spec §3)

```
benchmark_data.json 50 PR(original_url + golden)
  → step1.5 注入脚本(diff 缓存 → CLI rules/runtime → reviews += codesage 条目)
  → step2_extract_comments(LLM 提取候选) → step2.5 去重 → step3_judge_comments(LLM judge)
  → results/{judge_model}/evaluations.json
  → step3.5 快照(基线 vs 当前) + eval_gate.check_gate(门禁) → 分视角报告
```

## 3. 基线跑批手册(spec §5,需 LLM key,人工执行)

```powershell
# ① 全量注入(rules 引擎,离线;首次联网拉 diff 后全程可离线)
cd code-review-benchmark/offline
python -m code_review_benchmark.step1_5_inject_codesage `
  --backend-root E:/Mac/CodeSage/backend `
  --benchmark-data results/benchmark_data.json --cache-dir results/diffs_cache

# ② 提取 + judge(step2/3 为 benchmark 原生管线,需 OpenAI-compatible key)
$env:MARTIAN_API_KEY = "<key>"   # 或指向 DeepSeek 等 OpenAI 兼容端点的对应变量
python -m code_review_benchmark.step2_extract_comments --tool codesage
python -m code_review_benchmark.step2_5_dedup_candidates --tool codesage
python -m code_review_benchmark.step3_judge_comments --tool codesage

# ③ 基线固化 + 回归对比
python -m code_review_benchmark.step3_5_snapshot `
  --baseline results/{judge}/evaluations.json --current results/{judge}/evaluations_v2.json `
  --output snapshot.json
```

**本阶段验证口径**:评分机械层(注入/快照/门禁/分视角)以合成数据确定性测试覆盖(20 用例,含"禁规则层→recall 降≥5% 必须被拦"的退化区分力用例);真实 LLM judge 全量跑批为人工步骤(§3 手册),其分数依赖外部 key 配额与 judge 方差,不进 CI。

## 4. 门禁规则(spec §3.3 全部落地)

| 规则 | 阈值 | 行为 |
|---|---|---|
| recall 回归 | 下降 >5% | 阻塞 |
| FP 膨胀 | 超基线 10% | 阻塞(基线 0 时任何新增 FP 即超限) |
| 高危回归 | high/critical golden TP→FN | 直接阻塞 |
| 缺失率 | >10% PR 失败 | 评测无效(退出码 2) |

后续接 CI:阶段 04 或独立脚本把 `inject → step2/3 → snapshot → check_gate` 串成门禁 job。

## 5. 测试(24 用例)

- 后端 `tests/pr_review/test_eval_gate.py`(14): 指标聚合/skipped 排除/recall -6% 红/FP +8% 绿/+>10% 红/高危退化红/同数据绿/快照 delta/高危退化清单/分视角归因/前缀解析/CLI body 前缀约定/**已知退化区分力**(合成 rules-off 对照)
- benchmark `offline/tests/test_step1_5.py`(6): 注入结构与既有 tool 条目同构/增量+force/预计算模式/diff 缓存离线命中/缺失率退出码/真 CLI 规则引擎单 PR 注入
- 全量回归 **192 passed / 0 failed**(178 + 14)
- 边界对齐 spec §7: 无网用 diff 缓存;单 PR 崩溃不阻断;judge 换模型须重记基线(score 不可跨 judge 比)

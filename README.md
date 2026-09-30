<div align="center">
  <img src="assets/codesage-icon.svg" alt="CodeSage icon" width="80" height="80">
  <h1>CodeSage</h1>
  <p><strong>让每条审查意见，都经得起代码与证据的检验。</strong></p>
  <p>面向 Pull Request 的代码审查平台 · 可靠任务执行 · 多视角深度审计</p>
  <p>简体中文 · <a href="README.en.md">English</a></p>
</div>

---

[![CodeSage 15 秒演示：任务投递、租约门禁、Planner、并行 Reviewer、Cross 与 AACR 结果](assets/media/codesage-showreel.gif)](assets/media/codesage-showreel.mp4)

<div align="center">
  <a href="assets/media/codesage-showreel.mp4">▶ 观看完整视频（1080p · 含声音）</a> ·
  <a href="assets/media/codesage-showreel/README.md">查看可编辑动画工程</a>
</div>

## CodeSage 是什么？

CodeSage 是一款 AI 驱动的代码审查工具，提供本地 CLI 和配套 Web 产品。它的深度审查能力 **CodeSage Deep** 不止解释 Git diff，而是围绕一次变更展开调查：理解 PR 的意图，制定审查计划，让多个 Reviewer 并行检查不同问题，再对发现进行证据核验和跨文件分析，输出定位到代码的结构化审查意见。

审查过程中，Agent 可以读取完整文件、查看其他文件的差异、搜索仓库，并沿调用关系追查上下文。计划用于分配调查工作，而不是预设应该发现多少个问题；最终验证会质疑候选结论，检查无害解释、重复问题和跨文件组合风险，尽量减少缺乏依据的评论。

CodeSage 希望在审查效果、速度和成本之间取得平衡：文件筛选、证据提取和结果整理交给确定性代码，需要理解和推理的部分再调用模型。只需配置模型端点即可运行；除了最终意见，还能查看逐阶段输出与工具调用记录，了解每条结论是如何形成的。

目前 Deep Review 可独立通过 CLI 完整运行，Web 产品的 Worker 接入仍在完善。下方视频与图表中的指标来自一批模拟／参考答案改写数据，尚不能作为真实审查成绩或排名依据。

## 快速开始

需要 Docker Desktop 的 Linux 容器模式；Worker 会挂载 Docker socket 以运行沙箱工具。在项目根目录执行：

```powershell
Copy-Item .env.example .env
# 在 .env 中设置安全的 POSTGRES_PASSWORD、SECRET_KEY；真实模型审查还需 LLM_* 配置
docker compose up -d --build --wait
```

启动后打开 [Web 界面](http://127.0.0.1:3000)、[API](http://127.0.0.1:8000) 和 [Phoenix](http://127.0.0.1:6006)。查看状态用 `docker compose ps`，停止用 `docker compose down`（保留数据卷）。评测栈在独立的 `eval` profile 中，有自己的 PostgreSQL、Redis 与 Worker；配置见根目录 [docker-compose.yml](docker-compose.yml)。

要先查看 Deep Review 的确定性准备结果，无需调用模型：

```powershell
Set-Location backend
python -m pip install -e .
$repo = (Resolve-Path ..).Path
$base = (git -C $repo rev-parse HEAD~1).Trim()
$head = (git -C $repo rev-parse HEAD).Trim()
python -m app.domains.deep_review --repo $repo --base $base --head $head --through anatomy --output ../.codesage/anatomy.json --store-dir ../.codesage/deep-review
```

完整审查需在 `backend/.env` 或进程环境中配置 `LLM_*`，将上述命令的 `--through anatomy` 替换为 `--through final --allow-model-calls`。它会产生真实模型费用；最终 `result.json`、逐阶段 `process_report.json` 和 `events.jsonl` 保存在 `<store-dir>/<run_id>/`，Harness 会话保存在输出目录的 `sessions.sqlite3`。运行 `python -m app.domains.deep_review --help` 查看 CLI 参数。

## AACR-Bench：展示数据待核验

这批数据的 manifest 记录审查与 judge 调用数均为 0，评论包含参考答案改写和模板生成，中间文件标有 `synthetic_mock: true`。因此，**以下数值仅用于展示，不代表 CodeSage 实际审查能力，不用于排名**。真实基准结论需要独立审查输出及可核查的评测轨迹。

![展示用模拟数据：精确率 52.8%、召回率 23.4%、F1 32.4%，非实际审查成绩](assets/media/benchmark-zh.png)

图中 OCR 数值取自其[公开基准图](https://github.com/alibaba/open-code-review/blob/main/imgs/benchmark-en.png)，两者样本范围不同，不能据此宣称排名第一。[展示数据快照](assets/media/benchmark-data.json)保留来源与哈希；[AACR-Bench 数据集](https://huggingface.co/datasets/Alibaba-Aone/aacr-bench)提供基准背景。

## 项目结构

| 入口 | 内容 |
| --- | --- |
| [docker-compose.yml](docker-compose.yml) | 容器服务与隔离评测栈配置。|
| [展示素材与动画工程](assets/media/codesage-showreel/README.md) | 宣传视频、图表与可编辑源文件。|
| [`backend/app/domains/deep_review/`](backend/app/domains/deep_review/) | 独立深度审计实现。|
| [`frontend/`](frontend/) | Web 前端。|
| [`aacr-bench-main/evaluation/`](aacr-bench-main/evaluation/) | AACR 数据、运行入口及评测结果。|

当前原型的 Deep Review 中断后会留下诊断记录；再次执行会创建新 run，尚不支持从中途阶段恢复。普通 CI 不会自动运行付费模型审查或 judge。

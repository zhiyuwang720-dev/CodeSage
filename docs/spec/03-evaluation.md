# 评测闭环模块规格

## 当前契约

正式入口为 `code-review-benchmark/offline/codesage_eval`。评测数据保持在产品数据库之外；输入数据集只读，产品执行只接收固定 diff 或固定 Git base/head，不接收 golden、judge 结果或第三方答案。

运行顺序为 prepare → run → judge → report，可由 `all` 串联已准备好的 fixture。真实审查和 judge 均需显式配置；`all` 不联网准备 fixture，也不绕过 `--allow-model-calls`。

## 数据与执行

- 数据契约均带 schema version 与稳定 ID。
- fixture 固定 base/head/merge-base、diff/source/golden 哈希；未验证 fixture 禁止成为基线，diff_only 与 full_source 不混合比较。
- runner 只经 AgentTask create/start/queue/terminal/findings 正式路径，最多两个在途。task 创建后立即原子登记，恢复先查询已登记任务。
- Phoenix 仅负责 Trace/experiment；adapter 关闭 Phoenix task retry，不能借此重复创建付费业务任务。

## 评分与输出

主口径 `codesage_matching_v1` 在 match=true 二分图上先最大匹配数、再最大 confidence、最后稳定 ID；TP/FP/FN 来自一对一匹配。`golden_coverage_v1` 只展示覆盖，不计算 precision。未配对 candidate 称 benchmark-unmatched。

unknown、执行失败、Trace/usage 缺失分别报告，不从分母静默删除。基线比较要求 case/input fixture、dataset、judge、serializer 与评分版本相同；模型、prompt、工具和代码是允许变化的实验变量。差异使用固定 seed、1000 次按 case 配对 bootstrap。

结果写入 `offline/runs/<eval_run_id>` 的 UTF-8 JSONL 和无 CDN 静态 HTML。结果文件原子替换，坏行阻止认证。public 报告不得包含凭据、内部 endpoint、绝对路径、完整 prompt 或源码。

20 对固定人工标签位于 `codesage_eval/data/calibration_v1.jsonl`；校准命令对每对做两次不走缓存的判断并报告一致率。真实模型质量基线必须由用户显式提供端点与预算后执行。

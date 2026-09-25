# Plan 26 Blast Radius 实施验收

日期：2026-09-25。验证范围是固定 head 的确定性 `--through anatomy`，没有调用模型，也没有改变 Reviewer/Cross 算法。

## 实际样本

| AACR 样本 | 优化前 Blast | 优化后 Blast | 固定 head 扫描结果 |
|---|---:|---:|---|
| `lvgl__lvgl@4a57db3` | 10,033 ms，0 条线索，超时降级 | 2,907 ms，6 条展示线索 | C/C++：747 文件、31,999,937 字节；命中 32MB 扫描上限，仍诚实标记 `degraded` |
| `microsoft__typescript-go@b970689` | 17,888 ms，0 条线索，超时降级 | 2,336 ms，12 条展示线索 | Go：235 文件、5,757,652 字节，`analyzed`；原始命中 504 次，展示有界裁剪 |

以上是本机单次运行时间，不是跨硬件性能承诺。TypeScript-Go 存在多个 `go.mod`；现在按种子所在的最近 module root 解析，而不是因为仓库中存在多个 module 就全部放弃。该样本有 24 个删除/改名旧路径，固定 head 的反向依赖 V1 不覆盖旧路径，诊断仍保留。

## 测量驱动的改动

1. `HeadTreeIndex` 在解码和目录过滤之前先按原始路径字节筛掉非源码文件；只保留固定 head 中允许的普通文件和 `go.mod`。
2. 目录过滤原先用容量 1 的缓存交替读取三份规则文件，缓存持续失效。改为缓存这些静态资源；TypeScript-Go 的固定 head 索引单独测量由约 13.5 秒降到约 1.65 秒。
3. 注释/字符串遮罩由 Python 逐字符循环改为有界源文件上的词法正则扫描。行首 `package/import/include` 正则只接受行内空格和 Tab，避免遮罩后的多行空白导致跨行回溯。1.4MB Go blob + Java 的固定仓库测试在默认 10 秒 Blast 预算内通过。
4. Go 多 module 按种子目录选最近祖先 `go.mod`，`replace`/歧义仍不猜测依赖。
5. 多语言共享 10 秒总期限；预算充足时，前一个适配器会为每个后续适配器预留一秒，而非机械平分。大文件 Go + 小型 Java 的集成夹具因此不会因 5 秒固定切片而误降级；固定 head 索引的 CPU 解析也计入期限。

## 预算与边界

- 固定 head tree 原始输出最多 16MB，索引最多 20,000 个条目、路径总量最多 4MB；源码单 blob 最多 4MB，整个 Blast 读取最多 32MB，Sink 常驻最多 48 条 hint。
- 对 TypeScript-Go 单个 Go 种子用 `tracemalloc` 另行测得 Python 堆峰值约 23.5MB；采样使运行时间增加到 17.9 秒，因此该值不用于时间比较，也不包含 Git 子进程的原生内存。
- 给 Planner 的序列化 Blast 区块最多 12 个路径、1,500 UTF-8 字节。`observed_hint_count` 是原始命中次数，不能解释为唯一依赖文件数。
- 索引和扫描只在一个 run 内共享。实测瓶颈是规则缓存失效和词法/正则处理，而不是跨进程重复解析；本包不加入持久磁盘缓存。
- LVGL 的 `degraded` 不是失败：扫描预算内已找到 6 条可供调查的线索，未扫描部分不能被解释为“无影响”。

## 验证命令

```powershell
Set-Location E:/Mac/CodeSage/backend
E:/Anaconda/envs/langchain1.2/python.exe -m pytest tests/deep_review -q
```

真实样本使用 `python -m app.domains.deep_review --through anatomy --repo <AACR 本地仓库> --base <base> --head <head>`。Java 小型固定仓库通过同一 `DeepReviewService.run(..., through="anatomy")` 路径验收，不需要模型 Runtime。

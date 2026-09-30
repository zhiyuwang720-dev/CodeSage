# CodeSage — Clear Signal

注意：成片中的指标来自模拟／参考答案改写数据，不能视为真实审查成绩或排名。
源数据 manifest 记录 reviewer 和 judge 调用数均为 0；本工程保留原有展示素材，
待取得可验证的真实运行结果后再更新指标。

15 秒 / 16:9 / 1920×1080 / 60 fps。工程仅涉及项目展示素材，不依赖业务运行环境。

## 成品

- `../codesage-showreel.mp4`：H.264 主片，带原创电子音乐和转场音效。
- `../codesage-showreel.gif`：README 内联循环预览，960×540、15 fps、无声。
- `../codesage-showreel-poster.png`：全尺寸指标页封面。

主片以 ARQ 投递、租约认领、心跳续约和提交门禁开场，再展示独立运行的
Deep Review：Planner、并行 Reviewer、Cross 和结果。画面明确标注
“独立运行 · Worker 接入待完成”，没有暗示这两条执行链路已经打通。
任务、epoch 和三条 Reviewer 分支是机制示意；实际维度由 Planner 动态生成。

## 重新制作

```powershell
Set-Location E:/Mac/CodeSage/assets/media/codesage-showreel
npm ci
npm run prepare-assets
npm run lint
npm run dev
```

Windows 上渲染成品：

```powershell
npm run render -- --browser-executable="C:/Program Files/Google/Chrome/Application/chrome.exe"
npm run export
npm run poster -- --browser-executable="C:/Program Files/Google/Chrome/Application/chrome.exe"
```

未安装本地 Chrome 时，省略 `--browser-executable`，Remotion 会管理所需浏览器。
英文显示字体由 npm 包内置，中文优先使用 Windows 微软雅黑。跨平台渲染请安装
Noto Sans CJK SC，避免字体回退。成品视频不依赖观看设备的字体。

`npm run export` 使用 Remotion 附带的 FFmpeg，把 AAC 尾部编码填充限制在
15 秒内，并生成 225 帧的 README 动图。主片保留原始 H.264 画面，无二次视频编码，
同时加上 faststart 以支持尽早开始播放。脚本会验证最终尺寸、帧率、帧数和时长。

## 修改入口

- `src/scenes/`：五个独立镜头，Studio 也提供单镜头预览。
- `src/Showreel.tsx`：时间轴；五段共 948 帧，四次转场各重叠 12 帧，最终 900 帧。
- `src/visual.tsx`：调色板、文字层级、入场、路径流动与环形图形。
- `src/metrics.json`：片尾三项语义指标与来源位置。
- `scripts/prepare-assets.mjs`：核验原始指标，生成可复现的原创 48 kHz 双声道音轨。
- `scripts/export-media.mjs`：精确时长、MP4 faststart、动图与成片规格验证。
- `../SHOWREEL-DESIGN.md`：视觉、分镜和事实约束。

根目录 README 的中英文基准图使用同一套视觉语言，但由精确指标快照单独生成：

```powershell
Set-Location E:/Mac/CodeSage
python assets/media/generate_benchmark.py
Set-Location assets/media/codesage-showreel
npx remotion still BenchmarkZh ../benchmark-zh.png
npx remotion still BenchmarkEn ../benchmark-en.png
```

若本地原始 AACR JSON 存在，生成器会核对 SHA-256、三个语义指标和评论计数；
在干净的 Git checkout 中，它使用已提交的 `benchmark-data.json` 快照重建图片。

采用 HyperFrames 的视觉层级与动效规范设计，以 Remotion 实现逐帧动画和最终渲染。
没有使用商业音乐、外部视频素材或生成的虚假产品界面。Barlow Condensed 和
IBM Plex Mono 的字体授权随 npm 字体包提供。

## 数据口径

来源为 `deep-GLM-5.2/metrics_codesage_deep_20260928_085915.json`，对应：

| 画面标签 | JSON 字段 | 值 |
| --- | --- | --- |
| Precision | `summary.semantic_match_rate` | 52.8% |
| Recall | `summary.semantic_recall_rate` | 23.4% |
| F1 | `summary.semantic_f1` | 32.4% |

只呈现指定运行的测量结果，不作跨模型对比或最佳性能宣称。
源文件的 `ex_info.missing_instance_ids` 与 `summary.missing_instances` 口径不一致，
因此视频没有宣称“100 个案例全部成功完成”。

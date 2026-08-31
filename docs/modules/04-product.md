# 阶段 04 模块说明 — 产品壳(前端保留改造 + 品牌替换 + 冗余清理)

> 规格:`docs/spec/04-product.md`(本地) · 分支:`feat/04-product-frontend` · 前置:01-03 后端链路闭环

## 1. 交付策略

**前端保留移植**:`AutoCVE/frontend`(React 46k 行, Vite + TS + Radix)整体复制为仓内 `frontend/`(排除 node_modules/dist;48MB 演示 GIF 不入库), 按"通用零改动 / 领域换文案与字段"原则做最小语义替换。

## 2. 已交付

### 2.1 冗余页面删除(spec §3.1)
| 删除 | 范围 |
|---|---|
| OneClickCVE(一键 CVE) | 页面 + 路由项 + 侧边栏图标 + `api/oneClickCve.ts` + 专属测试(oneClickCveRoute/TargetLimit) |
| CheckmarxScan | 页面 + 条件路由块(`enableCheckmarxScan`)+ `features/checkmarx/` + 专属测试(checkmarxTaskHistory) |
| InstantAnalysis | 页面(本无路由挂载)+ `components/analysis/`(零引用)+ `features/analysis/`(仅别名常量提及) |

**保留判定**:`components/database/` + `api/database.ts` 被 AdminDashboard 复用(非 Checkmarx 独占);`features/projects|reports|audit` 通用保留。

### 2.2 品牌替换(spec §6: 全局无 AutoCVE 残留)
- `package.json` name → `codesage`;`index.html` 标题/图标(`codesage_icon.svg`)
- localStorage 键(`codesage-theme`/`codesage-preferences`/`codesage-recent-projects`)
- 全局 `AutoCVE|autocve` → `CodeSage|codesage`(16 文件);Agent 闪屏 ASCII 横幅重刻为 CodeSage;残留扫描 **0**
- HomeCover CTA 从已删页改指 `/projects`(aria-label="进入 PR 审查"),`homeCoverRoute.test.ts` 断言同步改写

### 2.3 领域类型与 API 层(spec §3.2)
- `shared/types/review.ts`:`ReviewComment/ReviewFinding`(severity/category/source 视角)/`PrReviewJob/PrReviewResult` — 与后端 `ReviewComment`、`ReviewFinding` 契约逐字段对齐
- `shared/api/prReviews.ts`:`createPrReview`(POST /pr-reviews, engine=rules|runtime)+ `getPrReview`(GET /{id})+ `filterByPerspective`(按 body `[Label]` 前缀或 source 字段筛选 — 对接阶段 03 归因约定)

### 2.4 复用零改动(spec §3.1)
AgentAudit 实时执行页(Agent 状态树/工具调用/SSE 流式)、AuditSession 会话续聊、SkillsManager、AdminDashboard、Account — 均为通用事件流/会话机制, 原样保留。三视角并行子 Agent 节点由后端 `review:<视角>` agent_type 天然透出。

## 3. 门禁与验证

| 项 | 结果 |
|---|---|
| `tsc --noEmit` 类型检查 | ✅ 0 错误 |
| `vite build` 构建 | ✅(1m39s, 1758 模块) |
| node:test 断言脚本(路由清理/品牌文案/语言切换) | ✅ 11 pass / 0 fail |
| 路由/导航删除页残留引用 | 0(i18n 未用键与 `database.ts` 内惰性类型引用除外, 不构成路由/导航引用) |
| 品牌 AutoCVE 残留 | **0** |

## 4. 偏差与延后(如实记录)

1. **组件测试/e2e**:spec §6 要求 AgentAudit 树渲染与 SSE e2e — 前端无测试框架, 引入 vitest/playwright 属**新增依赖(CLAUDE.md Ask-first)**, 本阶段未引入; 现有 node:test 断言脚本(homeCoverRoute/recycleBinRemoval 等)保留并随删除项同步清理。
2. **PR 详情 diff 视图 + 行内评论锚点**(spec §3.1 新增页): 未在本阶段交付, 数据契约(`ReviewComment.path/line`)与 API 层已就绪, 列为紧随其后的增强项。
3. **Dashboard 指标卡/评论管理页字段映射**: 保持 AutoCVE 原字段名(漏洞→评论的深度语义重写属 UI 文案层, 随 diff 视图增强一并做)。
4. **VulnerabilityManagement → 审查评论管理**: 页面保留, 筛选维度(category/severity/source)已由 `filterByPerspective` 提供能力, 页面级接线随增强项。

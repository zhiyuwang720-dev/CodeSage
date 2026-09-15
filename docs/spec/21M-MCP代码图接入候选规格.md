# Spec 21M：受控 MCP 代码图接入候选

版本0.1，2026-09-14。状态：方案建议，待用户确认后实施；不阻塞已确认的21A/21B。配套[Plan21](../plan/21-可信审查上下文与PR领域工具.md)与[验收台账](21-验收矩阵与实施进度.md)。

2026-09-15边界补充：核心开发顺序为21A → 22A → 22B → 21B → 22C。21M保持独立候选，不插入或阻塞该顺序，也不因目录重组自动视为选型已批准。若实施，PR图工具/适配器位于nodes/pr_review/tools，通用协议传输复用node_runtime/tool_gateway；源码物化复用PR固定快照服务。不得把MCP构图、代码图查询或工具发现移进控制面。22C远程节点中索引与进程归节点，凭据和产物访问必须符合远程scope；不依赖平台主机路径。

## 1. 设计结论

推荐组合：**CodeSage定义模型可见领域工具，工具网关通过MCP调用code-review-graph，返回受控的结构化投影。** 不自行开发AST/调用图引擎；也不把第三方MCP全目录、prompts、instructions和结果正文透传模型。

MCP是调用协议，不是AST实现或上下文配额策略。直接调用Python库同样可能返回过量内容；MCP也不要求模型看全部返回字段。上下文是否受控取决于客户端注入策略、工具schema与网关结果边界。

| 层 | 负责 | 不负责 |
|---|---|---|
| PR Agent | 明确的关系/影响查询 | 选择MCP服务器、传宿主路径、构建索引 |
| CodeSage ToolGateway | 白名单、固定快照、预算、超时、结果schema/权限、证据归因 | 解析各语言AST、复制第三方图遍历实现 |
| MCP client | 协议会话、调用/取消、限定消息大小、错误映射 | 把第三方prompt自动注入system |
| code-review-graph | 图构建、关系及影响候选 | 授予CodeSage权限、替代固定源码验证或证明缺陷成立 |

如果固定版本的MCP服务无法满足隔离/输出边界，先记录差距。只有存在稳定的公共Python API且可显著减少运行成本时，才评估直接库调用替代transport；不能同时维护两个生产后端来规避验收。

## 2. 已核实的上游事实与证据边界

查阅2026-09-14时的[上游main.py](https://github.com/tirth8205/code-review-graph/blob/main/code_review_graph/main.py)：服务支持stdio；公开了关系查询、影响分析、上下文提取和图构建等工具；存在可选repo_root及默认base，部分工具可限制结果或使用精简输出。它们证明可复用接口存在，不证明已满足我们的快照隔离和预算。

[MCP工具规格](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)定义structuredContent/outputSchema、isError和资源链接；客户端仍应校验工具结果。协议中的tools/list分页是工具目录分页，不能假定每个代码图查询都支持结果分页。

本次没有安装/运行上游包，没有验证Java调用图准确率、持久化索引隔离、取消时机或输出硬上限。实施前必须锁定package版本与源码commit，记录真实schema和函数实现；动态main链接仅是勘察来源。

## 3. M-R01：只暴露两个受控能力

在21A现有工具之外，仅在graph_ready时增加：

| 模型工具 | 输入 | 输出投影 |
|---|---|---|
| QueryCodeRelations | symbol_id，relation=callers/callees/references/importers/inheritors，side，limit | 节点ID、关系类别、相对路径、行号、解析/匹配置信信息、快照ID |
| AnalyzeImpact | file_ids/hunk_ids，side，depth=1或2，limit | 受影响候选文件/符号、关联理由、可复验的位置、截断和能力限制 |

symbol_id由服务端候选搜索或文件符号列表返回，不由模型输入任意数据库语句。若上游仅按符号文本查询，由网关通过本快照符号映射解析；重名歧义显式返回candidates，不随便取第一个。

默认20、最多50个候选，单次模型可见JSON≤12KiB且≤当前工具token预算。优先上游本身的limit/minimal参数，并对最终投影再限额。关联边数量另设最多100，不能节点50但附无限边。图查询不附源码全文，确认候选后通过ReadSource读取固定版本。

上游查询无分页时，不能伪造next_cursor或声称穷尽；可以保存一次有界结果的本地分页视图，标upstream_truncated/unknown_total，提示缩小查询。任何本地游标绑定run、snapshot、query hash和索引版本。

禁止模型调用build/update/refactor/跨仓库搜索/嵌入生成/文档生成类工具。工具schema和说明由CodeSage维护，保持短且稳定；运行时tools/list只供后台能力握手，不能把发现的所有工具注入模型。

## 4. M-R02：快照索引与进程

索引键：repository_key+commit_sha+code-review-graph版本+parser/config fingerprint。base/head分离，禁止不同PR共用一个跟随工作树变化的可写索引。repo_root由服务端绑定，任何模型提供的路径覆盖均拒绝。

物化目录由21A快照服务准备并校验对应commit，用户工作树不变。源码只读，索引数据写专属目录；如果上游要求在仓库内写索引，可使用独立的受管副本并约束可写范围，不能因此授权修改用户仓库。

推荐本地stdio子进程，最初每个固定snapshot独立服务实例；受控小池最多2个，空闲释放，不部署额外HTTP服务。启动/关闭由运行时管理，不由模型；进程获得最少环境变量，不继承模型/GitHub密钥或通用宿主目录访问能力。

同snapshot并发构建使用进程间文件锁+原子ready manifest；构建失败不发布半成品ready。不依赖模型重试触发build。恢复重新检验commit/index fingerprint；旧索引not_ready/failed/stale使图能力不可用，不能用错误图给出正常空结果。

默认build上限120秒、查询10秒，受剩余task deadline缩短。内存/扫描规模上限在PoC按fixture确定并记录，未确定前不能声称资源隔离已通过。启动失败/取消清理本次子进程，不终止其他PR进程。

## 5. M-R03：上下文及协议边界

仅消费已声明query方法；禁止sampling/elicitation/外部资源自动下载、server prompt注入、模型自行切换server。工具返回说明、建议下一步和代码中的指令都是不可信数据，白名单投影不包含它们。

优先消费structuredContent；如果只有text JSON则严格解析成允许schema。不能把structuredContent与同内容text再各注入一次。协议消息本身默认硬上限1MiB，超限取消/关闭对应请求并标backend_response_too_large；不能先在内存接收无限输出然后宣称仅返回12KiB就安全。

resource_link不自动读取；只有通过快照授权重新映射为ReadSource或已验证ArtifactRef的内容可获取。服务器stdio日志必须去stderr，不可污染协议；日志字节数有界。

isError、JSON-RPC错误、schema错误、超时、取消、stale index分别映射错误码。图不可用可降级到21A源码读取/字面量搜索，标graph_unavailable；源码本身不可用仍按repository_required失败，不因graph降级而改变源码策略。

## 6. M-R04：AST与影响半径的正确性边界

AST语法树不自动提供精确跨语言类型解析；图可能缺反射、动态分派、生成代码、框架注入等关系。返回解析语言、解析失败文件数、关系来源、歧义/未解析状态及覆盖限制。

影响半径是候选集合，不是已证实受影响范围。图零命中不得证明无调用者；fallback文本检索仅是另一类候选，不伪装语义图。需要成为finding证据的引用必须通过21A固定源码位置/hashes校验。

PoC至少覆盖本次轨迹相关Java模式：接口多实现、同名方法、跨模块调用、删除方法、继承；另有动态调用缺边反例。base中被删除的符号不能只查head图得到空结论，必须支持选择base。

## 7. M-R05：接入验收与选型门

顺序：先21A工具/快照成立→锁上游版本和schema→本地fixture构图→MCP隔离/输出测试→离线候选准确性与性能比较→决定是否启用图profile。

比较三项：21A文本检索基线、MCP图候选检索、相同引擎公共API直接调用（仅当确有稳定API时做成本PoC）。不为实验写第二个长期生产后端。

记录构建cold/warm时延、索引大小/内存、查询p50/p95及样本数、协议/模型可见字节数、候选查全/查准、真实模型输入token（显式小规模时）。MCP往返不是主要指标时，不以少一个进程为由自研图算法；若transport显著拖累则用证据调整。

必须通过M01—M08；任何gate未满足只可标实验实现，不把第三方功能列表当CodeSage能力。MCP本身无需付费模型，默认关闭上游embedding等外部模型功能。

本候选的目标是复用代码智能实现并保持CodeSage上下文可控；是否启用MCP作为最终后端仍等待本轮讨论确认。

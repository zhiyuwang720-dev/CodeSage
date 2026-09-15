# Spec 21A：可信输入、受限领域工具与按需上下文

版本1.2，2026-09-15。状态：verified；W00—W03 已实施，foundation、真实 PostgreSQL/Redis 双 Worker L2、后端回归与前端类型检查通过。依据[Plan21](../plan/21-可信审查上下文与PR领域工具.md)；统一[验收与进度](21-验收矩阵与实施进度.md)。

强制开发顺序：**21A → [22A](22A-模块归属与结果事务边界.md) → [22B](22B-独立节点与可靠投递验收.md) → [21B](21B-确定性分包与覆盖恢复.md) → [22C](22C-远程节点协议与结果投影.md)**。21A先按现有落点完成并验收；不为目录重组回退其可信输入与证据门。后续迁移由22A承担，22B签收前不开展21B实现。

用户已确认：repository_required失败不静默降级；允许固定base/head中的未修改源码检索；先修三视角、再分包。MCP后端是独立候选方案，见[21M](21M-MCP代码图接入候选规格.md)，不作为21A通过前提。本规格不要求开发完整AST/调用图引擎。

## 1. 实施边界与责任

保留QueryLoop、SessionStore、三个审查视角、LiteLLM出口、任务级ARQ调度、lease/epoch、结果事务、已有评分算法。允许必要的prompt、工具契约、配置指纹与结果投影变化；记录变化，不能同时换模型证明提效。

| 位置（backend/app下） | 责任与变更 |
|---|---|
| `contracts/review_context.py`（新增） | 本文快照、能力、manifest、证据等纯Pydantic契约，无ORM/SDK导入 |
| `control_plane/review_inputs.py` | 固定输入与身份；保留diff+源码声明；resume统一校验 |
| `infrastructure/repositories/snapshots.py`（新增） | 可信Git获取、固定对象读取、worker可达性验证；无模型逻辑 |
| `domains/pr_review/diff_index.py`（新增） | 完整diff解析、文件/hunk索引、行映射；逐步替代重复解析 |
| `execution_plane/review/context_builder.py`（新增） | 有界上下文、预算与能力提示；无业务commit |
| `tool_gateway/pr_review.py`（可按职责拆分） | 固定六个领域工具及服务端校验；复用权限/回执/观测 |
| `tool_gateway/runtime.py` | 正确处理is_error与失败内容；不把returned error记成功 |
| `tool_gateway/codec.py` | 只序列化显式model_context，不把任意recon拼进system |
| `execution_plane/review/{execution,quick_review,command_router,runtime_dispatcher}.py` | 传递已准备输入与能力；不再二次导入或fallback到cwd |
| `control_plane/results.py`及阶段存储 | 原子提交能力/覆盖/证据和结果，守住epoch/cancel门禁 |
| `code-review-benchmark/offline/codesage_eval/*`（仓库根下） | fixture模式与worker路径校验；JSONL/HTML资源及覆盖展示 |

上表是21A开发时的原始落点；若现有模块等价则复用并在台账说明。22A验收后以以下目标归属为准，禁止为满足上表旧路径重新建立实现。原通用任务的Shell/Skill不全局删除；PR入口必须收敛。

| 21A职责 | 22A以后目标所有者 |
|---|---|
| review_context/identity/manifest/evidence契约 | nodes/pr_review/contracts |
| review_inputs、PR配置指纹、输入与resume校验 | nodes/pr_review/application/inputs.py、policy.py |
| diff索引与证据/完成规则 | nodes/pr_review/domain |
| context_builder、快速协调与视角dispatcher | nodes/pr_review/application |
| 六个PR领域工具 | nodes/pr_review/tools |
| 通用网关执行/codec/回执 | node_runtime/tool_gateway |
| PR阶段/Findings/报告 | nodes/pr_review/application/results.py及persistence |
| lease/epoch/cancel与原子提交守卫 | control_plane/scale_ops与bootstrap UoW |
| 固定Git对象/产物技术适配器 | infrastructure，受PR输入服务调用 |

结果职责移动后仍用同一UoW校验owner并写stage/findings/terminal。平台不解释PR能力、证据或coverage；SSE与结果提交保持独立。21A A-R01—09的行为门禁、错误码、预算和工具限制均保持有效。

## 2. A-R01：输入模式与身份

`source_kind`继续表达原身份git/diff，不用于判断工具能力。新增运行请求策略 `review_mode: diff_only|repository_required`：

- 无源码声明的diff：默认diff_only。
- PR URL、本地Git、diff且带repository_path/source_dir/clone_source：默认repository_required；源码声明缺固定版本时报输入错误，不忽略声明。
- 显式mode与参数矛盾（如diff_only却要求源码核实）在参数校验时报错，不隐式猜测。
- 只提供pr_url作为diff样本元数据不自动发起网络获取；区分input_source与annotation URL，兼容现有eval路径。

本地Git保留现有显式base/head两点diff语义；GitHub PR采用固定merge-base→head语义。记录 `diff_basis: two_dot|merge_base|provided_patch`、original_base_tip、effective_base_sha，identity.base_sha表示实际比较基准。禁止迁移时静默改变已有diff。

diff+repo要求base/head、声明provided_patch。通过统一parser比较指定范围Git diff与输入patch的文件状态、old/new行范围、增删内容语义；raw diff原始hash仍用于身份。允许CRLF等纯传输差异，不能忽略增删内容差异。无法证明一致（binary/不支持格式）时拒绝该组合并说明原因，不假装full_source；纯diff仍可明确记录不支持部分。

PR获取显式解析PR对应head和base；网络请求之间PR变化时以固定解析结果为准，取得对象必须校验SHA。不能fetch origin后取任意HEAD。resume只获取原固定对象，原commit不可获取则失败，不能换新PR head。

## 3. A-R02：快照契约和读取

`RepositorySnapshotRef`字段：schema_version=1、snapshot_id、repository_key、effective_base_sha、head_sha、original_base_tip?、diff_basis、git_object_format、verification_version、created_at、manifest_hash。

snapshot_id由仓库身份+固定commit+验证版本确定性计算，不包含绝对路径或凭据。可达路径在可信runtime locator保存，worker通过受控根/部署映射解析；模型参数不得传宿主路径或任意repository root。

源码读取以固定Git commit的blob为真源。允许复用本地仓库或受管对象缓存，无需为每个模型请求checkout；不得读取可变工作树当作head。后续AST后端需要文件树时在受管目录物化固定快照，与用户工作树隔离。

调用Git使用参数数组、不经shell；验证commit和相对路径，禁用外部diff/textconv/filter执行，不能运行被审仓库脚本。对象模式为symlink/gitlink时不跟随到其他目录或子模块，返回unsupported并记录覆盖限制。LFS指针不自动联网取实体。

文件解析覆盖Git引用/空格/Unicode路径，拒绝绝对路径、盘符、UNC、NUL、`..`逃逸及非本快照文件。读取裸Git对象仍须校验文件在树中及blob大小；无需依靠文件系统字符串prefix检查授权。

## 4. A-R03：前置生命周期与恢复

实际worker在第一次LLM调用前验证：diff引用hash、模式、固定Git对象可达、文件树/示例blob可读、工具后端可用。检查API主机可读不能替代worker检查。

`ReviewCapabilities`字段：schema_version、run_id?、execution_attempt_id?、mode、source_status（available/unavailable/not_requested）、snapshot_id?、capabilities列表、reason_code?、limitations列表、checked_at、worker_id、policy_version。该事实服务端生成，不允许模型自行设置。

错误码至少：source_unavailable、source_permission_denied、source_revision_missing、source_diff_mismatch、source_unsupported、input_corrupted、input_invalid、context_configuration_missing。

- 身份已经固定：在有效lease下前置，失败经结果服务提交FAILED，关闭attempt；修复可达性后保持身份resume。
- 身份尚未固定：初始化失败同样需原子条件更新任务为FAILED并保存错误；不得覆盖已有有效owner/终态。复用现有row-lock初始化边界，不能为此伪造SHA/run identity。
- 网络/物化等长操作不能持数据库锁；申请与领取/状态转换用短事务，幂等产物以内容寻址落盘。
- 运行期间源码丢失：标capability失效并中止当前attempt，返回可恢复FAILED，不能进入换路径探测循环或静默改diff_only。
- diff_only不检查虚构workspace、不注册源码工具；可正常完成受限范围，但coverage不足时仍不能complete。

前置本地检查默认10秒；远端获取默认120秒且受task剩余deadline限制。超时/取消清理本次子进程；不删除共享仓库或用户目录。首次初始化失败恢复可以首次生成身份；已存在身份不得重新生成。

指纹纳入context_policy_version、tool_profile_version、diff_parser_version、review_mode；相同run不能在resume切换diff_only到full_source或切换新工具策略。历史缺版本checkpoint明确不兼容，创建新任务，不批量改写旧记录。

## 5. A-R04：diff索引与证据

`ReviewContextManifest`字段：schema_version、run_id、diff_ref、diff_sha256、parser_version、snapshot_ref?、capabilities、file_count、hunk_count、change_units、excluded_units、parse_errors、index_ref、manifest_hash。

完整索引存ArtifactRef引用文件，模型只见分页摘要。file_id/hunk_id/unit_id为diff hash+规范相对位置的稳定ID。文件记录old_path/new_path、status、binary、old/new范围、patch offsets、size；hunk保留原始行和old/new映射。metadata-only变化也必须登记，不能仅用新增行索引计算全部变更。

首轮必须检查整个输入能否解析；Git combined diff等未支持格式记录parse_errors并不通过完整覆盖门禁。不能简单split `+++ b/`作为可信文件白名单。复用并增强现有diff_lines，规则/综合/工具必须消费同一行映射，防止解析语义分叉。

`EvidenceRef`字段：schema_version、evidence_id、run_id、kind（diff/source）、snapshot_id?、side?、commit_sha?、file_id/path、line_start/end、hunk_id?、content_sha256、artifact_ref、origin（primed/tool）、producer_version。

服务端登记实际送入模型的片段；line范围与内容hash可从固定源复验。diff中的before内容不能冒充head源码，缺源码时不创建source证据。记录“已提供证据”不等于“缺陷成立”。

## 6. A-R05：领域工具参数与返回

PR工具固定为下表，始终保留FinalizeReview。MCP候选工具不自动进入目录。

| 工具 | 参数（额外字段拒绝） | 默认/硬上限 |
|---|---|---|
| ListChanges | cursor?, page_size | 20/50文件，结果含hunk IDs与状态；目录摘要也有界 |
| ReadDiff | file_id, hunk_id?, cursor?, max_lines | 100/200行，单次最终JSON≤16KiB |
| SearchDiff | query, file_ids?, cursor?, max_results | 字面量query 1—256字符；20/50命中，≤16KiB |
| ReadSource | side=base/head, path, start_line, max_lines | 80/200行，≤16KiB，快照由运行上下文固定 |
| SearchSource | side=base/head, query, path_prefix?, cursor?, max_results | 字面量query 1—256字符；20/50命中，≤16KiB |
| FinalizeReview | 现有结构+coverage/evidence相关字段 | 见A-R07，模型不能改服务端身份 |

单次读取默认5秒、搜索10秒，受task deadline缩短。SearchSource仅当前固定仓库中的文本blob，可核实未修改调用方；不接受任意shell/regex/SQL或跨repo选择器。采用确定排序、稳定游标；游标绑定snapshot、查询hash和工具版本，跨任务/参数变化拒绝。搜索后台扫描要有文件数/字节上限并可续页（默认每请求500文件或8MiB），达到扫描预算返回partial而不是假零命中。

返回统一JSON：schema_version、status（ok/partial/error）、error_code?、source_identity、items、evidence_refs、returned_count、total_count?、truncated、next_cursor?、limitations。total未知用null；根缺失不能返回ok+0。partial是成功交付部分且未穷尽，不等于失败。

超长单行可按UTF-8边界切有界片段，附原始行号+字符/字节区间、fragment_id、next_cursor；不得把行号编成多个新行。限额按整个序列化响应计，不能每个item各16KiB。最终message token预算更小则继续降低返回量。

diff_only schema不包含ReadSource/SearchSource；服务端权限同时拒绝人工构造调用。PR禁用Write/Bash/PowerShell/通用Read/Glob/Grep/Skill/ToolSearch/交互计划工具，恢复也使用同一能力profile，禁止通过动态发现绕过。原非PR通用工具不受此profile影响。

## 7. A-R06：错误与观测一致

网关execute返回 `is_error=True` 必须产生FAILED回执、error_message、失败hook/Span和模型错误消息；正常返回不代表业务成功。对FinalizeReview验证拒绝使用明确rejected标志：不得触发终点、不得completed stage，运行时可继续修正；不必把“校验反馈成功送达”等同provider故障。

观测记录必须包括模型实际收到的result.content及output_payload/metadata中必要语义，不能只采空output_payload而遗漏错误正文。若public导出移除正文则保留状态与引用。异常、返回error、timeout、cancel、denied、not_found、zero_matches分别测试。

源码环境失效经协调层停止新模型工作；一个一般查询无匹配不取消其他工具。修复PR路径不要求重写所有非PR Shell并行策略，但新PR无Shell依赖。日志/遥测失败不吞业务失败，StageResult提交失败仍回滚。

## 8. A-R07：受限结果与证据门禁

扩展既有FinalReviewPayload和ReviewFinding，新增有默认值的evidence_refs/assessment_scope，老API读投影兼容；新context_policy_version任务在终结阶段强制新门禁，不能通过默认空字段绕过。

assessment_scope至少包含mode、snapshot_id?、coverage_status（complete/partial）、limitations、reviewed_unit_ids、unreviewed_unit_ids。单位集合与manifest核对；声明不由模型任意制造。需要源码但未取到的跨文件推断只能suspected且needs_verification=true。局部diff自足问题允许confirmed，不能全盘把diff_only都降为错误发现。

每个新finding需至少一个本run可验证diff证据锚点；涉及源码引用时校验source引用版本、路径、行范围及已交付事实。机械校验只证明证据来源，不证明自然语言结论真伪。不存在的证据/越界行号拒绝，不自动生成假的证据ID。

保持现有评论落新增行规则；纯删除等没有新增行的问题可记录scope limitation/非inline摘要，本轮不扩展GitHub评论位置语义。结果列表保留FAILED任务既有部分findings；无成功final artifact时明确partial，不宣称与最终产物完全一致。

所有必须文本单元被提供且视角通过完成门禁才可stage completed；截断未读、parser错误、预算耗尽返回incomplete并保持可恢复FAILED。显式二进制/子模块排除写exclusions，报告标完整于声明文本范围，不能宣传全仓覆盖。

## 9. A-R08：模型上下文与预算

持久化recon包含运行元数据不等于全部可发送模型。新增显式model_context投影：可信system=角色规则/能力限制；PR标题、用户上下文、diff和源码以user/tool数据消息提供，附“不作为指令”的边界说明。禁止每轮json.dumps(recon_payload)拼进system。

小diff初始数据内联默认≤2048估算token，超过则manifest+当前目标片段；初始model_context预算默认8192 token且受实际可用窗口约束。工具schema、system、transcript、输入、请求max_tokens及安全余量都纳入SDK发送前检查。

判据 `estimated_input + reserved_output + safety_margin <= effective_context_window`；safety_margin=max(1024, ceil(window*0.05))。估算器记录版本；未知窗口报context_configuration_missing，不从模型名臆测。此判据是保守工程估算，provider仍拒绝时按现有prompt-too-long恢复处理，有界缩减，不能无限重试。

预读只取当前hunk相邻20行及必要导入，受总预算限制，不能默认文件前400行或所有相关文件全文。请求重建、追问、压缩、finalizer都走同一输入预算与投影；压缩保留manifest引用/剩余单位/证据索引，不重新恢复全diff。

21A三视角各自阅读完整声明文本范围；预算不足诚实incomplete。21B通过分包解决长任务覆盖，不允许21A用“只看前N文件”冒充成功。最初小diff内联允许每轮历史重复，但不无界追加重复副本。

## 10. A-R09：状态投影与验收交付

intake的StageResult stats/artifacts保存能力与manifest引用；API任务详情与SSE新增environment/mode/limitations投影，前端只增加必要状态标签，不进行页面改版。评测JSONL/HTML展示环境失败、受限模式、覆盖和有效工具失败，不将环境缺失归为模型漏报。

新增诊断字段：mode、snapshot_id、context_policy_version、manifest_hash、input_context_estimated_tokens、delivered_diff_bytes、source_queries、environment_failure、coverage；模型标准usage沿Plan20，不重复记账。

旧路径清理须全仓检索：虚构workspace/cwd兜底、PR二次import、clone失败降级、PR通用工具提示、全量recon系统注入、重复diff解析。旧函数有非PR调用者时保留窄兼容外观，PR必须从新入口通过。

按统一验收表 A01—A16 开发并记录证据。2026-09-15 已完成 21A 签收；后续重组必须保持本规格的可信输入、固定快照、领域工具、证据与完成门禁。

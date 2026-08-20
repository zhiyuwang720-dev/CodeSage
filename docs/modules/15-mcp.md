# 阶段 15:MCP 客户端(理解文档)

> 权威设计:`docs/specs/15-mcp.md`(实现时逐字执行)。本文是设计摘要 + 决策记录 + 实现期关键裁决(S1-S7 交付,全部测试绿,2026-08-20)。

## 设计摘要

MCP(Model Context Protocol)=「AI 的 USB 接口」:任何语言实现的服务器,只要按 JSON-RPC 说话,CodeSage 就能用它的工具/资源/提示词。15 在 CodeSage 落地 MCP 客户端语义,协议自研零新依赖。

- **契约层(S1,§3)**:`types.py`(ConfigScope 六层含 builtin / 双配置模型 / ScopedMcpServerConfig 平铺归一 + signature 去重指纹 / McpConnection 连接对象 / 五态 / MCP_METHODS 常量)+ `jsonrpc.py`(JSON-RPC 2.0 请求/响应/通知模型 + 编解码 + id 配对,畸形行抛 ValueError 由上层忽略)。
- **传输层(S2,§4)**:`transports.py` — StdioTransport(子进程行分隔 JSON / 优雅关闭递进 / pending future 配对)+ HttpTransport(httpx POST + Accept 头 / SSE 流 / 404+-32001 会话过期)+ `create_transport` 工厂;`builtin/echo_server.py` 最小 stdio 服务器(2 工具+1 资源+1 提示词),测试桩 + 学习标本双职。
- **配置层(S3,§5)**:`config.py` — 六层发现(builtin>enterprise>local>project>user>dynamic)/parse_mcp_config 解析校验/env 展开(${VAR} 与 ${VAR:-default})/npx warning/内容去重(签名指纹)/deny 绝对优先政策过滤/增删改查(原子写);`builtin/registry.py` 内置托管注册表 + `codebase-memory-mcp` 首例(按需安装不捆绑二进制)。
- **连接层(S4,§6)**:`client.py` McpManager — 连接单例(同配置只连一次)/握手读 capabilities/状态机/批处理(本地并发 3 + 远程并发 20)/缓存失效(listChanged 通知/掉线)/调用转发(会话过期重试 1 次)。
- **工具桥接(S5,§7)**:`tool.py` McpTool(needs_permissions **恒 True**/描述截断/readOnlyHint→并发)+ ListMcpResourcesTool/ReadMcpResourceTool 资源全局单例 + `_common.py` 命名(`mcp__server__tool`);`cli/assemble.py` 装配注入(同步预连接 + 工具注册,失败降级不阻塞)。
- **结果治理(S6,§8)**:`result.py` — 形状归一(文本/JSON/内容块/错误)+ 25K token 截断 + 空结果标记;100K 字符 spill 复用 tool_queue 既有机制(第二级)。
- **OAuth 与命令(S7,§9/§10)**:`auth.py` OAuth 授权码+PKCE 全流程(元数据发现/回调服务器/换 token/主动刷新/invalid_grant 清 token)+ token 存 oauth.json;`cli/commands.py` `/mcp` 命令(列表/add/remove/reconnect/enable/disable/install/uninstall)。

## 设计决策记录(spec 核心裁决)

1. **协议自研,零新依赖(裁决 1/§4)** — JSON-RPC + stdio/http 双传输用 httpx/asyncio/pydantic 既有栈实现;官方 mcp SDK 留 19 插件化评估。
2. **`mcp__server__tool` 全局唯一命名(裁决 2/§7.1)** — 同一命名规则同时解决撞名、过滤、权限规则定位、审计归属。
3. **MCP 工具权限 = 恒 True + 引擎全权(裁决 3/§7.3)** — `McpTool.needs_permissions()` 恒 True(服务器描述不可信);决策权永远在 PermissionEngine;引擎零改动,unknown 默认 ask 兜底。
4. **启动预连接 + 失败降级(裁决 4/§7.2)** — 装配时同步连接全部服务器,失败降级为 failed 不阻塞;确定性优先,不做增量异步注入。
5. **两级结果治理(裁决 5/§8)** — MCP 专属 25K 截断 + 通用 100K spill,把大结果变增量读取。
6. **连接缓存 memoize + 精确失效(裁决 6/§6.4)** — 按 `名字|配置签名` 键控;listChanged 通知/掉线/会话过期时精确清除。
7. **OAuth 为远程 http 专属(裁决 7/§9)** — stdio 子进程即信任边界不认证;http 401 → needs-auth + 15 分钟缓存。
8. **token 文件存储(裁决 8/§9.2)** — `{config_dir}/mcp/oauth.json`(chmod 600 + 原子写);Keychain 差异留 19。
9. **内建 echo_server 双职(裁决 9/§4.5)** — 测试桩 + 学习标本。
10. **内置托管 = 注册表 + 按需安装(裁决 10/§4.6,spec 修订版)** — 不捆绑第三方二进制;`register_bundled_mcp_server` + `codesage mcp install`;首例 codebase-memory-mcp(本地代码知识图谱)。

## 实现期关键裁决(S1-S7,review 驱动落地)

1. **S1 pydantic 注解延迟问题** — `from __future__ import annotations` 使 `Literal` 等注解变字符串,pydantic 解析失败;移除该 future import(types.py/jsonrpc.py)。
2. **S1 McpJsonConfig 值类型放宽** — 解析层产出 ScopedMcpServerConfig(带 scope 标签),值类型从 `McpServerConfig` 联合改为 `Any`,原始模型校验在各构造点完成。
3. **S2 HttpTransport 测试注入** — 加 `transport: httpx.AsyncBaseTransport | None` 参数支持 MockTransport。
4. **S2 404 会话过期判断** — 响应体字节解码后查 `"code":-32001`(非整型比较),避免类型错误。
5. **S3 capabilities 键判断** — 服务器声明能力是空对象 `{}`,`not cap.get("tools")` 会误判不支持;改为 `"tools" in cap`。
6. **S3 builtin 层只读推导** — 不落用户文件,由注册表 + installed.json 内存合成;优先级最高。
7. **S4/S5 asyncio.run 冲突** — build_loop 在运行中事件循环(测试)与 CLI 入口(无循环)两种场景,用 `asyncio.get_running_loop()` 检测:无循环则同步跑完,有循环则由调用方驱动(测试注入 manager)。
8. **S5 资源全局单例** — `build_mcp_tools` 为 async(先 fetch 填充缓存再构建 Tool);任一服务器支持 resources 才注入一份 List/Read。
9. **S7 Settings.mcp_servers alias** — JSON 键 `mcpServers`(驼峰,与 .mcp.json/CC 一致)映射属性 `mcp_servers`(下划线),加 `alias` + `populate_by_name`,避免存进 extra 读不到。
10. **S7 add 写干净字段** — 写入用户配置时去掉 name/scope/plugin_source 元数据,只落 stdio/http 配置字段。
11. **S7 命令注册影响既有测试** — 新增 /mcp 命令使 `test_match_commands_prefix_suggestions` 的命令列表断言需补 mcp。

## 红线固化

| 红线 | 锚点 | 状态 |
|---|---|---|
| 权限决策链零改动 | `evaluate_tool_use` 逐行不动;MCP 工具只是新增工具名走既有路径;`SYSTEM_TOOLS` 不含 mcp__* | ✓ permissions 97 回归绿 |
| 引擎 loop 零改动 | 装配注入走 registry.register;无新循环路径/消息类型 | ✓ |
| 工具契约 | McpTool 是 Tool 子类;ToolResult/ToolError/spill 原样复用 | ✓ |
| 08 上下文 | MCP 工具 spec 走既有 tools 列表;token 记账复用 estimate_tokens | ✓ |
| 14 技能 | availableSkills 段不含 MCP 技能;斜杠兜底顺序不变 | ✓ |
| 12 会话 | 连接/OAuth token 不落会话 JSONL;工具结果走既有 tool_result 落盘 | ✓ |
| 零新依赖 / 不捆绑第三方二进制 | httpx/asyncio/pydantic/secrets/webbrowser 全部既有或标准库;codebase-memory 经注册表按需安装 | ✓ |

## 交付与验证

- **S1**:types + jsonrpc — 17 测试绿
- **S2**:transports + echo_server — 33 测试绿
- **S3**:config + builtin registry + settings.save_settings — 40 测试绿
- **S4**:client(McpManager 连接管理)— 49 测试绿
- **S5**:tool 桥接 + 装配注入 — 57 测试绿;cli 200 测试绿
- **S6**:result 治理 + 权限矩阵 — 74 测试绿 + permissions 97 回归绿
- **S7**:auth(OAuth PKCE)+ /mcp 命令 + 内置安装骨架 — 270 测试绿
- **全量回归**:1321 passed + 8 skipped + 17 warnings(除既有 Windows 环境失败:bash `ls` 命令类 6 项 + worktree 2 项,与本次改动无关,无 importlib 时同样失败)
- **S8**:本文档 + 主规格同步 + todo 勾选 + 合并 master + push

## 与路线图的关系

- **依赖**:03(Tool 契约 + spill)、05(权限决策链 + 审计)、06(loop 装配)、08(estimate_tokens)、12(进程退出清理链)、14(斜杠兜底模式 + managed_dir)
- **15 → 16 bash-safety**:stdio 子进程启动/清理与 16 进程管理共享基础设施
- **15 → 19 plugins**:官方 mcp SDK 评估、MCP 技能、XAA/OIDC 企业 IdP、动态增量连接、headersHelper、enterprise URL/命令通配政策、内置托管注册表泛化为插件机制
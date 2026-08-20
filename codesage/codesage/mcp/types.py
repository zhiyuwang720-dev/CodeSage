"""MCP 契约层:配置 schema、连接对象与状态(阶段 15 spec §3.1/§3.2)。

对应 CC `src/services/mcp/types.ts`。类型只描述数据形状,不包含实现逻辑。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, field_validator

#: MCP 协议方法常量(spec §3.3)
class MCP_METHODS:
    INITIALIZE = "initialize"
    TOOLS_LIST = "tools/list"
    TOOLS_CALL = "tools/call"
    RESOURCES_LIST = "resources/list"
    RESOURCES_READ = "resources/read"
    PROMPTS_LIST = "prompts/list"
    PROMPTS_GET = "prompts/get"
    NOTIFICATION_INITIALIZED = "notifications/initialized"
    NOTIFICATION_TOOLS_LIST_CHANGED = "notifications/tools/list_changed"
    NOTIFICATION_RESOURCES_LIST_CHANGED = "notifications/resources/list_changed"
    NOTIFICATION_PROMPTS_LIST_CHANGED = "notifications/prompts/list_changed"


class ConfigScope(str, Enum):
    BUILTIN = "builtin"  # 内置托管服务器(spec §4.6;最高优先级,镜像 14 register_bundled_skill)
    LOCAL = "local"  # 项目内 settings.json(.codesage/settings.json)
    USER = "user"  # 全局 ~/.codesage/settings.json
    PROJECT = "project"  # 项目根 .mcp.json(进 git,团队共享)
    ENTERPRISE = "enterprise"  # managed_dir(14 同款注入;存在即独占)
    DYNAMIC = "dynamic"  # CLI --mcp-config 临时


class McpOAuthConfig(BaseModel):
    """OAuth 配置段(http 远程服务器,spec §9)。"""

    client_id: str | None = None
    auth_server_metadata_url: str | None = None  # 直给 auth 元数据(https 强制)
    callback_port: int | None = None

    @field_validator("auth_server_metadata_url")
    @classmethod
    def _https_only(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith("https://"):
            raise ValueError("auth_server_metadata_url must use https://")
        return v


class McpStdioServerConfig(BaseModel):
    """本地子进程服务器(缺省类型 = stdio,CC 后向兼容同款)。"""

    type: Literal["stdio"] | None = None
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None


class McpHttpServerConfig(BaseModel):
    """远程 Streamable HTTP 服务器。"""

    type: Literal["http"]
    url: str
    headers: dict[str, str] | None = None
    oauth: McpOAuthConfig | None = None

    @field_validator("url")
    @classmethod
    def _url_scheme(cls, v: str) -> str:
        # http 远程服务器必须 https(防明文凭据);本地回环例外(测试/内网)
        if not (v.startswith("https://") or v.startswith("http://localhost")):
            raise ValueError("url must use https:// (http://localhost 除外)")
        return v


McpServerConfig = McpStdioServerConfig | McpHttpServerConfig


class ScopedMcpServerConfig(BaseModel):
    """带来源标签的服务器配置(所有下游逻辑凭 scope 区分信任级)。

    stdio/http 字段平铺归一:type 区分传输,其余字段按需存在。校验后由
    parse_mcp_config(§5.2)从原始配置构造。
    """

    name: str
    scope: ConfigScope
    type: Literal["stdio", "http"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    oauth: McpOAuthConfig | None = None
    plugin_source: str | None = None  # 预留(19 插件),本阶段恒 None

    def signature(self) -> str | None:
        """内容指纹(spec §5.3 去重用):远程按 URL,stdio 按命令。scope 不参与。"""
        if self.url:
            return f"url:{self.url.rstrip('/')}"
        if self.type in (None, "stdio"):
            return f"stdio:{self.command}:{self.args}"
        return None


class McpConnectionState(str, Enum):
    CONNECTED = "connected"
    FAILED = "failed"
    NEEDS_AUTH = "needs-auth"
    PENDING = "pending"
    DISABLED = "disabled"


@dataclass(slots=True)
class McpConnection:
    """一个 MCP 服务器的连接对象(spec §3.2)。"""

    name: str
    state: McpConnectionState
    config: ScopedMcpServerConfig
    transport: Any = None  # BaseMcpTransport(transports.py)
    capabilities: dict[str, Any] = field(default_factory=dict)  # initialize 返回
    server_info: dict[str, Any] | None = None
    instructions: str | None = None  # 服务器 instructions(截断 2048 后保存,§7.1)
    tools: list[dict[str, Any]] = field(default_factory=list)  # 原始 tools/list 结果
    resources: list[dict[str, Any]] = field(default_factory=list)
    prompts: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None  # FAILED 时的错误信息
    cleanup: Callable[[], Any] | None = None
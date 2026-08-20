"""MCP 配置层(spec §5):发现来源、解析校验、内容去重、政策过滤、增删改查。

配置告诉 CodeSage 有哪些 MCP 服务器:存在 6 个作用域,优先级 builtin > enterprise >
local > project > user > dynamic(spec §5.1)。本模块只读配置文件与设置项,不连接服务器。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from ..config import atomic, paths
from .types import ConfigScope, McpJsonConfig, ScopedMcpServerConfig

#: 内置安装状态文件(记录已安装的内置托管服务器,spec §4.6)
INSTALLED_FILE = "installed.json"

#: 内置配置目录下的二进制安装根
BIN_DIR = "bin"

#: 内置 opt-in 型(默认禁用)服务器列表的 settings 键
DISABLED_MCP_SERVERS_KEY = "disabledMcpServers"
ENABLED_MCP_SERVERS_KEY = "enabledMcpServers"


def expand_env_vars_in_string(value: str) -> tuple[str, list[str]]:
    """展开字符串里的 ${VAR} 与 ${VAR:-default};返回展开结果与缺失变量列表。

    spec §5.2:缺变量保留原样(便于调试)并上报 warning 级错误。
    """
    missing: list[str] = []

    def _repl(match: re.Match[str]) -> str:
        var_content = match.group(1)
        var_name, sep, default = var_content.partition(":-")
        env_value = os.environ.get(var_name)
        if env_value is not None:
            return env_value
        if sep:
            return default
        missing.append(var_name)
        return match.group(0)

    expanded = re.sub(r"\$\{([^}]+)\}", _repl, value)
    return expanded, list(dict.fromkeys(missing))

def expand_env_vars(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """按字段展开配置里的环境变量(命令/参数/env/url/headers)。"""
    missing: list[str] = []

    def _exp(value: Any) -> Any:
        if isinstance(value, str):
            out, miss = expand_env_vars_in_string(value)
            missing.extend(miss)
            return out
        if isinstance(value, list):
            return [_exp(v) for v in value]
        if isinstance(value, dict):
            return {k: _exp(v) for k, v in value.items()}
        return value

    expanded = {k: _exp(v) for k, v in config.items()}
    return expanded, list(dict.fromkeys(missing))


def _is_npx_command(config: dict[str, Any]) -> bool:
    """Windows 裸 npx 检查(spec §5.2):需要 cmd /c 包装否则 warning。"""
    cmd = config.get("command")
    return isinstance(cmd, str) and (cmd == "npx" or cmd.endswith("\\npx") or cmd.endswith("/npx"))


def parse_mcp_config(
    config_object: Any,
    *,
    expand_vars: bool,
    scope: ConfigScope,
    file_path: str | None = None,
) -> tuple[McpJsonConfig | None, list[Any]]:
    """解析并校验一个 MCP 配置对象(通常来自 .mcp.json 或 settings.mcpServers)。

    返回 (校验后的配置, 错误列表);文件缺失/非 JSON 由调用方处理(file_path 为空时)。
    """
    errors: list[Any] = []

    if not isinstance(config_object, dict) or not isinstance(config_object.get("mcpServers"), dict):
        return None, [
            {
                **({"file": file_path} if file_path else {}),
                "path": "mcpServers",
                "message": "Does not adhere to MCP server configuration schema",
                "mcpErrorMetadata": {"scope": scope.value, "severity": "fatal"},
            }
        ]

    validated: dict[str, Any] = {}
    for name, raw in config_object["mcpServers"].items():
        if isinstance(raw, BaseModel):
            raw = raw.model_dump()  # 模型转回 dict 再逐字段展开
        if not isinstance(raw, dict):
            errors.append(
                {
                    **({"file": file_path} if file_path else {}),
                    "path": f"mcpServers.{name}",
                    "message": f"Invalid MCP server configuration for {name}",
                    "mcpErrorMetadata": {"scope": scope.value, "severity": "fatal"},
                }
            )
            continue
        if expand_vars:
            raw, missing = expand_env_vars(raw)
            if missing:
                errors.append(
                    {
                        **({"file": file_path} if file_path else {}),
                        "path": f"mcpServers.{name}",
                        "message": f"Missing environment variables: {', '.join(missing)}",
                        "suggestion": f"Set the following environment variables: {', '.join(missing)}",
                        "mcpErrorMetadata": {"scope": scope.value, "severity": "warning"},
                    }
                )
        if scope == ConfigScope.PROJECT and _is_npx_command(raw):
            errors.append(
                {
                    **({"file": file_path} if file_path else {}),
                    "path": f"mcpServers.{name}",
                    "message": "Windows requires 'cmd /c' wrapper to execute npx",
                    "suggestion": 'Change command to "cmd" with args ["/c", "npx", ...]',
                    "mcpErrorMetadata": {"scope": scope.value, "severity": "warning"},
                }
            )
        try:
            validated[name] = ScopedMcpServerConfig(name=name, scope=scope, **raw)
        except (ValidationError, TypeError) as e:
            errors.append(
                {
                    **({"file": file_path} if file_path else {}),
                    "path": f"mcpServers.{name}",
                    "message": f"Invalid MCP server configuration: {e}",
                    "mcpErrorMetadata": {"scope": scope.value, "severity": "fatal"},
                }
            )

    return McpJsonConfig(mcpServers=validated), errors


def parse_mcp_config_from_file(
    file_path: Path,
    *,
    expand_vars: bool,
    scope: ConfigScope,
) -> tuple[McpJsonConfig | None, list[Any]]:
    """从文件解析 .mcp.json;文件缺失/非 JSON 返回错误列表。"""
    try:
        content = file_path.read_text(encoding="utf-8-sig")
        data = json.loads(content)
        if not isinstance(data, dict):
            return None, [
                {
                    "file": str(file_path),
                    "path": "",
                    "message": "MCP config is not a valid JSON object",
                    "mcpErrorMetadata": {"scope": scope.value, "severity": "fatal"},
                }
            ]
        return parse_mcp_config(data, expand_vars=expand_vars, scope=scope, file_path=str(file_path))
    except FileNotFoundError:
        return None, [
            {
                "file": str(file_path),
                "path": "",
                "message": f"MCP config file not found: {file_path}",
                "suggestion": "Check that the file path is correct",
                "mcpErrorMetadata": {"scope": scope.value, "severity": "fatal"},
            }
        ]
    except (ValueError, OSError) as e:
        return None, [
            {
                "file": str(file_path),
                "path": "",
                "message": f"Failed to read file: {e}",
                "suggestion": "Check file permissions and ensure the file exists",
                "mcpErrorMetadata": {"scope": scope.value, "severity": "fatal"},
            }
        ]


def _add_scope_to_servers(
    servers: dict[str, Any] | None, scope: ConfigScope
) -> dict[str, ScopedMcpServerConfig]:
    """给原始配置打 scope 标签(内部工具;调用方保证 servers 结构正确)。"""
    from pydantic import BaseModel

    out: dict[str, ScopedMcpServerConfig] = {}
    for name, raw in (servers or {}).items():
        if isinstance(raw, ScopedMcpServerConfig):
            out[name] = raw  # 已带 scope,直接复用
        else:
            raw = raw.model_dump() if isinstance(raw, BaseModel) else dict(raw)
            out[name] = ScopedMcpServerConfig(name=name, scope=scope, **raw)
    return out


def get_enterprise_mcp_file_path() -> Path:
    """企业托管 MCP 配置文件路径(managed_dir;spec §5.1)。"""
    return paths.config_dir() / "managed-mcp.json"

def does_enterprise_mcp_config_exist() -> bool:
    """企业配置存在即独占:其余作用域全部忽略(spec §5.4)。"""
    return get_enterprise_mcp_file_path().exists()

def get_builtin_mcp_configs() -> dict[str, ScopedMcpServerConfig]:
    """内置托管服务器配置(只读推导层,spec §4.6/§5.1)。

    从注册表 + 已安装状态合成,不落用户文件。注册表见 mcp/builtin/registry.py。
    """
    from .builtin.registry import iter_bundled_servers

    installed = _read_installed()
    out: dict[str, ScopedMcpServerConfig] = {}
    for spec in iter_bundled_servers():
        if spec.name not in installed:
            continue
        cfg = dict(spec.default_config)
        cfg.setdefault("command", str(installed[spec.name]["binary"]))
        out[spec.name] = ScopedMcpServerConfig(name=spec.name, scope=ConfigScope.BUILTIN, **cfg)
    return out

def _read_installed() -> dict[str, dict[str, Any]]:
    """读取已安装的内置托管服务器记录({config_dir}/mcp/installed.json)。"""
    return atomic.read_json_lossy(paths.config_dir() / "mcp" / INSTALLED_FILE, {})

def write_installed(installed: dict[str, dict[str, Any]]) -> None:
    """写入已安装记录(原子写)。"""
    atomic.atomic_write(paths.config_dir() / "mcp" / INSTALLED_FILE, json.dumps(installed, indent=2))

def installed_bin_dir(name: str) -> Path:
    """某内置服务器的二进制安装目录({config_dir}/mcp/bin/{name}/)。"""
    return paths.config_dir() / "mcp" / BIN_DIR / name

def get_mcp_configs_by_scope(scope: ConfigScope) -> dict[str, ScopedMcpServerConfig]:
    """读取一个作用域的配置(project 沿目录向上遍历合并,越近越优先)。"""
    if scope == ConfigScope.BUILTIN:
        return get_builtin_mcp_configs()
    if scope == ConfigScope.ENTERPRISE:
        config, _ = parse_mcp_config_from_file(
            get_enterprise_mcp_file_path(), expand_vars=True, scope=scope
        )
        return _add_scope_to_servers(config.mcpServers if config else None, scope) if config else {}
    if scope == ConfigScope.PROJECT:
        merged: dict[str, ScopedMcpServerConfig] = {}
        cwd = paths.cwd()
        for parent in (cwd, *cwd.parents):
            p = parent / ".mcp.json"
            if not p.exists():
                continue
            config, _ = parse_mcp_config_from_file(p, expand_vars=True, scope=scope)
            if config and config.mcpServers:
                merged.update(_add_scope_to_servers(config.mcpServers, scope))
            if p.parent == p:  # 到达文件系统根
                break
        return merged
    if scope == ConfigScope.LOCAL:
        from ..config import settings

        return _add_scope_to_servers(settings.load_settings().mcp_servers, scope)
    if scope == ConfigScope.USER:
        from ..config import global_config

        return _add_scope_to_servers(global_config.GlobalConfig.load().mcp_servers, scope)
    raise ValueError(f"unsupported scope for get: {scope}")

def get_mcp_config_by_name(name: str) -> ScopedMcpServerConfig | None:
    """按名查配置:enterprise 独占时仅企业配置可达(spec §5.1 优先级顺序)。"""
    if does_enterprise_mcp_config_exist():
        return get_mcp_configs_by_scope(ConfigScope.ENTERPRISE).get(name)
    # 优先级:builtin > local > project > user > dynamic(§5.1;builtin 恒不可被用户配置覆盖)
    for scope in (ConfigScope.BUILTIN, ConfigScope.LOCAL, ConfigScope.PROJECT, ConfigScope.USER):
        found = get_mcp_configs_by_scope(scope).get(name)
        if found:
            return found
    return None

def _merge_with_dedup(
    layers: list[dict[str, ScopedMcpServerConfig]],
) -> tuple[dict[str, ScopedMcpServerConfig], list[tuple[str, str]]]:
    """按优先级从低到高合并,内容去重:同签名只保留最高层(spec §5.3)。"""
    merged: dict[str, ScopedMcpServerConfig] = {}
    suppressed: list[tuple[str, str]] = []
    seen_signatures: dict[str, str] = {}
    for layer in layers:
        for name, cfg in layer.items():
            sig = cfg.signature()
            if sig is None:
                merged[name] = cfg
                continue
            owner = seen_signatures.get(sig)
            if owner is not None:
                suppressed.append((name, owner))  # 低层同名被抑制(手动配置优先)
                continue
            seen_signatures[sig] = name
            merged[name] = cfg
    return merged, suppressed

def get_all_mcp_configs() -> dict[str, ScopedMcpServerConfig]:
    """全层合并(spec §5.1/§5.3):builtin > enterprise > local > project > user > dynamic。"""
    if does_enterprise_mcp_config_exist():
        configs = get_mcp_configs_by_scope(ConfigScope.ENTERPRISE)
        return {k: v for k, v in configs.items() if is_mcp_server_allowed_by_policy(k, v)}

    layers = [
        get_mcp_configs_by_scope(ConfigScope.DYNAMIC),  # 最低
        get_mcp_configs_by_scope(ConfigScope.USER),
        get_mcp_configs_by_scope(ConfigScope.PROJECT),
        get_mcp_configs_by_scope(ConfigScope.LOCAL),
        get_mcp_configs_by_scope(ConfigScope.ENTERPRISE),
        get_builtin_mcp_configs(),  # 最高
    ]
    merged, _suppressed = _merge_with_dedup(layers)
    return {k: v for k, v in merged.items() if is_mcp_server_allowed_by_policy(k, v)}

def is_mcp_server_denied(name: str, config: ScopedMcpServerConfig) -> bool:
    """名称/命令/URL 三路 deny(spec §5.4)。"""
    from ..config import settings

    settings_obj = settings.load_settings()
    denied = getattr(settings_obj, "deniedMcpServers", None)
    if not denied:
        return False
    if name in denied:
        return True
    if config.command and config.command in denied:
        return True
    if config.url and config.url in denied:
        return True
    return False

def is_mcp_server_allowed_by_policy(name: str, config: ScopedMcpServerConfig) -> bool:
    """政策过滤(spec §5.4):deny 绝对优先(连 allowlist 也不能解禁)。"""
    if is_mcp_server_denied(name, config):
        return False
    from ..config import settings

    settings_obj = settings.load_settings()
    allowed = getattr(settings_obj, "allowedMcpServers", None)
    if allowed is None:
        return True
    if len(allowed) == 0:
        return False  # 空 allowlist = 全禁
    return name in allowed or (config.command is not None and config.command in allowed) or (config.url is not None and config.url in allowed)

def add_mcp_config(name: str, config: dict[str, Any], scope: ConfigScope) -> None:
    """新增一个服务器(spec §5.5)。

    校验名字/政策/重复后写入对应作用域。enterprise 存在即拒绝(独占权)。
    """
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        raise ValueError("Invalid name {name}. Names can only contain letters, numbers, hyphens, and underscores.")
    if does_enterprise_mcp_config_exist():
        raise PermissionError("enterprise MCP configuration is active and has exclusive control over MCP servers")

    validated, errors = parse_mcp_config({"mcpServers": {name: config}}, expand_vars=True, scope=scope)
    if errors:
        raise ValueError(errors[0]["message"])
    cfg = validated.mcpServers[name] if validated else config

    if is_mcp_server_denied(name, cfg):
        raise PermissionError(f'Cannot add MCP server "{name}": blocked by policy')

    if scope == ConfigScope.PROJECT:
        existing = get_mcp_configs_by_scope(scope)
        if name in existing:
            raise ValueError(f"MCP server {name} already exists in .mcp.json")
        payload = {k: v.model_dump(exclude_none=True) for k, v in existing.items() if k != name}
        payload[name] = cfg.model_dump(exclude_none=True)
        write_mcp_json(payload)
    elif scope == ConfigScope.USER:
        from ..config import global_config

        gc = global_config.GlobalConfig.load()
        servers = dict(gc.model_dump().get("mcpServers") or {})
        servers[name] = cfg.model_dump(exclude_none=True)
        gc.mcp_servers = servers
        gc.save()
    elif scope == ConfigScope.LOCAL:
        from ..config import settings

        def _add(cur):
            servers = dict(cur.get("mcpServers") or {})
            servers[name] = cfg.model_dump(exclude_none=True)
            return {**cur, "mcpServers": servers}

        settings.save_settings(_add)
    else:
        raise ValueError(f"Cannot add MCP server to scope: {scope}")

def write_mcp_json(mcp_servers: dict[str, Any]) -> None:
    """写 .mcp.json(原子写,保留权限,spec §5.5)。"""
    atomic.atomic_write(paths.cwd() / ".mcp.json", json.dumps({"mcpServers": mcp_servers}, indent=2))

def remove_mcp_config(name: str, scope: ConfigScope) -> None:
    """移除一个服务器(enterprise 不可手动移除)。"""
    existing = get_mcp_configs_by_scope(scope)
    if name not in existing:
        raise KeyError(f'No MCP server found with name: {name} in scope {scope}')
    if scope == ConfigScope.PROJECT:
        payload = {k: v.model_dump(exclude_none=True) for k, v in existing.items() if k != name}
        write_mcp_json(payload)
    elif scope == ConfigScope.USER:
        from ..config import global_config

        gc = global_config.GlobalConfig.load()
        servers = dict(gc.model_dump().get("mcpServers") or {})
        servers.pop(name, None)
        gc.mcp_servers = servers
        gc.save()
    elif scope == ConfigScope.LOCAL:
        from ..config import settings

        def _remove(cur):
            servers = dict(cur.get("mcpServers") or {})
            servers.pop(name, None)
            return {**cur, "mcpServers": servers}

        settings.save_settings(_remove)
    else:
        raise ValueError(f"Cannot remove MCP server from scope: {scope}")

def is_mcp_server_disabled(name: str) -> bool:
    """是否禁用(spec §6.5):用户显式禁用或内置 opt-in 未启用。"""
    from ..config import settings

    settings_obj = settings.load_settings()
    if name in (getattr(settings_obj, "disabledMcpServers", None) or []):
        return True
    if name in (getattr(settings_obj, "enabledMcpServers", None) or []):
        return False
    return name in get_builtin_mcp_configs()  # 内置 opt-in 型:未显式启用则禁用

def set_mcp_server_enabled(name: str, enabled: bool) -> None:
    """启用/禁用(spec §6.5):写 local settings 的 disabled/enabled 列表。"""
    from ..config import settings

    def _toggle(cur):
        disabled = list(cur.get("disabledMcpServers") or [])
        enabled_list = list(cur.get("enabledMcpServers") or [])
        if enabled:
            disabled = [n for n in disabled if n != name]
            if name not in enabled_list:
                enabled_list.append(name)
        else:
            enabled_list = [n for n in enabled_list if n != name]
            if name not in disabled:
                disabled.append(name)
        return {**cur, "disabledMcpServers": disabled, "enabledMcpServers": enabled_list}

    settings.save_settings(_toggle)
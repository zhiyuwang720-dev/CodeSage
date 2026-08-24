"""包内配置:模型指针与 profile 的单一配置源,从 codesage 转移。

配置来自用户级 ~/.codesage/config.json(CODESAGE_CONFIG_DIR 可
覆盖),形状与旧 codesage.config 完全一致 —— 本包自包含,不再
引用旧代码。三个源文件合并于此:全局配置模型、路径、原子写;
settings 三层与 agents_md 属于其他包的职责,未随迁。
"""

from __future__ import annotations

import errno
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("llm.config")

#: 数据根:全局配置与状态所在目录(~/.codesage 缺省,环境变量覆盖)。
DEFAULT_CONFIG_DIR = Path.home() / ".codesage"

GLOBAL_CONFIG_FILENAME = "config.json"


def config_dir() -> Path:
    """数据根目录(配置、会话、记忆都在这下面)。"""
    override = os.getenv("CODESAGE_CONFIG_DIR")
    return Path(override).expanduser() if override else DEFAULT_CONFIG_DIR


def global_config_path() -> Path:
    return config_dir() / GLOBAL_CONFIG_FILENAME


def atomic_write(path: Path | str, content: str | bytes) -> None:
    """原子写:同目录临时文件 + os.replace + fsync。

    tmp+rename 是整套 harness 的持久化骨干(设计不变量):读者
    永远看不到写了一半的文件。符号链接先解析(chezmoi/stow 的
    链接文件替换目标而非链接),已有文件的权限位保留,失败清理
    临时文件,调用方决定是否降级。
    """
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o7777
    except OSError:
        mode = None  # 新文件:保持 mkstemp 的 0600 缺省
    data = content.encode("utf-8") if isinstance(content, str) else content
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.replace(tmp_name, path)
        except PermissionError:
            # Windows:目标被短暂锁住(杀毒扫描/编辑器)时 replace 失败,
            # 先删目标再试一次
            if path.exists():
                os.unlink(path)
            os.replace(tmp_name, path)
        if mode is not None:
            os.chmod(path, mode)
    except BaseException:
        # 失败绝不留下游离临时文件
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_json_lossy(path: Path | str, default: dict) -> dict:
    """读 JSON;缺失或损坏返回 default(BOM 容错)。"""
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except (OSError, ValueError):
        return default


class ProjectConfig(BaseModel):
    """按项目绝对路径索引的项目条目。"""

    model_config = ConfigDict(extra="allow")

    allowed_tools: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class GlobalConfig(BaseModel):
    """顶层全局配置:模型 profile 与指针是 llm 关心的部分。"""

    model_config = ConfigDict(extra="allow")

    theme: Optional[str] = None
    model_profiles: dict[str, Any] = Field(default_factory=dict)
    model_pointers: dict[str, str] = Field(
        default_factory=lambda: {"main": "main", "task": "task", "compact": "compact", "quick": "quick"}
    )
    mcp_servers: dict[str, Any] = Field(default_factory=dict)
    projects: dict[str, ProjectConfig] = Field(default_factory=dict)

    def project(self, project_path: str) -> ProjectConfig:
        """取或建某个项目路径的条目。"""
        return self.projects.setdefault(project_path, ProjectConfig())

    @classmethod
    def load(cls) -> "GlobalConfig":
        """从磁盘加载;缺失/损坏降级为缺省,绝不因配置崩溃启动。"""
        data = read_json_lossy(global_config_path(), {})
        try:
            return cls(**data)
        except Exception:  # pydantic 校验失败;降级,不致命
            return cls()

    def save(self) -> None:
        """原子持久化;权限/只读错误只告警不抛(只读 HOME 不能崩 CLI)。"""
        try:
            atomic_write(global_config_path(), self.model_dump_json(indent=2) + "\n")
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
                logger.warning("cannot save config %s: %s", global_config_path(), exc)
            else:
                raise

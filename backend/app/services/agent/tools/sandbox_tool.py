"""
沙箱管理器(06-P1: 仅保留 SandboxConfig + SandboxManager; legacy AgentTool 子类退役)。
Docker 沙箱命令执行由 SandboxManager.execute_command 提供; 删除的 AgentTool 类:
SandboxTool/SandboxHttpTool/VulnerabilityVerifyTool/PhpTestTool/CommandInjectionTestTool。
"""

import asyncio
import io
import logging
import tempfile
import os
import ntpath
import posixpath
import socket
import tarfile
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

from app.core.config import settings

logger = logging.getLogger(__name__)

TOOL_STDOUT_CAPTURE_LIMIT = 5 * 1024 * 1024


@dataclass
class SandboxConfig:
    """沙箱配置"""
    image: str = None  # 默认从 settings.SANDBOX_IMAGE 读取
    memory_limit: str = "512m"
    cpu_limit: float = 1.0
    timeout: int = 60
    network_mode: str = "none"  # none, bridge, host
    read_only: bool = True
    user: str = "1000:1000"
    cap_drop: list = None  # 丢弃的 Linux 能力列表
    no_new_privileges: bool = True  # 禁止提权

    def __post_init__(self):
        if self.image is None:
            self.image = settings.SANDBOX_IMAGE
        if self.cap_drop is None:
            cap_drop_str = getattr(settings, 'SANDBOX_CAP_DROP', 'ALL')
            if cap_drop_str.upper() == 'NONE':
                self.cap_drop = []
            else:
                self.cap_drop = [c.strip() for c in cap_drop_str.split(',') if c.strip()]
        self.no_new_privileges = getattr(settings, 'SANDBOX_NO_NEW_PRIVILEGES', True)


class SandboxManager:
    """
    沙箱管理器
    管理 Docker 容器的创建、执行和清理
    """
    
    def __init__(self, config: Optional[SandboxConfig] = None):
        self.config = config or SandboxConfig()
        self._docker_client = None
        self._initialized = False
        self._init_error = None
    
    async def initialize(self):
        """初始化 Docker 客户端"""
        if self._initialized:
            logger.info("✅ SandboxManager already initialized")
            return

        try:
            import docker
            logger.info(f"🔄 Attempting to connect to Docker... (lib: {docker.__file__})")
            self._docker_client = docker.from_env()
            # 测试连接
            self._docker_client.ping()
            self._initialized = True
            self._init_error = None
            logger.info("✅ Docker sandbox manager initialized successfully")
        except ImportError as e:
            logger.error(f"❌ Docker library not installed: {e}")
            self._docker_client = None
            self._init_error = f"ImportError: {e}"
        except Exception as e:
            logger.warning(f"❌ Docker not available: {e}")
            import traceback
            logger.warning(f"Docker connection traceback: {traceback.format_exc()}")
            self._docker_client = None
            self._init_error = f"{type(e).__name__}: {str(e)}"
    
    @property
    def is_available(self) -> bool:
        """检查 Docker 是否可用"""
        return self._docker_client is not None
        
    def get_diagnosis(self) -> str:
        """获取诊断信息"""
        if self.is_available:
            return "Docker Service Available"
        return f"Docker Service Unavailable. Error: {self._init_error or 'Not initialized'}"
    
    async def execute_command(
        self,
        command: str,
        working_dir: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        在沙箱中执行命令
        
        Args:
            command: 要执行的命令
            working_dir: 工作目录
            env: 环境变量
            timeout: 超时时间（秒）
            
        Returns:
            执行结果
        """
        if not self.is_available:
            return {
                "success": False,
                "error": "Docker 不可用",
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
            }

        timeout = timeout or self.config.timeout

        # 禁用代理环境变量，防止 Docker 自动注入的代理干扰容器网络
        no_proxy_env = {
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "http_proxy": "",
            "https_proxy": "",
            "NO_PROXY": "*",
            "no_proxy": "*",
        }
        # 合并用户传入的环境变量（用户变量优先）
        container_env = {**no_proxy_env, **(env or {})}

        try:
            # 创建临时目录
            with tempfile.TemporaryDirectory() as temp_dir:
                # 准备容器配置
                container_config = {
                    "image": self.config.image,
                    "command": ["sh", "-c", command],
                    "detach": True,
                    "mem_limit": self.config.memory_limit,
                    "cpu_period": 100000,
                    "cpu_quota": int(100000 * self.config.cpu_limit),
                    "network_mode": self.config.network_mode,
                    "user": self.config.user,
                    "read_only": self.config.read_only,
                    "volumes": {
                        temp_dir: {"bind": "/workspace", "mode": "rw"},
                    },
                    "tmpfs": {
                            "/home/sandbox": "rw,size=100m,mode=1777",
                            "/tmp": "rw,size=100m,mode=1777"
                        },
                    "working_dir": working_dir or "/workspace",
                    "environment": container_env,
                }

                # 安全配置：可通过环境变量调整
                if self.config.cap_drop:
                    container_config["cap_drop"] = self.config.cap_drop
                if self.config.no_new_privileges:
                    container_config["security_opt"] = ["no-new-privileges:true"]
                
                # 创建并启动容器
                container = await asyncio.to_thread(
                    self._docker_client.containers.run,
                    **container_config
                )
                
                try:
                    # 等待执行完成
                    result = await asyncio.wait_for(
                        asyncio.to_thread(container.wait),
                        timeout=timeout
                    )
                    
                    # 获取日志
                    stdout = await asyncio.to_thread(
                        container.logs, stdout=True, stderr=False
                    )
                    stderr = await asyncio.to_thread(
                        container.logs, stdout=False, stderr=True
                    )
                    
                    return {
                        "success": result["StatusCode"] == 0,
                        "stdout": stdout.decode('utf-8', errors='ignore')[:10000],
                        "stderr": stderr.decode('utf-8', errors='ignore')[:2000],
                        "exit_code": result["StatusCode"],
                        "error": None,
                    }
                    
                except asyncio.TimeoutError:
                    await asyncio.to_thread(container.kill)
                    return {
                        "success": False,
                        "error": f"执行超时 ({timeout}秒)",
                        "stdout": "",
                        "stderr": "",
                        "exit_code": -1,
                    }
                    
                finally:
                    # 清理容器
                    await asyncio.to_thread(container.remove, force=True)
                    
        except Exception as e:
            logger.error(f"Sandbox execution error: {e}")
            return {
                "success": False,
                "error": str(e),
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
            }
    
    async def execute_tool_command(
        self,
        command: str,
        host_workdir: str,
        timeout: Optional[int] = None,
        env: Optional[Dict[str, str]] = None,
        network_mode: str = "none",
        artifact_paths: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        在沙箱中对指定目录执行工具命令
        
        Args:
            command: 要执行的命令
            host_workdir: 宿主机上的工作目录（将被挂载到 /workspace）
            timeout: 超时时间
            env: 环境变量
            network_mode: 网络模式 (none, bridge, host)
            
        Returns:
            执行结果
        """
        if not self.is_available:
            return {
                "success": False,
                "error": "Docker 不可用",
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
                "artifacts": {},
            }
        
        timeout = timeout or self.config.timeout

        # 禁用代理环境变量，防止 Docker 自动注入的代理干扰容器网络
        no_proxy_env = {
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "http_proxy": "",
            "https_proxy": "",
            "NO_PROXY": "*",
            "no_proxy": "*",
        }
        # 合并用户传入的环境变量（用户变量优先）
        container_env = {**no_proxy_env, **(env or {})}

        try:
            # 清除代理环境变量：在命令前添加 unset（双重保险）
            unset_proxy_prefix = "unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy 2>/dev/null; "
            wrapped_command = unset_proxy_prefix + command
            docker_host_workdir = self._resolve_docker_host_workdir(host_workdir, docker_client=self._docker_client)
            if docker_host_workdir != host_workdir:
                logger.info("Mapped sandbox workdir for Docker bind mount: %s -> %s", host_workdir, docker_host_workdir)

            # 准备容器配置
            container_config = {
                "image": self.config.image,
                "command": ["sh", "-c", wrapped_command],
                "detach": True,
                "mem_limit": self.config.memory_limit,
                "cpu_period": 100000,
                "cpu_quota": int(100000 * self.config.cpu_limit),
                "network_mode": network_mode,
                "user": self.config.user,
                "read_only": self.config.read_only,
                "volumes": {
                    docker_host_workdir: {"bind": "/workspace", "mode": "ro"}, # 只读挂载项目代码
                },
                "tmpfs": {
                    "/home/sandbox": "rw,size=100m,mode=1777",
                    "/tmp": "rw,size=100m,mode=1777"  # 添加 /tmp 目录供工具写入临时文件
                },
                "working_dir": "/workspace",
                "environment": container_env,
            }

            # 安全配置：可通过环境变量调整
            if self.config.cap_drop:
                container_config["cap_drop"] = self.config.cap_drop
            if self.config.no_new_privileges:
                container_config["security_opt"] = ["no-new-privileges:true"]
            
            # 创建并启动容器
            container = await asyncio.to_thread(
                self._docker_client.containers.run,
                **container_config
            )
            
            try:
                # 等待执行完成
                result = await asyncio.wait_for(
                    asyncio.to_thread(container.wait),
                    timeout=timeout
                )
                
                # 获取日志
                stdout = await asyncio.to_thread(
                    container.logs, stdout=True, stderr=False
                )
                stderr = await asyncio.to_thread(
                    container.logs, stdout=False, stderr=True
                )
                artifacts = await self._read_container_artifacts(container, artifact_paths or [])

                return {
                    "success": result["StatusCode"] == 0,
                    "stdout": stdout.decode('utf-8', errors='ignore')[:TOOL_STDOUT_CAPTURE_LIMIT],
                    "stderr": stderr.decode('utf-8', errors='ignore')[:5000],
                    "exit_code": result["StatusCode"],
                    "error": None,
                    "artifacts": artifacts,
                    "host_workdir": host_workdir,
                    "docker_host_workdir": docker_host_workdir,
                }
                
            except asyncio.TimeoutError:
                await asyncio.to_thread(container.kill)
                return {
                    "success": False,
                    "error": f"执行超时 ({timeout}秒)",
                    "stdout": "",
                    "stderr": "",
                    "exit_code": -1,
                    "artifacts": {},
                }
                
            finally:
                # 清理容器
                await asyncio.to_thread(container.remove, force=True)
                
        except Exception as e:
            logger.error(f"Tool execution error: {e}")
            return {
                "success": False,
                "error": str(e),
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
                "artifacts": {},
            }

    @staticmethod
    def _resolve_docker_host_workdir(host_workdir: str, docker_client: Any = None) -> str:
        candidate = os.path.abspath(str(host_workdir or ""))
        mappings: List[tuple[str, str]] = []

        host_project_root = os.getenv("HOST_PROJECT_ROOT", "").strip().rstrip("/\\")
        if host_project_root:
            mappings.append((os.path.abspath(settings.MANAGED_PROJECTS_ROOT), host_project_root))

        if docker_client is not None:
            mappings.extend(SandboxManager._current_container_mount_mappings(docker_client))

        for container_root, docker_host_root in sorted(mappings, key=lambda item: len(item[0]), reverse=True):
            normalized_container_root = os.path.abspath(container_root)
            if not SandboxManager._is_path_within(candidate, normalized_container_root):
                continue
            relative_path = os.path.relpath(candidate, normalized_container_root)
            if relative_path == ".":
                relative_path = ""
            return SandboxManager._join_docker_host_path(docker_host_root, relative_path)

        return host_workdir

    @staticmethod
    def _current_container_mount_mappings(docker_client: Any) -> List[tuple[str, str]]:
        try:
            container = docker_client.containers.get(socket.gethostname())
            mounts = container.attrs.get("Mounts") or []
        except Exception:
            logger.debug("Unable to inspect current container mounts for sandbox workdir mapping", exc_info=True)
            return []

        mappings: List[tuple[str, str]] = []
        for mount in mounts:
            destination = str(mount.get("Destination") or "").strip()
            source = str(mount.get("Source") or "").strip()
            if destination and source:
                mappings.append((destination, source))
        return mappings

    @staticmethod
    def _is_path_within(candidate: str, root: str) -> bool:
        try:
            return os.path.commonpath([candidate, root]) == root
        except ValueError:
            return False

    @staticmethod
    def _join_docker_host_path(root: str, relative_path: str) -> str:
        cleaned_root = str(root or "").rstrip("/\\")
        cleaned_relative = str(relative_path or "").replace("\\", "/").strip("/")
        if not cleaned_relative:
            return cleaned_root
        if "\\" in cleaned_root or (len(cleaned_root) >= 2 and cleaned_root[1] == ":"):
            return ntpath.normpath(ntpath.join(cleaned_root, *cleaned_relative.split("/")))
        return posixpath.normpath(posixpath.join(cleaned_root, cleaned_relative))

    async def _read_container_artifacts(self, container, artifact_paths: List[str]) -> Dict[str, Dict[str, Any]]:
        artifacts: Dict[str, Dict[str, Any]] = {}
        for artifact_path in artifact_paths:
            normalized_path = str(artifact_path or "").strip()
            if not normalized_path:
                continue
            try:
                content = await asyncio.to_thread(self._read_container_file, container, normalized_path)
                artifacts[normalized_path] = {
                    "content": content.decode("utf-8", errors="replace"),
                    "bytes": len(content),
                    "encoding": "utf-8",
                }
            except Exception as exc:
                artifacts[normalized_path] = {
                    "content": "",
                    "bytes": 0,
                    "encoding": "utf-8",
                    "error": str(exc),
                }
        return artifacts

    @staticmethod
    def _read_container_file(container, artifact_path: str) -> bytes:
        stream, _stat = container.get_archive(artifact_path)
        archive_bytes = b"".join(stream)
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as archive:
            for member in archive.getmembers():
                extracted = archive.extractfile(member)
                if extracted is not None:
                    return extracted.read()
        return b""

    async def execute_python(
        self,
        code: str,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        在沙箱中执行 Python 代码
        
        Args:
            code: Python 代码
            timeout: 超时时间
            
        Returns:
            执行结果
        """
        # 转义代码中的单引号
        escaped_code = code.replace("'", "'\\''")
        command = f"python3 -c '{escaped_code}'"
        return await self.execute_command(command, timeout=timeout)
    
    async def execute_http_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[str] = None,
        timeout: int = 30,
    ) -> Dict[str, Any]:
        """
        在沙箱中执行 HTTP 请求
        
        Args:
            method: HTTP 方法
            url: URL
            headers: 请求头
            data: 请求体
            timeout: 超时
            
        Returns:
            HTTP 响应
        """
        # 构建 curl 命令
        curl_parts = ["curl", "-s", "-S", "-w", "'\\n%{http_code}'", "-X", method]
        
        if headers:
            for key, value in headers.items():
                curl_parts.extend(["-H", f"'{key}: {value}'"])
        
        if data:
            curl_parts.extend(["-d", f"'{data}'"])
        
        curl_parts.append(f"'{url}'")
        
        command = " ".join(curl_parts)
        
        # 使用带网络的镜像
        original_network = self.config.network_mode
        self.config.network_mode = "bridge"  # 允许网络访问
        
        try:
            result = await self.execute_command(command, timeout=timeout)
            
            if result["success"] and result["stdout"]:
                lines = result["stdout"].strip().split('\n')
                if lines:
                    status_code = lines[-1].strip()
                    body = '\n'.join(lines[:-1])
                    return {
                        "success": True,
                        "status_code": int(status_code) if status_code.isdigit() else 0,
                        "body": body[:5000],
                        "error": None,
                    }
            
            return {
                "success": False,
                "status_code": 0,
                "body": "",
                "error": result.get("error") or result.get("stderr"),
            }
            
        finally:
            self.config.network_mode = original_network
    
    async def verify_vulnerability(
        self,
        vulnerability_type: str,
        target_url: str,
        payload: str,
        expected_pattern: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        验证漏洞
        
        Args:
            vulnerability_type: 漏洞类型
            target_url: 目标 URL
            payload: 攻击载荷
            expected_pattern: 期望在响应中匹配的模式
            
        Returns:
            验证结果
        """
        verification_result = {
            "vulnerability_type": vulnerability_type,
            "target_url": target_url,
            "payload": payload,
            "is_vulnerable": False,
            "evidence": None,
            "error": None,
        }
        
        try:
            # 发送请求
            response = await self.execute_http_request(
                method="GET" if "?" in target_url else "POST",
                url=target_url,
                data=payload if "?" not in target_url else None,
            )
            
            if not response["success"]:
                verification_result["error"] = response.get("error")
                return verification_result
            
            body = response.get("body", "")
            status_code = response.get("status_code", 0)
            
            # 检查响应
            if expected_pattern:
                import re
                if re.search(expected_pattern, body, re.IGNORECASE):
                    verification_result["is_vulnerable"] = True
                    verification_result["evidence"] = f"响应中包含预期模式: {expected_pattern}"
            else:
                # 根据漏洞类型进行通用检查
                if vulnerability_type == "sql_injection":
                    error_patterns = [
                        r"SQL syntax",
                        r"mysql_fetch",
                        r"ORA-\d+",
                        r"PostgreSQL.*ERROR",
                        r"SQLite.*error",
                        r"ODBC.*Driver",
                    ]
                    for pattern in error_patterns:
                        if re.search(pattern, body, re.IGNORECASE):
                            verification_result["is_vulnerable"] = True
                            verification_result["evidence"] = f"SQL错误信息: {pattern}"
                            break
                
                elif vulnerability_type == "xss":
                    if payload in body:
                        verification_result["is_vulnerable"] = True
                        verification_result["evidence"] = "XSS payload 被反射到响应中"
                
                elif vulnerability_type == "command_injection":
                    # 检查命令执行结果
                    if "uid=" in body or "root:" in body:
                        verification_result["is_vulnerable"] = True
                        verification_result["evidence"] = "命令执行成功"
            
            verification_result["response_status"] = status_code
            verification_result["response_length"] = len(body)
            
        except Exception as e:
            verification_result["error"] = str(e)
        
        return verification_result

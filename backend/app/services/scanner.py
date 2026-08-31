"""
仓库扫描服务 - 支持GitHub, GitLab 和 Gitea 仓库扫描
"""

import asyncio
import httpx
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from urllib.parse import urlparse, quote
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.repo_utils import parse_repository_url
from app.models.project import Project
from app.services.llm.service import LLMService
from app.core.config import settings


def get_analysis_config(user_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    获取分析配置参数（优先使用用户配置，然后使用系统配置）

    Returns:
        包含以下字段的字典:
        - max_analyze_files: 最大分析文件数
        - llm_concurrency: LLM 并发数
        - llm_gap_ms: LLM 请求间隔（毫秒）
    """
    other_config = (user_config or {}).get('otherConfig', {})

    return {
        'max_analyze_files': other_config.get('maxAnalyzeFiles') or settings.MAX_ANALYZE_FILES,
        'llm_concurrency': other_config.get('llmConcurrency') or settings.LLM_CONCURRENCY,
        'llm_gap_ms': other_config.get('llmGapMs') or settings.LLM_GAP_MS,
    }


# 支持的文本文件扩展名
TEXT_EXTENSIONS = [
    ".js", ".ts", ".tsx", ".jsx", ".py", ".java", ".go", ".rs", 
    ".cpp", ".c", ".h", ".cc", ".hh", ".cs", ".php", ".rb", 
    ".kt", ".swift", ".sql", ".sh", ".json", ".yml", ".yaml"
]

# 排除的目录和文件模式
EXCLUDE_PATTERNS = [
    "node_modules/", "vendor/", "dist/", "build/", ".git/",
    "__pycache__/", ".pytest_cache/", ".venv/", "venv/", "env/",
    ".tox/", ".mypy_cache/", ".ruff_cache/", ".next/", ".nuxt/",
    ".cache/", "coverage/", ".nyc_output/",
    ".vscode/", ".idea/", ".vs/", "target/", "out/",
    "__MACOSX/", ".DS_Store", "package-lock.json", "yarn.lock",
    "pnpm-lock.yaml", ".min.js", ".min.css", ".map"
]


def is_text_file(path: str) -> bool:
    """检查是否为文本文件"""
    return any(path.lower().endswith(ext) for ext in TEXT_EXTENSIONS)


def should_exclude(path: str, exclude_patterns: List[str] = None) -> bool:
    """检查是否应该排除该文件"""
    all_patterns = EXCLUDE_PATTERNS + (exclude_patterns or [])
    return any(pattern in path for pattern in all_patterns)


def get_language_from_path(path: str) -> str:
    """从文件路径获取语言类型"""
    ext = path.split('.')[-1].lower() if '.' in path else ''
    language_map = {
        'js': 'javascript', 'jsx': 'javascript',
        'ts': 'typescript', 'tsx': 'typescript',
        'py': 'python', 'java': 'java', 'go': 'go',
        'rs': 'rust', 'cpp': 'cpp', 'c': 'cpp',
        'cc': 'cpp', 'h': 'cpp', 'hh': 'cpp',
        'cs': 'csharp', 'php': 'php', 'rb': 'ruby',
        'kt': 'kotlin', 'swift': 'swift'
    }
    return language_map.get(ext, 'text')


class TaskControlManager:
    """任务控制管理器 - 用于取消运行中的任务"""
    
    def __init__(self):
        self._cancelled_tasks: set = set()
    
    def cancel_task(self, task_id: str):
        """取消任务"""
        self._cancelled_tasks.add(task_id)
        print(f"🛑 任务 {task_id} 已标记为取消")
    
    def is_cancelled(self, task_id: str) -> bool:
        """检查任务是否被取消"""
        return task_id in self._cancelled_tasks
    
    def cleanup_task(self, task_id: str):
        """清理已完成任务的控制状态"""
        self._cancelled_tasks.discard(task_id)


# 全局任务控制器
task_control = TaskControlManager()


async def github_api(url: str, token: str = None) -> Any:
    """调用GitHub API"""
    headers = {"Accept": "application/vnd.github+json"}
    t = token or settings.GITHUB_TOKEN
    if t:
        headers["Authorization"] = f"Bearer {t}"
    
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, headers=headers)
        if response.status_code == 403:
            raise Exception("GitHub API 403：请配置 GITHUB_TOKEN 或确认仓库权限/频率限制")
        if response.status_code != 200:
            raise Exception(f"GitHub API {response.status_code}: {url}")
        return response.json()



async def gitea_api(url: str, token: str = None) -> Any:
    """调用Gitea API"""
    headers = {"Content-Type": "application/json"}
    t = token or settings.GITEA_TOKEN
    if t:
        headers["Authorization"] = f"token {t}"
    
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, headers=headers)
        if response.status_code == 401:
            raise Exception("Gitea API 401：请配置 GITEA_TOKEN 或确认仓库权限")
        if response.status_code == 403:
            raise Exception("Gitea API 403：请确认仓库权限/频率限制")
        if response.status_code != 200:
            raise Exception(f"Gitea API {response.status_code}: {url}")
        return response.json()


async def gitlab_api(url: str, token: str = None) -> Any:
    """调用GitLab API"""
    headers = {"Content-Type": "application/json"}
    t = token or settings.GITLAB_TOKEN
    if t:
        headers["PRIVATE-TOKEN"] = t
    
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, headers=headers)
        if response.status_code == 401:
            raise Exception("GitLab API 401：请配置 GITLAB_TOKEN 或确认仓库权限")
        if response.status_code == 403:
            raise Exception("GitLab API 403：请确认仓库权限/频率限制")
        if response.status_code != 200:
            raise Exception(f"GitLab API {response.status_code}: {url}")
        return response.json()


async def fetch_file_content(url: str, headers: Dict[str, str] = None) -> Optional[str]:
    """获取文件内容"""
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.get(url, headers=headers or {})
            if response.status_code == 200:
                return response.text
        except Exception as e:
            print(f"获取文件内容失败: {url}, 错误: {e}")
    return None


async def get_github_branches(repo_url: str, token: str = None) -> List[str]:
    """获取GitHub仓库分支列表（支持分页）"""
    repo_info = parse_repository_url(repo_url, "github")
    owner, repo = repo_info['owner'], repo_info['repo']

    all_branches = []
    page = 1
    per_page = 100

    while True:
        branches_url = f"https://api.github.com/repos/{owner}/{repo}/branches?per_page={per_page}&page={page}"
        branches_data = await github_api(branches_url, token)

        if not branches_data:
            break

        all_branches.extend([b["name"] for b in branches_data])

        if len(branches_data) < per_page:
            break

        page += 1

    return all_branches


async def get_github_repository_metadata(repo_url: str, token: str = None) -> Dict[str, Any]:
    """Fetch GitHub repository metadata used for import defaults."""
    repo_info = parse_repository_url(repo_url, "github")
    owner, repo = repo_info['owner'], repo_info['repo']

    repo_api_url = f"https://api.github.com/repos/{owner}/{repo}"
    data = await github_api(repo_api_url, token)
    return {"default_branch": data.get("default_branch")}





async def get_gitea_branches(repo_url: str, token: str = None) -> List[str]:
    """获取Gitea仓库分支列表"""
    repo_info = parse_repository_url(repo_url, "gitea")
    base_url = repo_info['base_url'] # This is {base}/api/v1
    owner, repo = repo_info['owner'], repo_info['repo']
    
    branches_url = f"{base_url}/repos/{owner}/{repo}/branches"
    branches_data = await gitea_api(branches_url, token)
    
    return [b["name"] for b in branches_data]


async def get_gitlab_branches(repo_url: str, token: str = None) -> List[str]:
    """获取GitLab仓库分支列表"""
    parsed = urlparse(repo_url)
    
    extracted_token = token
    if parsed.username:
        if parsed.username == 'oauth2' and parsed.password:
            extracted_token = parsed.password
        elif parsed.username and not parsed.password:
            extracted_token = parsed.username
    
    repo_info = parse_repository_url(repo_url, "gitlab")
    base_url = repo_info['base_url']
    project_path = quote(repo_info['project_path'], safe='')
    
    branches_url = f"{base_url}/projects/{project_path}/repository/branches?per_page=100"
    branches_data = await gitlab_api(branches_url, extracted_token)
    
    return [b["name"] for b in branches_data]


async def get_github_files(repo_url: str, branch: str, token: str = None, exclude_patterns: List[str] = None) -> List[Dict[str, str]]:
    """获取GitHub仓库文件列表"""
    # 解析仓库URL
    repo_info = parse_repository_url(repo_url, "github")
    owner, repo = repo_info['owner'], repo_info['repo']
    
    # 获取仓库文件树
    tree_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{quote(branch)}?recursive=1"
    tree_data = await github_api(tree_url, token)
    
    files = []
    for item in tree_data.get("tree", []):
        if item.get("type") == "blob" and is_text_file(item["path"]) and not should_exclude(item["path"], exclude_patterns):
            size = item.get("size", 0)
            if size <= settings.MAX_FILE_SIZE_BYTES:
                files.append({
                    "path": item["path"],
                    "url": f"https://raw.githubusercontent.com/{owner}/{repo}/{quote(branch)}/{item['path']}"
                })
    
    return files


async def get_gitlab_files(repo_url: str, branch: str, token: str = None, exclude_patterns: List[str] = None) -> List[Dict[str, str]]:
    """获取GitLab仓库文件列表"""
    parsed = urlparse(repo_url)
    
    # 从URL中提取token（如果存在）
    extracted_token = token
    if parsed.username:
        if parsed.username == 'oauth2' and parsed.password:
            extracted_token = parsed.password
        elif parsed.username and not parsed.password:
            extracted_token = parsed.username
    
    # 解析项目路径
    repo_info = parse_repository_url(repo_url, "gitlab")
    base_url = repo_info['base_url'] # {base}/api/v4
    project_path = quote(repo_info['project_path'], safe='')
    
    # 获取仓库文件树
    tree_url = f"{base_url}/projects/{project_path}/repository/tree?ref={quote(branch)}&recursive=true&per_page=100"
    tree_data = await gitlab_api(tree_url, extracted_token)
    
    files = []
    for item in tree_data:
        if item.get("type") == "blob" and is_text_file(item["path"]) and not should_exclude(item["path"], exclude_patterns):
            files.append({
                "path": item["path"],
                "url": f"{base_url}/projects/{project_path}/repository/files/{quote(item['path'], safe='')}/raw?ref={quote(branch)}",
                "token": extracted_token
            })
    
    return files



async def get_gitea_files(repo_url: str, branch: str, token: str = None, exclude_patterns: List[str] = None) -> List[Dict[str, str]]:
    """获取Gitea仓库文件列表"""
    repo_info = parse_repository_url(repo_url, "gitea")
    base_url = repo_info['base_url']
    owner, repo = repo_info['owner'], repo_info['repo']
    
    # Gitea tree API: GET /repos/{owner}/{repo}/git/trees/{sha}?recursive=1
    # 可以直接使用分支名作为sha
    tree_url = f"{base_url}/repos/{owner}/{repo}/git/trees/{quote(branch)}?recursive=1"
    tree_data = await gitea_api(tree_url, token)
    
    files = []
    for item in tree_data.get("tree", []):
         # Gitea API returns 'type': 'blob' for files
        if item.get("type") == "blob" and is_text_file(item["path"]) and not should_exclude(item["path"], exclude_patterns):
            # 使用API raw endpoint: GET /repos/{owner}/{repo}/raw/{filepath}?ref={branch}
             files.append({
                "path": item["path"],
                "url": f"{base_url}/repos/{owner}/{repo}/raw/{quote(item['path'])}?ref={quote(branch)}",
                "token": token # 传递token以便fetch_file_content使用
            })
    
    return files

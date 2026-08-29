"""只读变更请求来源适配器。"""

import json
import re
from pathlib import Path
from typing import Protocol
from urllib.parse import quote
from urllib.request import Request, urlopen

from .models import ChangeRequest


class ChangeRequestAdapter(Protocol):
    """连接外部代码托管平台与评审流水线的只读边界。"""

    def fetch(self, url: str) -> ChangeRequest: ...


class LocalDiffAdapter:
    def __init__(self, diff_path: str | Path) -> None:
        self.diff_path = Path(diff_path)

    def fetch(self, url: str) -> ChangeRequest:
        """读取调用方明确指定的 diff，不克隆仓库，也不执行仓库代码。"""
        if not url.startswith("local://"):
            raise ValueError("LocalDiffAdapter only accepts local:// URLs")
        if not self.diff_path.is_file():
            raise FileNotFoundError(self.diff_path)
        return ChangeRequest(url=url, diff=self.diff_path.read_text(encoding="utf-8"), source="local")


class GitHubAdapter:
    def __init__(self, token: str | None = None, timeout: float = 20.0) -> None:
        self.token, self.timeout = token, timeout

    def fetch(self, url: str) -> ChangeRequest:
        """先获取 PR 元数据，再请求元数据中的 unified diff 地址。"""
        match = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
        if not match:
            raise ValueError("invalid GitHub pull request URL")
        owner, repo, number = match.groups()
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "code-review-agent"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        # GitHub 元数据示例：{"title": "...", "user": {"login": "..."},
        # "diff_url": "https://github.com/.../pull/1.diff"。
        request = Request(f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}", headers=headers)
        with urlopen(request, timeout=self.timeout) as response:
            metadata = json.loads(response.read())
        diff_headers = {"User-Agent": "code-review-agent"}
        if self.token:
            diff_headers["Authorization"] = f"Bearer {self.token}"
        diff_request = Request(metadata["diff_url"], headers=diff_headers)
        with urlopen(diff_request, timeout=self.timeout) as response:
            diff = response.read().decode("utf-8")
        return ChangeRequest(url=url, title=metadata.get("title", ""), author=metadata.get("user", {}).get("login", ""), diff=diff, source="github")


class GitLabAdapter:
    def __init__(self, token: str | None = None, timeout: float = 20.0) -> None:
        self.token, self.timeout = token, timeout

    def fetch(self, url: str) -> ChangeRequest:
        """在同一 host 白名单约束下获取 MR 元数据和变更列表。"""
        match = re.match(r"(https?://[^/]+)/(.+)/-/merge_requests/(\d+)", url)
        if not match:
            raise ValueError("invalid GitLab merge request URL")
        host, project, number = match.groups()
        headers = {"User-Agent": "code-review-agent"}
        if self.token:
            headers["PRIVATE-TOKEN"] = self.token
        encoded = quote(project, safe="")
        # GitLab 元数据和 changes 响应分别包含 title/author 与 changes[].diff；
        # 这里只提取评审所需字段，不克隆项目或执行其中的代码。
        request = Request(f"{host}/api/v4/projects/{encoded}/merge_requests/{number}", headers=headers)
        with urlopen(request, timeout=self.timeout) as response:
            metadata = json.loads(response.read())
        payload = metadata
        if not payload.get("changes") or isinstance(payload.get("changes"), list) is False:
            changes_request = Request(f"{host}/api/v4/projects/{encoded}/merge_requests/{number}/changes", headers=headers)
            with urlopen(changes_request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        diff = "\n".join(change.get("diff", "") for change in payload.get("changes", []))
        return ChangeRequest(url=url, title=metadata.get("title", ""), author=metadata.get("author", {}).get("username", ""), diff=diff, source="gitlab")

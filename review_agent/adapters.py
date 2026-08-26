"""Change request source adapters."""

from pathlib import Path
from typing import Protocol

from .models import ChangeRequest


class ChangeRequestAdapter(Protocol):
    """Read-only source adapter contract used by the review pipeline."""

    def fetch(self, url: str) -> ChangeRequest:
        ...


class LocalDiffAdapter:
    """Read a diff from an explicitly supplied local file.

    Network URLs are deliberately rejected; remote providers can implement
    the same adapter contract without granting the local adapter network access.
    """

    def __init__(self, diff_path: str | Path) -> None:
        self.diff_path = Path(diff_path)

    def fetch(self, url: str) -> ChangeRequest:
        if not url.startswith("local://"):
            raise ValueError("LocalDiffAdapter only accepts local:// URLs")
        if not self.diff_path.is_file():
            raise FileNotFoundError(self.diff_path)
        return ChangeRequest(url=url, diff=self.diff_path.read_text(encoding="utf-8"), source="local")


class GitHubAdapter:
    """GitHub pull-request adapter interface.

    Network retrieval is intentionally left explicit for v1; callers get a
    stable, actionable error instead of a misleading "adapter missing" error.
    """

    def fetch(self, url: str) -> ChangeRequest:
        raise NotImplementedError(
            "GitHubAdapter is available but remote diff retrieval is not configured; "
            "provide --diff-file for offline review or configure a GitHub token adapter"
        )


class GitLabAdapter:
    """GitLab merge-request adapter interface (remote retrieval is opt-in)."""

    def fetch(self, url: str) -> ChangeRequest:
        raise NotImplementedError(
            "GitLabAdapter is available but remote diff retrieval is not configured; "
            "provide --diff-file for offline review or configure a GitLab token adapter"
        )

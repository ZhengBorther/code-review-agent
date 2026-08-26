"""Change request source adapters."""

from pathlib import Path

from .models import ChangeRequest


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

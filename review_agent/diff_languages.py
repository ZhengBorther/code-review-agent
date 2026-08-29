"""按语言分组 unified diff 文件片段。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageDiff:
    language: str
    files: tuple[str, ...]
    diff: str
    diff_hash: str


_EXTENSIONS = {
    ".go": "go", ".py": "python", ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".java": "java",
    ".rs": "rust", ".cs": "csharp",
}
_SECTION = re.compile(r"^diff --git .*$", re.MULTILINE)


def _path_for_section(section: str) -> str | None:
    for line in section.splitlines():
        if line.startswith("+++ "):
            value = line[4:].split("\t", 1)[0]
            if value == "/dev/null":
                return None
            if value.startswith("b/"):
                return value[2:]
            return value
    # 二进制或格式不完整的片段可能没有 +++ 行；从标准 Git 头恢复路径，
    # 这样它们会进入 unknown 分组而不会被静默丢弃。
    header = re.search(r"^diff --git a/(\S+) b/(\S+)$", section, re.MULTILINE)
    if header:
        return header.group(2)
    return None


def _language(path: str) -> str | None:
    lowered = path.lower()
    for extension, language in _EXTENSIONS.items():
        if lowered.endswith(extension):
            return language
    return None


def split_diff_by_language(diff: str) -> tuple[LanguageDiff, ...]:
    """保留完整 diff 片段，无法识别扩展名的文件归入 ``unknown``。"""
    starts = [match.start() for match in _SECTION.finditer(diff)]
    sections = [diff[start:end] for start, end in zip(starts, starts[1:] + [len(diff)])]
    grouped: dict[str, list[tuple[str, str]]] = {}
    for section in sections:
        path = _path_for_section(section)
        language = _language(path) if path else None
        if language is not None:
            grouped.setdefault(language, []).append((path, section))
        elif path is not None:
            # 未知文件必须保留，供诊断和策略决策使用，不能从评审输入中消失。
            grouped.setdefault("unknown", []).append((path, section))
    result = []
    for language in sorted(grouped):
        entries = sorted(grouped[language], key=lambda item: (item[0], item[1]))
        grouped_diff = "".join(item[1] for item in entries)
        result.append(LanguageDiff(
            language=language,
            files=tuple(item[0] for item in entries),
            diff=grouped_diff,
            diff_hash=hashlib.sha256(grouped_diff.encode("utf-8")).hexdigest(),
        ))
    return tuple(result)

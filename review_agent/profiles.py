"""按仓库选择 MDR 目录和文件过滤规则的 TOML profile。"""

from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepoProfile:
    """一个 profile 只影响规则范围和跳过文件，不包含可执行命令。"""

    name: str
    repo: str
    rules_dirs: tuple[Path, ...] = ()
    enabled_languages: tuple[str, ...] = ()
    skip_globs: tuple[str, ...] = ()


def load_profiles(path: str | Path) -> tuple[RepoProfile, ...]:
    """读取 TOML 中的 ``[[profiles]]``，路径相对 profile 文件解析。"""
    config_path = Path(path).resolve()
    with config_path.open("rb") as stream:
        data = tomllib.load(stream)
    raw_profiles = data.get("profiles", [])
    if not isinstance(raw_profiles, list):
        raise ValueError("profiles must be an array of tables")
    result: list[RepoProfile] = []
    for raw in raw_profiles:
        if not isinstance(raw, dict) or not isinstance(raw.get("repo"), str):
            raise ValueError("each profile requires a repo string")
        raw_dirs = raw.get("rules_dirs", [])
        raw_languages = raw.get("enabled_languages", [])
        raw_skips = raw.get("skip_globs", [])
        if not all(isinstance(item, str) for item in (*raw_dirs, *raw_languages, *raw_skips)):
            raise ValueError(f"profile {raw.get('name', '<unnamed>')} lists must contain strings")
        result.append(RepoProfile(
            name=str(raw.get("name", raw["repo"])),
            repo=str(Path(raw["repo"]).expanduser().resolve()),
            rules_dirs=tuple((config_path.parent / item).resolve() for item in raw_dirs),
            enabled_languages=tuple(item.lower() for item in raw_languages),
            skip_globs=tuple(raw_skips),
        ))
    return tuple(result)


def select_profile(profiles: tuple[RepoProfile, ...], repo_path: str | Path) -> RepoProfile | None:
    """按规范化绝对路径选择唯一 profile，未命中返回 None。"""
    target = str(Path(repo_path).expanduser().resolve())
    return next((profile for profile in profiles if target == profile.repo), None)


def profile_skips(profile: RepoProfile | None, path: str) -> bool:
    """判断相对文件路径是否匹配 profile 的跳过 glob。"""
    return bool(profile and any(fnmatch.fnmatch(path, pattern) for pattern in profile.skip_globs))

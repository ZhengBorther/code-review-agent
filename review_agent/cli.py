"""Command-line interface for the checkpointed code review agent."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

from .adapters import GitHubAdapter, GitLabAdapter, LocalDiffAdapter
from .config import RulesConfig, load_rules_config
from .llm import DeterministicClient, OpenAICompatibleClient
from .models import RunConfig
from .pipeline import ReviewPipeline
from .rules import MdrRuleLoader, RuleRegistry, RuleLoadError
from .storage import StateStore
from .tools import ToolRegistry


def _gitlab_host_allowed(host: str, cli_hosts: list[str], persisted: dict | None) -> bool:
    normalized = {item.strip().lower() for item in cli_hosts if item.strip()}
    env_hosts = {item.strip().lower() for item in os.getenv("GITLAB_ALLOWED_HOSTS", "").split(",") if item.strip()}
    if persisted:
        env_hosts.update(item.lower() for item in persisted.get("gitlab_allowed_hosts", []))
    return host == "gitlab.com" or host.endswith(".gitlab.com") or host in normalized or host in env_hosts


def _load_rule_registry(config_path: Path | None, rule_dirs: list[Path] | None, *, snapshot: list[dict] | None = None) -> RuleRegistry:
    if snapshot is not None:
        configured = load_rules_config(config_path, rule_dirs)
        return RuleRegistry.from_snapshot(configured, snapshot)
    """Load explicitly authorized MDR directories into one registry."""
    configured = load_rules_config(config_path, rule_dirs)
    directories = list(configured.directories)
    default_dir = Path("~/.config/code-review-agent/rules.d").expanduser()
    if default_dir.is_dir():
        directories.insert(0, default_dir.resolve())
    effective = RulesConfig(
        directories=tuple(dict.fromkeys(directories)),
        enabled_languages=configured.enabled_languages,
        disabled_rules=configured.disabled_rules,
    )
    registry = RuleRegistry(effective)
    loader = MdrRuleLoader()
    source_roots: dict[str, Path] = {}
    for directory in effective.directories:
        for rule in loader.load(directory):
            try:
                registry.register(rule)
            except RuleLoadError as exc:
                if "duplicate rule id" in str(exc):
                    existing_root = source_roots.get(rule.id)
                    existing = (existing_root / rule.source) if existing_root else Path(rule.source)
                    current = Path(directory) / rule.source
                    raise RuleLoadError(
                        f"duplicate rule id: {rule.id} (sources: {existing.resolve()}, {current.resolve()})"
                    ) from exc
                raise
            source_roots[rule.id] = Path(directory)
    return registry


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="code-review-agent", description="Generate an auditable code review report")
    sub = parser.add_subparsers(dest="command")
    review = sub.add_parser("review", help="review a pull/merge request diff")
    review.add_argument("url", nargs="?", help="change request URL (use --diff-file for local/offline review)")
    review.add_argument("--run-id", help="resume an existing run by ID")
    review.add_argument("--diff-file", type=Path, help="path to an explicit unified diff file")
    review.add_argument("--output", type=Path, default=Path("review.md"), help="Markdown report path")
    review.add_argument("--state-dir", type=Path, default=Path(".review-state"), help="directory for SQLite checkpoints/traces")
    review.add_argument("--config", type=Path, help="TOML configuration file")
    review.add_argument("--rules-dir", type=Path, action="append", default=[], help="authorized MDR rule directory; repeatable")
    review.add_argument("--budget-usd", type=float, default=1.0, help="maximum LLM spend in USD")
    review.add_argument("--model", default=os.getenv("ONEAPI_MODEL", "large"), help="primary model name")
    review.add_argument("--fallback-model", default=os.getenv("ONEAPI_FALLBACK_MODEL", "small"), help="fallback model name")
    review.add_argument("--oneapi-base-url", default=os.getenv("ONEAPI_BASE_URL"), help="OpenAI-compatible API base URL")
    review.add_argument("--oneapi-api-key", default=os.getenv("ONEAPI_API_KEY") or os.getenv("OPENAI_API_KEY"), help="OneAPI API key")
    review.add_argument("--offline", action="store_true", help="use deterministic local model; never access network")
    review.add_argument("--gitlab-host", action="append", default=[], help="explicitly authorize a self-hosted GitLab hostname; repeatable")
    return parser


def _run_review(args: argparse.Namespace) -> int:
    url = args.url or ""
    if args.run_id is None and args.diff_file is None and not args.url:
        raise ValueError("provide a PR/MR URL or --diff-file")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(args.state_dir / "state.db")
    persisted_config = None
    if args.run_id is not None:
        persisted = store.get_run(args.run_id)
        persisted_config = persisted["config"]
        url = persisted["config"].get("url") or persisted["url"]
        if url.startswith("local://"):
            if store.get_checkpoint(args.run_id, "fetch") is None and args.diff_file is None:
                raise ValueError("无法恢复 fetch：需要原始 diff 文件（请提供 --diff-file）")
            adapter = LocalDiffAdapter(args.diff_file or Path("__unused_diff_for_resume__"))
        else:
            host = (urlparse(url).hostname or "").lower()
            if host == "github.com" or host.endswith(".github.com"):
                adapter = GitHubAdapter(token=os.getenv("GITHUB_TOKEN"))
            elif _gitlab_host_allowed(host, args.gitlab_host, persisted_config):
                adapter = GitLabAdapter(token=os.getenv("GITLAB_TOKEN"))
            else:
                raise ValueError("unsupported persisted change-request URL; expected GitHub or GitLab PR/MR")
    elif args.diff_file is not None:
        resolved = args.diff_file.resolve()
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest() if resolved.is_file() else "missing"
        # Include path and content identity so different local inputs never
        # silently reuse a prior run's checkpoints.
        url = args.url or f"local://{resolved}#sha256={digest}"
        adapter = LocalDiffAdapter(args.diff_file)
    else:
        host = (urlparse(args.url).hostname or "").lower()
        if host == "github.com" or host.endswith(".github.com"):
            adapter = GitHubAdapter(token=os.getenv("GITHUB_TOKEN"))
        elif _gitlab_host_allowed(host, args.gitlab_host, None):
            adapter = GitLabAdapter(token=os.getenv("GITLAB_TOKEN"))
        else:
            raise ValueError("unsupported change-request URL; expected GitHub or GitLab PR/MR")

    if args.offline:
        client = DeterministicClient()
    else:
        if not args.oneapi_base_url or not args.oneapi_api_key:
            raise ValueError("OneAPI requires --oneapi-base-url and --oneapi-api-key (or ONEAPI_BASE_URL/ONEAPI_API_KEY)")
        client = OpenAICompatibleClient(args.oneapi_base_url, args.oneapi_api_key)

    if persisted_config is not None and args.config is None and not args.rules_dir:
        # A resumed run must use the exact rule snapshot that created its
        # checkpoints; loading today's directories could silently change them.
        snapshot_config = RulesConfig(
            enabled_languages=tuple(persisted_config.get("rules_enabled_languages", ())),
            disabled_rules=tuple(persisted_config.get("rules_disabled", ())),
        )
        rules = RuleRegistry.from_snapshot(snapshot_config, persisted_config.get("rules_snapshot", []))
    else:
        rules = _load_rule_registry(args.config, args.rules_dir)
    config = RunConfig(
        url=url,
        budget_usd=args.budget_usd,
        model=args.model,
        fallback_model=args.fallback_model,
        offline=args.offline,
        output_path=str(args.output),
        state_dir=str(args.state_dir),
        rules_directories=tuple(str(item) for item in getattr(rules.config, "directories", ())),
        rules_enabled_languages=tuple(getattr(rules.config, "enabled_languages", ())),
        rules_disabled=tuple(getattr(rules.config, "disabled_rules", ())),
        rules_snapshot=rules.snapshot(),
        gitlab_allowed_hosts=tuple(args.gitlab_host),
    )
    run_target = args.run_id
    if not run_target and url.startswith("local://"):
        existing = store.find_latest_run(url)
        run_target = existing["run_id"] if existing else url
    if not run_target:
        run_target = url
    result = ReviewPipeline(store, adapter, ToolRegistry.with_builtins(), client, config, rules=rules).run(run_target)
    args.output.write_text(result.markdown, encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        if args.command is None:
            parser.print_help()
            return 0
        return _run_review(args)
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

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
from .config import RulesConfig, load_app_config, load_rules_config
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
    review.add_argument("--output", type=Path, default=None, help="Markdown report path")
    review.add_argument("--state-dir", type=Path, default=None, help="directory for SQLite checkpoints/traces")
    review.add_argument("--config", type=Path, help="TOML configuration file")
    review.add_argument("--rules-dir", type=Path, action="append", default=[], help="authorized MDR rule directory; repeatable")
    review.add_argument("--budget-usd", type=float, default=None, help="maximum LLM spend in USD")
    review.add_argument("--model", default=None, help="primary model name")
    review.add_argument("--fallback-model", default=None, help="fallback model name")
    review.add_argument("--oneapi-base-url", default=None, help="OpenAI-compatible API base URL")
    review.add_argument("--oneapi-api-key", default=None, help="OneAPI API key (prefer ONEAPI_API_KEY)")
    review.add_argument("--github-token", default=None, help="GitHub token (prefer GITHUB_TOKEN)")
    review.add_argument("--gitlab-token", default=None, help="GitLab token (prefer GITLAB_TOKEN)")
    review.add_argument("--offline", action="store_true", default=None, help="use deterministic local model; never access network")
    review.add_argument("--gitlab-host", action="append", default=[], help="explicitly authorize a self-hosted GitLab hostname; repeatable")
    return parser


def _run_review(args: argparse.Namespace) -> int:
    url = args.url or ""
    if args.run_id is None and args.diff_file is None and not args.url:
        raise ValueError("provide a PR/MR URL or --diff-file")
    app_config = load_app_config(
        args.config,
        args.rules_dir,
        overrides={
            "budget_usd": args.budget_usd,
            "model": args.model,
            "fallback_model": args.fallback_model,
            "output_path": str(args.output) if args.output is not None else None,
            "state_dir": str(args.state_dir) if args.state_dir is not None else None,
            "llm_base_url": args.oneapi_base_url,
            "llm_api_key": args.oneapi_api_key,
            "github_token": args.github_token,
            "gitlab_token": args.gitlab_token,
            "offline": args.offline,
            "gitlab_allowed_hosts": tuple(args.gitlab_host) if args.gitlab_host else None,
        },
    )
    output = Path(app_config.output_path)
    state_dir = Path(app_config.state_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(state_dir / "state.db")
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
                adapter = GitHubAdapter(token=app_config.github_token)
            elif _gitlab_host_allowed(host, list(app_config.gitlab_allowed_hosts), persisted_config):
                adapter = GitLabAdapter(token=app_config.gitlab_token)
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
            adapter = GitHubAdapter(token=app_config.github_token)
        elif _gitlab_host_allowed(host, list(app_config.gitlab_allowed_hosts), None):
            adapter = GitLabAdapter(token=app_config.gitlab_token)
        else:
            raise ValueError("unsupported change-request URL; expected GitHub or GitLab PR/MR")

    if app_config.offline:
        client = DeterministicClient()
    else:
        if not app_config.llm_base_url or not app_config.llm_api_key:
            raise ValueError("OneAPI requires --oneapi-base-url and --oneapi-api-key (or ONEAPI_BASE_URL/ONEAPI_API_KEY)")
        client = OpenAICompatibleClient(
            app_config.llm_base_url,
            app_config.llm_api_key,
            timeout=app_config.llm_timeout_seconds,
            pricing=app_config.model_pricing,
        )

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
        budget_usd=app_config.budget_usd,
        model=app_config.model,
        fallback_model=app_config.fallback_model,
        offline=app_config.offline,
        max_diff_chars=app_config.max_diff_chars,
        completion_tokens=app_config.completion_tokens,
        output_path=str(output),
        state_dir=str(state_dir),
        rules_directories=tuple(str(item) for item in getattr(rules.config, "directories", ())),
        rules_enabled_languages=tuple(getattr(rules.config, "enabled_languages", ())),
        rules_disabled=tuple(getattr(rules.config, "disabled_rules", ())),
        rules_snapshot=rules.snapshot(),
        gitlab_allowed_hosts=app_config.gitlab_allowed_hosts,
        model_pricing=app_config.model_pricing,
        llm_timeout_seconds=app_config.llm_timeout_seconds,
    )
    run_target = args.run_id
    if not run_target and url.startswith("local://"):
        existing = store.find_latest_run(url)
        run_target = existing["run_id"] if existing else url
    if not run_target:
        run_target = url
    result = ReviewPipeline(store, adapter, ToolRegistry.with_builtins(), client, config, rules=rules).run(run_target)
    output.write_text(result.markdown, encoding="utf-8")
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

"""Command-line interface for the checkpointed code review agent."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from .adapters import GitHubAdapter, GitLabAdapter, LocalDiffAdapter
from .llm import DeterministicClient, OpenAICompatibleClient
from .models import RunConfig
from .pipeline import ReviewPipeline
from .storage import StateStore
from .tools import ToolRegistry


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="code-review-agent", description="Generate an auditable code review report")
    sub = parser.add_subparsers(dest="command")
    review = sub.add_parser("review", help="review a pull/merge request diff")
    review.add_argument("url", nargs="?", help="change request URL (use --diff-file for local/offline review)")
    review.add_argument("--run-id", help="resume an existing run by ID")
    review.add_argument("--diff-file", type=Path, help="path to an explicit unified diff file")
    review.add_argument("--output", type=Path, default=Path("review.md"), help="Markdown report path")
    review.add_argument("--state-dir", type=Path, default=Path(".review-state"), help="directory for SQLite checkpoints/traces")
    review.add_argument("--budget-usd", type=float, default=1.0, help="maximum LLM spend in USD")
    review.add_argument("--model", default=os.getenv("ONEAPI_MODEL", "large"), help="primary model name")
    review.add_argument("--fallback-model", default=os.getenv("ONEAPI_FALLBACK_MODEL", "small"), help="fallback model name")
    review.add_argument("--oneapi-base-url", default=os.getenv("ONEAPI_BASE_URL"), help="OpenAI-compatible API base URL")
    review.add_argument("--oneapi-api-key", default=os.getenv("ONEAPI_API_KEY") or os.getenv("OPENAI_API_KEY"), help="OneAPI API key")
    review.add_argument("--offline", action="store_true", help="use deterministic local model; never access network")
    return parser


def _run_review(args: argparse.Namespace) -> int:
    url = args.url or ""
    if args.run_id is None and args.diff_file is None and not args.url:
        raise ValueError("provide a PR/MR URL or --diff-file")
    if args.diff_file is not None:
        resolved = args.diff_file.resolve()
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest() if resolved.is_file() else "missing"
        # Include path and content identity so different local inputs never
        # silently reuse a prior run's checkpoints.
        url = args.url or f"local://{resolved}#sha256={digest}"
        adapter = LocalDiffAdapter(args.diff_file)
    elif args.run_id is not None:
        url = args.url or "local://diff"
        adapter = LocalDiffAdapter(Path("__unused_diff_for_resume__"))
    else:
        host = (urlparse(args.url).hostname or "").lower()
        if host == "github.com" or host.endswith(".github.com"):
            adapter = GitHubAdapter()
        elif host == "gitlab.com" or "gitlab" in host:
            adapter = GitLabAdapter()
        else:
            raise ValueError("unsupported change-request URL; expected GitHub or GitLab PR/MR")

    if args.offline:
        client = DeterministicClient()
    else:
        if not args.oneapi_base_url or not args.oneapi_api_key:
            raise ValueError("OneAPI requires --oneapi-base-url and --oneapi-api-key (or ONEAPI_BASE_URL/ONEAPI_API_KEY)")
        client = OpenAICompatibleClient(args.oneapi_base_url, args.oneapi_api_key)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    config = RunConfig(
        url=url,
        budget_usd=args.budget_usd,
        model=args.model,
        fallback_model=args.fallback_model,
        offline=args.offline,
        output_path=str(args.output),
        state_dir=str(args.state_dir),
    )
    store = StateStore(args.state_dir / "state.db")
    run_target = args.run_id
    if not run_target:
        existing = store.find_latest_run(url)
        run_target = existing["run_id"] if existing else url
    result = ReviewPipeline(store, adapter, ToolRegistry.with_builtins(), client, config).run(run_target)
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

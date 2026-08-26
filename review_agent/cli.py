"""Command-line interface for the checkpointed code review agent."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .adapters import LocalDiffAdapter
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
    if args.diff_file is None and not args.url:
        raise ValueError("provide a PR/MR URL or --diff-file")
    if args.diff_file is not None:
        url = args.url or "local://diff"
        adapter = LocalDiffAdapter(args.diff_file)
    else:
        raise ValueError("remote GitHub/GitLab adapters are not configured in this release; use --diff-file")

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
    result = ReviewPipeline(store, adapter, ToolRegistry.with_builtins(), client, config).run(url)
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

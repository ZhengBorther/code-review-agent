"""用于独立 MDR 语言批次的受限并发调度器。"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence


@dataclass(frozen=True)
class AsyncBatchConfig:
    """批次并发上限；预算原子性仍由调用方的 StateStore 保证。"""

    max_concurrency: int = 2

    def __post_init__(self) -> None:
        if not 1 <= self.max_concurrency <= 16:
            raise ValueError("max_concurrency must be between 1 and 16")


@dataclass(frozen=True)
class BatchRunResult:
    """并发批次的汇总结果，异常不会覆盖已完成的兄弟任务。"""

    results: tuple[Any, ...]
    errors: tuple[str, ...]


async def run_batches_async(
    batches: Sequence[Any],
    reviewer: Callable[[Any], Any | Awaitable[Any]],
    config: AsyncBatchConfig | None = None,
) -> BatchRunResult:
    """以 semaphore 限制并发执行 reviewer；每个 reviewer 自行负责 checkpoint/预算。"""
    settings = config or AsyncBatchConfig()
    semaphore = asyncio.Semaphore(settings.max_concurrency)

    async def run_one(batch: Any) -> tuple[Any | None, str | None]:
        async with semaphore:
            try:
                value = reviewer(batch)
                if inspect.isawaitable(value):
                    value = await value
                return value, None
            except Exception as exc:  # 保留其他批次结果
                return None, f"{type(exc).__name__}: {exc}"

    completed = await asyncio.gather(*(run_one(batch) for batch in batches))
    return BatchRunResult(
        results=tuple(value for value, error in completed if error is None),
        errors=tuple(error for value, error in completed if error is not None),
    )

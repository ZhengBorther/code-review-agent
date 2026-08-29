import asyncio

import pytest

from review_agent.async_batches import AsyncBatchConfig, run_batches_async


def test_async_batches_respect_concurrency_limit():
    async def run():
        active = 0
        peak = 0

        async def reviewer(batch):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return batch

        result = await run_batches_async(range(4), reviewer, AsyncBatchConfig(max_concurrency=2))
        return result, peak

    result, peak = asyncio.run(run())
    assert result.results == (0, 1, 2, 3)
    assert peak == 2


def test_async_batch_failure_does_not_erase_sibling_results():
    async def run():
        async def reviewer(batch):
            if batch == "bad":
                raise RuntimeError("boom")
            return batch

        return await run_batches_async(("good", "bad"), reviewer)

    result = asyncio.run(run())
    assert result.results == ("good",)
    assert "RuntimeError: boom" in result.errors


def test_async_batch_config_rejects_unbounded_concurrency():
    with pytest.raises(ValueError):
        AsyncBatchConfig(max_concurrency=17)

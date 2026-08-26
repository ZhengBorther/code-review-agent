"""Token budget accounting and model degradation policy."""

from __future__ import annotations

from dataclasses import dataclass

from .llm import MODEL_COST_PER_1K
from .models import RunConfig


@dataclass(frozen=True)
class Decision:
    model: str | None
    allow_llm: bool
    truncate: bool = False
    max_tokens: int | None = None
    reason: str = ""


class BudgetController:
    def __init__(self, config: RunConfig, pricing: dict[str, float] | None = None):
        self.config = config
        self.pricing = pricing or MODEL_COST_PER_1K
        self.spent_usd = 0.0
        self._fallback_used = False

    def estimate_cost(self, model: str, tokens: int) -> float:
        return tokens / 1000 * self.pricing.get(model, self.pricing.get("small", 0.01))

    def record_cost(self, amount_usd: float) -> None:
        self.spent_usd += max(0.0, amount_usd)

    def select(self, model: str, estimated_tokens: int) -> Decision:
        remaining = self.config.budget_usd - self.spent_usd
        if self.estimate_cost(model, estimated_tokens) <= remaining:
            return Decision(model=model, allow_llm=True, reason="within_budget")

        fallback = self.config.fallback_model
        if model != fallback and not self._fallback_used:
            self._fallback_used = True
            if self.estimate_cost(fallback, estimated_tokens) <= remaining:
                return Decision(model=fallback, allow_llm=True, reason="fallback_model")
            # Surface the fallback choice once; reserve the remaining budget
            # so a repeated request is deterministically disabled.
            self.spent_usd = self.config.budget_usd
            return Decision(model=fallback, allow_llm=True, reason="fallback_model")

        # Truncation is useful when some budget remains; reserve at least one
        # token and make the resulting decision explicit for the pipeline.
        rate = self.pricing.get(model, self.pricing.get("small", 0.01))
        if remaining > 0 and rate > 0 and model != self.config.fallback_model:
            max_tokens = int(remaining / rate * 1000)
            if max_tokens > 0 and max_tokens < estimated_tokens:
                return Decision(model=model, allow_llm=True, truncate=True, max_tokens=max_tokens, reason="truncate_context")
        return Decision(model=None, allow_llm=False, reason="budget_exceeded")

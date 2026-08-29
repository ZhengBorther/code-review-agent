"""Token budget accounting and model degradation policy."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from .llm import MODEL_COST_PER_1K
from .models import RunConfig


@dataclass(frozen=True)
class Decision:
    model: str | None
    allow_llm: bool
    truncate: bool = False
    max_tokens: int | None = None
    max_chars: int | None = None
    estimated_tokens: int | None = None
    reason: str = ""

@dataclass(frozen=True)
class Reservation:
    token: str
    reserved_usd: float


class BudgetController:
    """Apply the model fallback/truncation policy and track in-process spend."""

    def __init__(self, config: RunConfig, pricing: dict[str, float] | None = None):
        self.config = config
        self.pricing = pricing if pricing is not None else (config.model_pricing or MODEL_COST_PER_1K)
        self.spent_usd = 0.0
        self._fallback_used = False
        self.over_budget = False
        self.reserved_usd = 0.0
        self._reservations: dict[str, float] = {}

    def estimate_cost(self, model: str, tokens: int) -> float:
        """Estimate blended prompt+completion cost in USD for 1K-token pricing."""
        return tokens / 1000 * self.pricing.get(model, self.pricing.get("small", 0.01))

    def reserve(self, model: str, estimated_tokens: int) -> Reservation | None:
        """Atomically reserve prompt plus completion allowance before calling an LLM."""
        amount = self.estimate_cost(model, estimated_tokens + self.config.completion_tokens)
        if amount > max(0.0, self.config.budget_usd - self.spent_usd - self.reserved_usd):
            return None
        self.reserved_usd += amount
        token = Reservation(str(uuid4()), amount)
        self._reservations[token.token] = amount
        return token

    def commit(self, token: Reservation | str | None, actual_cost_usd: float) -> bool:
        """Close one reservation and reject a response that would exceed budget."""
        token_id = token.token if isinstance(token, Reservation) else token
        if not token_id or token_id not in self._reservations:
            return False
        amount = self._reservations.pop(token_id)
        self.reserved_usd = max(0.0, self.reserved_usd - amount)
        return self.accept_response(actual_cost_usd)

    def record_cost(self, amount_usd: float) -> None:
        if self.over_budget:
            return
        amount = max(0.0, amount_usd)
        remaining = max(0.0, self.config.budget_usd - self.spent_usd)
        if amount > remaining:
            self.spent_usd = self.config.budget_usd
            self.over_budget = True
        else:
            self.spent_usd += amount

    def accept_response(self, response_cost_usd: float) -> bool:
        """Record actual usage; return False when it exceeded remaining budget."""
        before = self.spent_usd
        self.record_cost(response_cost_usd)
        return not self.over_budget and response_cost_usd <= max(0.0, self.config.budget_usd - before)

    def select(self, model: str, estimated_tokens: int, *, allow_truncate: bool = False) -> Decision:
        """Choose primary, fallback, truncated, or disabled execution in that order."""
        if self.over_budget:
            return Decision(model=None, allow_llm=False, reason="budget_exceeded")
        remaining = self.config.budget_usd - self.spent_usd - self.reserved_usd
        estimated_total = estimated_tokens + self.config.completion_tokens
        if self.estimate_cost(model, estimated_total) <= remaining:
            return Decision(model=model, allow_llm=True, reason="within_budget")

        fallback = self.config.fallback_model
        if model != fallback and not self._fallback_used:
            self._fallback_used = True
            if self.estimate_cost(fallback, estimated_total) <= remaining:
                return Decision(model=fallback, allow_llm=True, reason="fallback_model")
            if allow_truncate:
                return self._truncate_or_disable(fallback, remaining, estimated_tokens)
            return Decision(model=None, allow_llm=False, reason="fallback_over_budget")

        # Truncation is useful when some budget remains; reserve at least one
        # token and make the resulting decision explicit for the pipeline.
        rate = self.pricing.get(model, self.pricing.get("small", 0.01))
        if allow_truncate and remaining > 0 and rate > 0:
            max_tokens = int(remaining / rate * 1000) - self.config.completion_tokens
            if max_tokens > 0 and max_tokens < estimated_tokens:
                return Decision(model=model, allow_llm=True, truncate=True, max_tokens=max_tokens, max_chars=max_tokens * 4, estimated_tokens=max_tokens, reason="truncate_context")
        return Decision(model=None, allow_llm=False, reason="budget_exceeded")

    def _truncate_or_disable(self, model: str, remaining: float, estimated_tokens: int) -> Decision:
        rate = self.pricing.get(model, self.pricing.get("small", 0.01))
        max_tokens = int(remaining / rate * 1000) - self.config.completion_tokens if rate > 0 else 0
        if 0 < max_tokens < estimated_tokens:
            return Decision(model=model, allow_llm=True, truncate=True, max_tokens=max_tokens, max_chars=max_tokens * 4, estimated_tokens=max_tokens, reason="truncate_context")
        return Decision(model=None, allow_llm=False, reason="fallback_over_budget")

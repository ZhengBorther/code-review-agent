"""Token 预算核算和模型降级策略。"""

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
    """执行主模型、降级、截断策略，并跟踪当前进程中的花费。"""

    def __init__(self, config: RunConfig, pricing: dict[str, float] | None = None):
        self.config = config
        self.pricing = pricing if pricing is not None else (config.model_pricing or MODEL_COST_PER_1K)
        self.spent_usd = 0.0
        self._fallback_used = False
        self.over_budget = False
        self.reserved_usd = 0.0
        self._reservations: dict[str, float] = {}

    def estimate_cost(self, model: str, tokens: int) -> float:
        """按每 1,000 token 的 blended 费率估算 prompt 加 completion 的美元成本。"""
        return tokens / 1000 * self.pricing.get(model, self.pricing.get("small", 0.01))

    def reserve(self, model: str, estimated_tokens: int) -> Reservation | None:
        """调用 LLM 前，预留 prompt 和 completion 配额，防止预计成本超预算。"""
        amount = self.estimate_cost(model, estimated_tokens + self.config.completion_tokens)
        if amount > max(0.0, self.config.budget_usd - self.spent_usd - self.reserved_usd):
            return None
        self.reserved_usd += amount
        token = Reservation(str(uuid4()), amount)
        self._reservations[token.token] = amount
        return token

    def commit(self, token: Reservation | str | None, actual_cost_usd: float) -> bool:
        """关闭一次预留；如果实际响应会超预算，则拒绝该响应。"""
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
        """记录实际用量；实际成本超过剩余预算时返回 False。"""
        before = self.spent_usd
        self.record_cost(response_cost_usd)
        return not self.over_budget and response_cost_usd <= max(0.0, self.config.budget_usd - before)

    def select(self, model: str, estimated_tokens: int, *, allow_truncate: bool = False) -> Decision:
        """按主模型、fallback、截断、禁用 LLM 的顺序选择执行方案。"""
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

        # 仍有少量预算时尝试截断，并把上限明确交给流水线执行。
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

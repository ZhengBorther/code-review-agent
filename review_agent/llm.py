"""OpenAI-compatible and deterministic LLM clients."""

from __future__ import annotations

import json
import urllib.request
from typing import Protocol

from .models import LLMResponse


# USD per 1K tokens. These conservative defaults can be overridden by callers
# through BudgetController pricing; responses with provider-reported costs are
# still represented using the same estimates.
MODEL_COST_PER_1K = {"large": 0.03, "small": 0.01}


class LLMClient(Protocol):
    def review(self, prompt: str, model: str, *, max_chars: int | None = None, max_tokens: int | None = None) -> LLMResponse: ...


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int = 0) -> float:
    rate = MODEL_COST_PER_1K.get(model, MODEL_COST_PER_1K["small"])
    return (prompt_tokens + completion_tokens) / 1000 * rate


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


class OpenAICompatibleClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def review(self, prompt: str, model: str, *, max_chars: int | None = None, max_tokens: int | None = None) -> LLMResponse:
        if max_chars is not None:
            prompt = prompt[:max_chars]
        endpoint = self.base_url if self.base_url.endswith("/chat/completions") else self.base_url + "/chat/completions"
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("invalid chat-completions response") from exc
        usage = data.get("usage") or {}
        usage_known = "prompt_tokens" in usage or "completion_tokens" in usage
        prompt_tokens = int(usage.get("prompt_tokens", estimate_tokens(prompt)) or 0)
        completion_tokens = int(usage.get("completion_tokens", estimate_tokens(str(text))) or 0)
        return LLMResponse(
            text=str(text),
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=estimate_cost(model, prompt_tokens, completion_tokens),
            usage_known=usage_known,
        )


class DeterministicClient:
    def review(self, prompt: str, model: str, *, max_chars: int | None = None, max_tokens: int | None = None) -> LLMResponse:
        if max_chars is not None:
            prompt = prompt[:max_chars]
        # Stable output keeps offline runs reproducible and avoids network use.
        return LLMResponse(text=f"Offline review ({model}): {prompt}", model=model)

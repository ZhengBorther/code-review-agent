"""OpenAI 兼容客户端和离线确定性 LLM 客户端。"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Protocol

from .models import LLMResponse


logger = logging.getLogger(__name__)


# 默认单位为每 1,000 token 的美元 blended 费率；调用方可通过 BudgetController
# 覆盖模型价格，供应商未返回用量时也使用同一套估算方法。
MODEL_COST_PER_1K = {"large": 0.03, "small": 0.01}


class LLMClient(Protocol):
    """通用 review 和 MDR review 共用的最小模型提供方接口。"""

    def review(self, prompt: str, model: str, *, max_chars: int | None = None, max_tokens: int | None = None) -> LLMResponse: ...


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int = 0,
                  pricing: dict[str, float] | None = None) -> float:
    prices = pricing or MODEL_COST_PER_1K
    rate = prices.get(model, prices.get("default", prices.get("small", 0.01)))
    return (prompt_tokens + completion_tokens) / 1000 * rate


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


class OpenAICompatibleClient:
    """面向 OneAPI 及其他兼容网关的 OpenAI Chat Completions 客户端。"""

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0,
                 pricing: dict[str, float] | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.pricing = pricing or MODEL_COST_PER_1K

    def review(self, prompt: str, model: str, *, max_chars: int | None = None, max_tokens: int | None = None) -> LLMResponse:
        """只发送受限 prompt，并把供应商用量统一转换为 LLMResponse。"""
        if max_chars is not None:
            prompt = prompt[:max_chars]
        endpoint = self.base_url if self.base_url.endswith("/chat/completions") else self.base_url + "/chat/completions"
        # MDR 要求模型直接返回 JSON；关闭思考输出，避免推理内容耗尽
        # completion token 后 message.content 为空，导致 JSON 解析失败。
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "enable_thinking": False,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        logger.info("LLM 请求开始 model=%s endpoint=%s prompt_chars=%d max_tokens=%s",
                    model, endpoint, len(prompt), max_tokens if max_tokens is not None else "default")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
            finish_reason = choice.get("finish_reason", "")
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("invalid chat-completions response") from exc
        usage = data.get("usage") or {}
        usage_known = (
            isinstance(usage, dict)
            and isinstance(usage.get("prompt_tokens"), int)
            and isinstance(usage.get("completion_tokens"), int)
            and usage.get("prompt_tokens", 0) > 0
            and usage.get("completion_tokens", 0) > 0
        )
        prompt_tokens = int(usage.get("prompt_tokens") or estimate_tokens(prompt))
        completion_tokens = int(usage.get("completion_tokens") or estimate_tokens(str(text)))
        logger.info("LLM 响应完成 model=%s finish_reason=%s content_chars=%d prompt_tokens=%d completion_tokens=%d duration_ms=%d usage_known=%s",
                    model, finish_reason, len(str(text)), prompt_tokens, completion_tokens,
                    int((time.monotonic() - started) * 1000), usage_known)
        return LLMResponse(
            text=str(text),
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=estimate_cost(model, prompt_tokens, completion_tokens, self.pricing),
            usage_known=usage_known,
        )


class DeterministicClient:
    """用于可重复测试和安全演示的离线客户端。"""

    def review(self, prompt: str, model: str, *, max_chars: int | None = None, max_tokens: int | None = None) -> LLMResponse:
        if max_chars is not None:
            prompt = prompt[:max_chars]
        # 固定输出保证离线运行可重复，并确保不会访问网络。
        if "MDR_RULE_BATCH" in prompt:
            return LLMResponse(text='{"findings": []}', model=model)
        return LLMResponse(text=f"Offline review ({model}): {prompt}", model=model)

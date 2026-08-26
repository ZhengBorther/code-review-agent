import json

from review_agent.budget import BudgetController
from review_agent.llm import DeterministicClient, OpenAICompatibleClient
from review_agent.models import RunConfig


def test_budget_selects_fallback_then_no_llm():
    controller = BudgetController(RunConfig(url="x", budget_usd=0.01, model="large", fallback_model="small"))
    assert controller.select("large", 100000).allow_llm is False
    assert controller.select("small", 100000).allow_llm is False


def test_deterministic_client_is_stable():
    response = DeterministicClient().review("inspect diff", "small")
    assert response.model == "small"
    assert "inspect diff" in response.text
    assert response.cost_usd == 0


def test_openai_compatible_client_parses_response(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "Looks good"}}], "usage": {"prompt_tokens": 12, "completion_tokens": 3}}).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    response = OpenAICompatibleClient("https://oneapi.example/v1", "key", timeout=7).review("safe prompt", "small")
    assert response.text == "Looks good"
    assert response.prompt_tokens == 12
    assert response.completion_tokens == 3
    assert captured["url"].endswith("/chat/completions")
    assert captured["body"]["model"] == "small"
    assert captured["body"]["messages"][0]["content"] == "safe prompt"
    assert captured["headers"]["Authorization"] == "Bearer key"
    assert captured["timeout"] == 7


def test_client_applies_truncate_limits(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":1,"completion_tokens":1}}'
    captured = {}
    def fake(request, timeout):
        captured["body"] = json.loads(request.data)
        return Response()
    monkeypatch.setattr("urllib.request.urlopen", fake)
    OpenAICompatibleClient("https://example/v1", "key").review("abcdefgh", "small", max_chars=4, max_tokens=2)
    assert captured["body"]["messages"][0]["content"] == "abcd"
    assert captured["body"]["max_tokens"] == 2


def test_missing_usage_is_estimated_and_marked_unknown(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"choices":[{"message":{"content":"ok"}}]}'
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: Response())
    response = OpenAICompatibleClient("https://example/v1", "key").review("abcd", "small")
    assert response.usage_known is False
    assert response.cost_usd > 0


def test_truncate_decision_has_limits():
    controller = BudgetController(RunConfig(url="x", budget_usd=0.01, model="large", fallback_model="small"))
    decision = controller.select("small", 100000, allow_truncate=True)
    assert decision.allow_llm is True
    assert decision.truncate is True
    assert decision.max_chars == decision.max_tokens * 4


def test_actual_cost_overrun_locks_budget():
    controller = BudgetController(RunConfig(url="x", budget_usd=0.01))
    assert controller.accept_response(0.02) is False
    assert controller.spent_usd == 0.01
    assert controller.select("small", 1).allow_llm is False


def test_completion_reservation_can_reject_prompt_only_budget():
    config = RunConfig(url="x", budget_usd=0.01, model="small", fallback_model="small", completion_tokens=512)
    controller = BudgetController(config)
    assert controller.select("small", 500).allow_llm is False
    assert controller.reserve("small", 400) is True
    assert controller.select("small", 400).allow_llm is False


def test_zero_usage_is_charged_using_estimate(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":0,"completion_tokens":0}}'
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: Response())
    response = OpenAICompatibleClient("https://example/v1", "key").review("abcd", "small")
    assert response.usage_known is True
    assert response.cost_usd > 0

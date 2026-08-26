import json

from review_agent.budget import BudgetController
from review_agent.llm import DeterministicClient, OpenAICompatibleClient
from review_agent.models import RunConfig


def test_budget_selects_fallback_then_no_llm():
    controller = BudgetController(RunConfig(url="x", budget_usd=0.01, model="large", fallback_model="small"))
    assert controller.select("large", 100000).model == "small"
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

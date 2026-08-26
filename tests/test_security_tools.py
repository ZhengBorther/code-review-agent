from review_agent.adapters import LocalDiffAdapter
from review_agent.models import ChangeRequest, Finding
from review_agent.security import redact_secrets
from review_agent.tools import ToolRegistry, ToolSpec


def test_redaction_hides_api_key_and_returns_match_metadata():
    result = redact_secrets("token = 'sk-test-1234567890abcdef'")
    assert "sk-test" not in result.text
    assert result.matches
    assert result.matches[0].kind == "api_key"
    assert result.matches[0].replacement == "[REDACTED:api_key]"


def test_redaction_handles_private_key_and_password():
    text = "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----\npassword = 's3cret-value'"
    result = redact_secrets(text)
    assert "BEGIN PRIVATE KEY" not in result.text
    assert "s3cret-value" not in result.text
    assert {match.kind for match in result.matches} >= {"private_key", "password"}


def test_redaction_handles_platform_tokens_and_unquoted_credentials():
    text = "ghp_abcdefghijklmnopqrstuvwxyz123456\ngho_abcdefghijklmnopqrstuvwxyz123456\nglpat-abcdefghijklmnopqrstuvwxyz123456\nxoxb-123456789012-123456789012-abcdefghijklmnop\nAuthorization: Bearer eyJabcdefghijklmnopqrstuvwxyz1234567890\npassword = s3cret-value\ntoken: abcdefghijklmnop1234"
    result = redact_secrets(text)
    assert "ghp_" not in result.text
    assert "Bearer eyJ" not in result.text
    assert "s3cret-value" not in result.text
    assert {match.kind for match in result.matches} >= {"token", "password"}


def test_tool_registry_is_declarative_and_runs_registered_tools():
    registry = ToolRegistry()
    registry.register(ToolSpec(name="constant", description="test", runner=lambda _c, _d: [], confidence="high"))
    assert [spec.name for spec in registry.specs] == ["constant"]
    request = ChangeRequest(url="local://fixture", diff="diff")
    assert registry.run_all(request, request.diff) == []


def test_tool_registry_passes_only_sanitized_diff_to_runner():
    seen = []
    registry = ToolRegistry()
    registry.register(ToolSpec(name="inspect", description="test", runner=lambda change, diff: seen.append((change.diff, diff)) or [], confidence="high"))
    request = ChangeRequest(url="local://fixture", diff="token='sk-test-1234567890abcdef'")
    registry.run_all(request, "token='[REDACTED:api_key]'")
    assert seen == [("token='[REDACTED:api_key]'", "token='[REDACTED:api_key]'")]


def test_tool_registry_rejects_invalid_confidence_at_registration():
    spec = object.__new__(ToolSpec)
    object.__setattr__(spec, "name", "bad")
    object.__setattr__(spec, "description", "bad")
    object.__setattr__(spec, "runner", lambda _c, _d: [])
    object.__setattr__(spec, "confidence", "low")
    registry = ToolRegistry()
    try:
        registry.register(spec)
    except ValueError as exc:
        assert "confidence" in str(exc)
    else:
        raise AssertionError("invalid confidence must be rejected")


def test_tool_registry_normalizes_runner_finding_confidence_to_spec():
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="advisory-rule", description="test",
        runner=lambda _c, _d: [Finding("x", "y", "high")], confidence="advisory",
    ))
    findings = registry.run_all(ChangeRequest(url="local://fixture"), "")
    assert findings[0].confidence == "advisory"


def test_builtin_tools_find_todo_and_secrets():
    registry = ToolRegistry.with_builtins()
    request = ChangeRequest(url="local://fixture", diff="+ # TODO: handle error\n+token = 'sk-test-1234567890abcdef'\n")
    findings = registry.run_all(request, request.diff)
    assert any("TODO" in finding.title for finding in findings)
    assert any("secret" in finding.title.lower() for finding in findings)
    assert all(finding.confidence == "high" for finding in findings)


def test_local_diff_adapter_reads_only_local_diff_file(tmp_path):
    diff_file = tmp_path / "change.diff"
    diff_file.write_text("diff --git a/a.py b/a.py\n+pass\n", encoding="utf-8")
    request = LocalDiffAdapter(diff_file).fetch("local://fixture")
    assert request == ChangeRequest(url="local://fixture", diff=diff_file.read_text(), source="local")


def test_local_diff_adapter_rejects_non_local_url(tmp_path):
    diff_file = tmp_path / "change.diff"
    diff_file.write_text("diff", encoding="utf-8")
    try:
        LocalDiffAdapter(diff_file).fetch("https://github.com/a/b/pull/1")
    except ValueError as exc:
        assert "local" in str(exc)
    else:
        raise AssertionError("non-local URL must be rejected")

# Code Review Agent Design

## Goal

Build a Python CLI that accepts a GitHub/GitLab pull request URL or an explicit local diff and produces an auditable Markdown code-review report. The first release is local-only for output, with interfaces ready for remote publishing later.

## Architecture

The system is a checkpointed pipeline with five stages: `fetch`, `sanitize`, `tools`, `review`, and `render`. A `ChangeRequestAdapter` provides metadata and a unified diff; the sanitizer removes secrets before any model call; registered tools produce structured findings; an injected LLM client produces advisory findings; and the renderer writes Markdown with confidence labels and trace IDs.

The CLI uses a stable `run_id` and SQLite state. Each stage stores a JSON result and is skipped when a matching successful checkpoint already exists, so an interrupted run resumes without repeating completed work. The original diff is retained locally and referenced by SHA-256 rather than sent to the model when it contains secrets.

## Components

- `review_agent/cli.py`: command parsing, configuration, and exit codes.
- `review_agent/adapters.py`: `ChangeRequestAdapter` protocol plus local diff implementation; GitHub/GitLab implementations can be added without changing orchestration.
- `review_agent/pipeline.py`: stage ordering, checkpoint resume, budget decisions, and result aggregation.
- `review_agent/tools.py`: `ToolSpec`, registry, and built-in safe analyzers. New tools are registered declaratively.
- `review_agent/security.py`: secret detection and deterministic redaction.
- `review_agent/llm.py`: `LLMClient` protocol, OneAPI-compatible OpenAI client, and deterministic offline client.
- `review_agent/storage.py`: SQLite schema and atomic checkpoint/trace operations.
- `review_agent/report.py`: deterministic Markdown rendering.

## Interfaces

```python
class ChangeRequestAdapter(Protocol):
    def fetch(self, url: str) -> ChangeRequest: ...

class LLMClient(Protocol):
    def review(self, *, prompt: str, model: str) -> LLMResponse: ...

@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    runner: Callable[[ChangeRequest, str], list[Finding]]
    confidence: Literal["high", "advisory"]
```

`Finding` includes `title`, `body`, optional file/line location, confidence, evidence, and trace ID. `LLMResponse` includes text plus prompt/completion token counts and estimated cost.

## Persistence and Recovery

SQLite tables are `runs`, `checkpoints`, and `traces`. A run stores URL, configuration snapshot, budget, accumulated cost, and status. A checkpoint stores stage, status, JSON output, and timestamps. A trace stores tool name, input/diff hash, exact prompt and model reply (with secrets already redacted), model, usage, cost, duration, and errors. Checkpoint writes occur in transactions; a failed stage is retried on the next invocation.

## Budget and Model Degradation

`--budget-usd` defaults to a small explicit value and is enforced before each LLM call. When the estimated call would exceed the remaining budget, the pipeline switches to `fallback_model`, then truncates the sanitized diff to a configured character limit, and finally disables LLM review while retaining deterministic findings. The report includes the actual spend and any degradation reason. Network retries have a bounded count and each attempt has its own trace.

## Security Boundaries

Secret detection covers common API keys/tokens, private keys, password assignments, and high-entropy values. Matches are replaced with stable placeholders before prompts are built. Original diffs stay on disk under the state directory and are never included in LLM request payloads after redaction. No repository command is executed by default. Any future command tool must declare an executable allowlist, working directory, and timeout; undeclared commands are rejected.

## Reporting

The Markdown report contains run metadata, summary counts, budget usage, degradation notes, and findings grouped by confidence. High-confidence findings cite a deterministic rule or test evidence and are labeled directly actionable. Advisory findings are labeled for human review. Every finding links to exactly one trace ID, and the trace section lists tool, prompt, response, and input hash.

## Testing

Unit and integration tests cover checkpoint resume after interruption, budget fallback/truncation/no-LLM behavior, secret redaction, declarative tool registration, trace completeness, confidence rendering, OneAPI response parsing, and an offline end-to-end CLI run using a fixture diff. Tests never execute code from the fixture repository and never require network access.

## Non-goals for v1

- Posting comments or review states back to GitHub/GitLab.
- Executing arbitrary repository tests, builds, or shell commands.
- Multi-user server deployment or a distributed task queue.

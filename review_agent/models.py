"""Immutable domain objects shared by the review pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal, TypeVar

Confidence = Literal["high", "advisory"]
_Model = TypeVar("_Model")


class _Serializable:
    """Small explicit serialization API used by SQLite JSON columns."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls: type[_Model], value: dict[str, Any]) -> _Model:
        names = {item.name for item in fields(cls)}
        return cls(**{key: val for key, val in value.items() if key in names})


@dataclass(frozen=True)
class ChangeRequest(_Serializable):
    url: str
    title: str = ""
    author: str = ""
    diff: str = ""
    source: str = "local"
    base_ref: str = ""
    head_ref: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Finding(_Serializable):
    title: str
    body: str
    confidence: Confidence
    evidence: str = ""
    trace_id: str = ""
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None

    def __post_init__(self) -> None:
        if self.confidence not in ("high", "advisory"):
            raise ValueError("confidence must be 'high' or 'advisory'")


@dataclass(frozen=True)
class TraceRecord(_Serializable):
    trace_id: str
    run_id: str
    kind: str
    input_hash: str = ""
    prompt: str = ""
    response: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    tool_name: str = ""
    error: str = ""


@dataclass(frozen=True)
class LLMResponse(_Serializable):
    text: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class RunConfig(_Serializable):
    url: str
    budget_usd: float = 1.0
    model: str = "large"
    fallback_model: str = "small"
    offline: bool = False
    max_diff_chars: int = 12000
    output_path: str = "review.md"
    state_dir: str = ".review-state"


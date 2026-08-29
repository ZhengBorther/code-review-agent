"""LangGraph 编排入口。

SQLite 仍是恢复事实来源；LangGraph 负责固定阶段的可观察编排，不提供 fallback，
从而保证所有 CLI 执行都经过同一条状态图路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict


class ReviewState(TypedDict, total=False):
    target: str
    result: Any
    prepared: bool
    delivered: bool


NODE_NAMES = (
    "prepare", "review_mdr_pipeline", "deliver",
)


@dataclass
class ReviewGraphAdapter:
    """通过编译后的 LangGraph 运行 Review Pipeline。"""

    pipeline: Any
    graph: Any = None

    @property
    def node_names(self) -> tuple[str, ...]:
        return NODE_NAMES

    def run(self, url_or_run_id: str) -> Any:
        state = self.graph.invoke({"target": url_or_run_id})
        return state["result"]


def build_review_graph(pipeline: Any) -> ReviewGraphAdapter:
    """构建唯一的固定阶段图；缺少 LangGraph 时启动即失败。"""
    from langgraph.graph import END, START, StateGraph

    graph_builder = StateGraph(ReviewState)

    def prepare(state: ReviewState) -> ReviewState:
        target = str(state.get("target", "")).strip()
        if not target:
            raise ValueError("review target is required")
        return {"target": target, "prepared": True}

    def execute(state: ReviewState) -> ReviewState:
        return {"target": state["target"], "result": pipeline.run(state["target"])}

    def deliver(state: ReviewState) -> ReviewState:
        if "result" not in state:
            raise RuntimeError("review pipeline produced no result")
        return {"delivered": True}

    # SQLite-aware Pipeline owns the detailed stage checkpoints. LangGraph
    # intentionally models coarse orchestration instead of fake pass-through
    # nodes that pretend to execute fetch/sanitize independently.
    graph_builder.add_node("prepare", prepare)
    graph_builder.add_node("review_mdr_pipeline", execute)
    graph_builder.add_node("deliver", deliver)
    graph_builder.add_edge(START, "prepare")
    for previous, current in zip(NODE_NAMES, NODE_NAMES[1:]):
        graph_builder.add_edge(previous, current)
    graph_builder.add_edge("deliver", END)
    return ReviewGraphAdapter(pipeline, graph_builder.compile())

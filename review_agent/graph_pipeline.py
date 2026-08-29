"""可选的 LangGraph 编排外壳。

SQLite 仍是恢复事实来源；本模块只把固定阶段声明成可观察的图，并在未安装
LangGraph 时回退到已有 ReviewPipeline，避免把框架变成运行时硬依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict


class ReviewState(TypedDict, total=False):
    target: str
    result: Any


NODE_NAMES = (
    "fetch", "sanitize", "tools", "load_rules", "split_languages",
    "review_mdr_batches", "render",
)


@dataclass
class ReviewGraphAdapter:
    """以统一接口运行 LangGraph 图或现有 Pipeline fallback。"""

    pipeline: Any
    graph: Any = None

    @property
    def node_names(self) -> tuple[str, ...]:
        return NODE_NAMES

    def run(self, url_or_run_id: str) -> Any:
        if self.graph is None:
            return self.pipeline.run(url_or_run_id)
        state = self.graph.invoke({"target": url_or_run_id})
        return state["result"]


def build_review_graph(pipeline: Any) -> ReviewGraphAdapter:
    """构建固定阶段图；框架不可用时返回行为等价的 fallback。"""
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        return ReviewGraphAdapter(pipeline)

    graph_builder = StateGraph(ReviewState)

    # 每个节点保留显式名称，实际 checkpoint 仍由 pipeline/SQLite 管理。
    def passthrough(state: ReviewState) -> ReviewState:
        return state

    for name in NODE_NAMES[:-1]:
        graph_builder.add_node(name, passthrough)

    def execute(state: ReviewState) -> ReviewState:
        return {"target": state["target"], "result": pipeline.run(state["target"])}

    graph_builder.add_node("render", execute)
    graph_builder.add_edge(START, "fetch")
    for previous, current in zip(NODE_NAMES, NODE_NAMES[1:]):
        graph_builder.add_edge(previous, current)
    graph_builder.add_edge("render", END)
    return ReviewGraphAdapter(pipeline, graph_builder.compile())

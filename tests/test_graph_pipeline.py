from review_agent.graph_pipeline import NODE_NAMES, build_review_graph


class _Pipeline:
    def run(self, target):
        return target


def test_graph_declares_explicit_review_nodes_and_preserves_pipeline_result():
    adapter = build_review_graph(_Pipeline())
    assert adapter.node_names == ("prepare", "review_mdr_pipeline", "deliver")
    assert adapter.graph is not None
    assert adapter.run("run-1") == "run-1"

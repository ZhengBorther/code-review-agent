from review_agent.graph_pipeline import build_review_graph


class _Pipeline:
    def run(self, target):
        return target


def test_graph_declares_explicit_review_nodes_and_preserves_pipeline_result():
    adapter = build_review_graph(_Pipeline())
    assert adapter.node_names == (
        "fetch", "sanitize", "tools", "load_rules", "split_languages",
        "review_mdr_batches", "render",
    )
    assert adapter.run("run-1") == "run-1"

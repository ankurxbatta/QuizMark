from app.core.config import settings
from app.services.reranker import rerank_results


def _ids(docs):
    return [d["_id"] for d in docs]


def test_disabled_flag_is_noop(monkeypatch):
    monkeypatch.setattr(settings, "RERANK_ENABLED", False)
    docs = [{"_id": "a", "text": "standard deviation"}, {"_id": "b", "text": "unrelated"}]
    assert rerank_results("text", "standard deviation", docs) is docs


def test_short_list_is_noop():
    docs = [{"_id": "a", "text": "anything"}]
    assert rerank_results("text", "query", docs) is docs


def test_zero_lexical_signal_keeps_vector_order():
    docs = [
        {"_id": "a", "text": "alpha beta"},
        {"_id": "b", "text": "gamma delta"},
        {"_id": "c", "text": "epsilon zeta"},
    ]
    assert _ids(rerank_results("text", "zzz qqq", docs)) == ["a", "b", "c"]


def test_text_overlap_promotes_matching_chunk():
    docs = [
        {"_id": "a", "text": "the mean of a data set"},
        {"_id": "b", "text": "computing the sample standard deviation step by step"},
        {"_id": "c", "text": "probability basics"},
    ]
    reranked = rerank_results("text", "how to compute the sample standard deviation", docs)
    assert reranked[0]["_id"] == "b"


def test_formula_math_vocab_boost():
    docs = [
        {"_id": "a", "latex": "P(A \\cup B)", "formula_plain": "probability of union"},
        {"_id": "b", "latex": "s = \\sqrt{\\sum(x-\\bar{x})^2/(n-1)}",
         "formula_plain": "sample standard deviation", "concept_name": "standard deviation"},
    ]
    reranked = rerank_results("formula", "standard deviation formula", docs)
    assert reranked[0]["_id"] == "b"


def test_figure_kind_boost():
    docs = [
        {"_id": "a", "kind": "pie", "caption": "Figure 4.1", "description": "share of budget"},
        {"_id": "b", "kind": "histogram", "caption": "Figure 4.2", "description": "exam scores"},
    ]
    reranked = rerank_results("figure", "histogram of exam scores", docs)
    assert reranked[0]["_id"] == "b"


def test_table_header_overlap():
    docs = [
        {"_id": "a", "table_summary": "city populations", "headers": ["city", "population"]},
        {"_id": "b", "table_summary": "frequency distribution of scores",
         "headers": ["class", "frequency", "midpoint"]},
    ]
    reranked = rerank_results("table", "frequency distribution table with midpoints", docs)
    assert reranked[0]["_id"] == "b"


def test_deterministic():
    docs = [
        {"_id": "a", "text": "variance and deviation"},
        {"_id": "b", "text": "deviation of samples"},
        {"_id": "c", "text": "nothing relevant"},
    ]
    first = _ids(rerank_results("text", "deviation", docs))
    for _ in range(5):
        assert _ids(rerank_results("text", "deviation", docs)) == first

from app.services.retrieval_eval import (
    aggregate,
    doc_matches,
    hit_at_k,
    judge,
    mrr,
    ndcg_at_k,
    recall_at_k,
    score_query,
)


# ── metrics ────────────────────────────────────────────────────────────────────

def test_mrr_first_hit_position():
    assert mrr([True, False]) == 1.0
    assert mrr([False, True, False]) == 0.5
    assert mrr([False, False, False]) == 0.0
    assert mrr([]) == 0.0


def test_hit_at_k():
    assert hit_at_k([False, False, True], 3) == 1.0
    assert hit_at_k([False, False, True], 2) == 0.0
    assert hit_at_k([], 5) == 0.0


def test_recall_at_k():
    assert recall_at_k([True, True, False], total_relevant=4, k=3) == 0.5
    assert recall_at_k([True], total_relevant=0, k=1) == 0.0


def test_ndcg_perfect_and_worst():
    assert ndcg_at_k([True, True, False, False], 4) == 1.0
    assert ndcg_at_k([False, False, False], 3) == 0.0
    # one relevant doc at rank 2 of 2: dcg = 1/log2(3), ideal = 1/log2(2)
    val = ndcg_at_k([False, True], 2)
    assert 0.0 < val < 1.0


# ── relevance judgement ────────────────────────────────────────────────────────

def test_doc_matches_by_page_range():
    chunk = {"page_start": 10, "page_end": 14, "text": "irrelevant"}
    assert doc_matches(chunk, {"pages": [12]})
    assert not doc_matches(chunk, {"pages": [15]})


def test_doc_matches_specialist_page_field():
    formula = {"page": 42, "latex": "x"}
    assert doc_matches(formula, {"pages": [42]})


def test_doc_matches_by_substring_any_field():
    fig = {"kind": "histogram", "caption": "Figure 3.1", "description": "exam scores"}
    assert doc_matches(fig, {"contains": ["Exam Scores"]})  # case-insensitive
    assert not doc_matches(fig, {"contains": ["boxplot"]})


def test_doc_matches_empty_expect_is_false():
    assert not doc_matches({"text": "anything"}, {})


def test_judge_maps_docs():
    docs = [{"page": 1}, {"page": 2}]
    assert judge(docs, {"pages": [2]}) == [False, True]


# ── scoring / aggregation ──────────────────────────────────────────────────────

def test_score_query_and_aggregate():
    fused_lists = {
        "text": [{"text": "standard deviation explained", "page_start": 1, "page_end": 1}],
        "formula": [],
    }
    expect = {"contains": ["standard deviation"]}
    scores = score_query(fused_lists, expect, k=8)
    assert scores["text"]["hit@k"] == 1.0
    assert scores["text"]["mrr"] == 1.0
    assert scores["formula"]["hit@k"] == 0.0

    agg = aggregate([scores, scores])
    assert agg["text"]["hit@k"] == 1.0
    assert agg["text"]["queries"] == 2
    assert agg["formula"]["mrr"] == 0.0

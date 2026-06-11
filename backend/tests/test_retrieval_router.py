from app.services.retrieval_router import (
    INTENT_COMPUTATIONAL,
    INTENT_CONCEPTUAL,
    INTENT_VISUAL,
    FusedContext,
    classify_intent,
    expand_to_parent_chunks,
    rrf_fuse,
)


# ── Intent classification ───────────────────────────────────────────────────────

def test_computational_intent():
    assert classify_intent("how do I calculate the standard deviation") == INTENT_COMPUTATIONAL
    assert classify_intent("formula for the confidence interval") == INTENT_COMPUTATIONAL
    assert classify_intent("z-score of an observation") == INTENT_COMPUTATIONAL


def test_visual_intent():
    assert classify_intent("interpret the histogram of exam scores") == INTENT_VISUAL
    assert classify_intent("what trend does the scatter plot show") == INTENT_VISUAL
    assert classify_intent("read the frequency table") == INTENT_VISUAL


def test_visual_wins_over_computational():
    assert classify_intent("calculate the mean from the histogram") == INTENT_VISUAL


def test_conceptual_fallback():
    assert classify_intent("define a representative sample") == INTENT_CONCEPTUAL
    assert classify_intent("") == INTENT_CONCEPTUAL


# ── RRF fusion ──────────────────────────────────────────────────────────────────

def _doc(i):
    return {"_id": str(i), "text": f"doc {i}"}


def test_rrf_doc_in_both_lists_wins():
    list_a = [_doc(1), _doc(2), _doc(3)]
    list_b = [_doc(4), _doc(2), _doc(5)]
    fused = rrf_fuse([list_a, list_b], k_const=60)
    assert fused[0]["_id"] == "2"  # appears in both lists → highest fused score


def test_rrf_preserves_rank_within_single_list():
    fused = rrf_fuse([[_doc(1), _doc(2), _doc(3)]], k_const=60)
    assert [d["_id"] for d in fused] == ["1", "2", "3"]


def test_rrf_dedupes_and_keeps_first_copy():
    a = {"_id": "1", "text": "first copy"}
    b = {"_id": "1", "text": "second copy"}
    fused = rrf_fuse([[a], [b]], k_const=60)
    assert len(fused) == 1
    assert fused[0]["text"] == "first copy"


def test_rrf_empty_input():
    assert rrf_fuse([]) == []
    assert rrf_fuse([[], []]) == []


def test_rrf_ignores_docs_without_id():
    fused = rrf_fuse([[{"text": "no id"}, _doc(1)]], k_const=60)
    assert len(fused) == 1


# ── FusedContext rendering ──────────────────────────────────────────────────────

def test_to_prompt_combines_sections():
    ctx = FusedContext(
        text_chunks=[{"chapter_title": "Ch1", "section_title": "Mean", "text": "The mean is..."}],
        formulas=[{"concept_label": "mean", "formula_latex": "x_bar = sum(x)/n",
                   "context_sentence": ""}],
        figures=[{"figure_kind": "histogram", "caption": "Figure 1.1", "axis_summary": "",
                  "description": "A histogram.", "chapter_num": 1, "page": 5}],
        tables=[],
    )
    prompt = ctx.to_prompt()
    assert "[TEXTBOOK 1: Ch1 — Mean]" in prompt
    assert "KEY FORMULAS" in prompt
    assert "FIGURES FROM THE TEXTBOOK" in prompt
    assert "TABLES FROM THE TEXTBOOK" not in prompt  # empty section omitted


def test_to_prompt_empty_context():
    assert FusedContext().to_prompt() == ""


# ── Cross-link expansion (mongomock-backed) ─────────────────────────────────────

async def test_expansion_fetches_unknown_parents(mock_db, monkeypatch):
    from app.services import mongo_vector_store

    async def fake_get_collection(name):
        return mock_db[name]

    monkeypatch.setattr(
        "app.services.retrieval_router._get_collection", fake_get_collection
    )
    await mock_db[mongo_vector_store.CHUNKS_COLLECTION].insert_one(
        {"_id": "c1", "text": "parent chunk", "embedding": [0.1]}
    )

    parents = await expand_to_parent_chunks(
        [{"_id": "f1", "parent_chunk_id": "c1"},
         {"_id": "f2", "parent_chunk_id": "missing"}],
        known_chunk_ids=set(),
        limit=2,
    )
    assert len(parents) == 1
    assert parents[0]["_id"] == "c1"
    assert "embedding" not in parents[0]


async def test_expansion_skips_known_and_respects_limit(mock_db, monkeypatch):
    from app.services import mongo_vector_store

    async def fake_get_collection(name):
        return mock_db[name]

    monkeypatch.setattr(
        "app.services.retrieval_router._get_collection", fake_get_collection
    )
    for i in range(3):
        await mock_db[mongo_vector_store.CHUNKS_COLLECTION].insert_one(
            {"_id": f"c{i}", "text": f"chunk {i}"}
        )

    specialist = [{"_id": f"s{i}", "parent_chunk_id": f"c{i}"} for i in range(3)]
    parents = await expand_to_parent_chunks(specialist, known_chunk_ids={"c0"}, limit=1)
    assert len(parents) == 1
    assert parents[0]["_id"] == "c1"  # c0 known, limit stops after one fetch

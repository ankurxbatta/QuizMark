"""
Insert-time bank de-duplication (ingest_tasks._drop_bank_duplicates).

Ensures generated questions that are near-duplicates of ones ALREADY stored for
the book are dropped, so separate generation runs don't accumulate equivalent
questions. Pure-function tests — no DB / network.
"""
from app.tasks.ingest_tasks import _drop_bank_duplicates, _cosine

THRESHOLD = 0.92


def _q(text: str) -> dict:
    return {"question_text": text, "question_type": "mcq"}


def test_drops_near_duplicate_of_bank():
    bank = [[1.0, 0.0, 0.0]]
    qs = [_q("dup"), _q("distinct")]
    embs = [[1.0, 0.01, 0.0], [0.0, 1.0, 0.0]]  # first ~identical to bank, second orthogonal
    kept_q, kept_e, dropped = _drop_bank_duplicates(qs, embs, bank, THRESHOLD)
    assert dropped == 1
    assert [q["question_text"] for q in kept_q] == ["distinct"]
    assert len(kept_e) == 1


def test_empty_bank_keeps_everything():
    qs = [_q("a"), _q("b")]
    embs = [[1.0, 0.0], [0.0, 1.0]]
    kept_q, kept_e, dropped = _drop_bank_duplicates(qs, embs, [], THRESHOLD)
    assert dropped == 0
    assert len(kept_q) == 2


def test_bank_with_only_none_embeddings_is_noop():
    qs = [_q("a")]
    embs = [[1.0, 0.0]]
    kept_q, _, dropped = _drop_bank_duplicates(qs, embs, [None, []], THRESHOLD)
    assert dropped == 0
    assert len(kept_q) == 1


def test_question_without_embedding_is_kept():
    bank = [[1.0, 0.0]]
    qs = [_q("no-embed")]
    embs = [None]  # can't compare → keep
    kept_q, _, dropped = _drop_bank_duplicates(qs, embs, bank, THRESHOLD)
    assert dropped == 0
    assert len(kept_q) == 1


def test_below_threshold_is_kept():
    bank = [[1.0, 0.0]]
    qs = [_q("somewhat-similar")]
    # cosine here is well under 0.92
    embs = [[0.7, 0.7]]
    assert _cosine(embs[0], bank[0]) < THRESHOLD
    kept_q, _, dropped = _drop_bank_duplicates(qs, embs, bank, THRESHOLD)
    assert dropped == 0
    assert len(kept_q) == 1

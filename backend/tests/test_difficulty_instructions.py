"""Tests for difficulty-level differentiation in question generation.

Covers:
  - _DIFFICULTY_INSTRUCTION easy/medium/hard are distinct and carry the
    intended, mutually-exclusive language.
  - _BLOOM_LEVEL_INSTRUCTIONS["L5"] demands multi-step / evaluate work.
  - _is_trivial_recall conservative guard behaviour.
  - The difficulty-stamping + bloom-band logic used by _generate_from_chunk.
"""
from app.services.question_generator import (
    _DIFFICULTY_INSTRUCTION,
    _BLOOM_LEVEL_INSTRUCTIONS,
    _is_trivial_recall,
)


# ── _DIFFICULTY_INSTRUCTION are distinct ─────────────────────────────────────

def test_difficulty_instructions_present():
    for key in ("easy", "medium", "hard"):
        assert key in _DIFFICULTY_INSTRUCTION
        assert _DIFFICULTY_INSTRUCTION[key].strip()


def test_difficulty_instructions_are_distinct():
    texts = [_DIFFICULTY_INSTRUCTION[k] for k in ("easy", "medium", "hard")]
    assert len(set(texts)) == 3


def test_easy_mentions_single_and_recall():
    easy = _DIFFICULTY_INSTRUCTION["easy"].lower()
    assert "single" in easy
    assert "one step" in easy or "one-step" in easy or "single step" in easy
    assert "recall" in easy
    # easy must forbid the higher-order work
    assert "calculation" in easy or "calculate" in easy


def test_medium_mentions_apply_or_interpret():
    medium = _DIFFICULTY_INSTRUCTION["medium"].lower()
    assert "apply" in medium or "applying" in medium
    assert "interpret" in medium or "interpreting" in medium


def test_hard_mentions_multistep_combine_evaluate():
    hard = _DIFFICULTY_INSTRUCTION["hard"].lower()
    assert "multi-step" in hard or "multi step" in hard or "chained" in hard or "two or more" in hard
    assert "two or more" in hard or "2 or more" in hard or "two" in hard or "combine" in hard
    assert "combine" in hard
    assert "evaluate" in hard
    # hard must explicitly forbid single-fact / one-line recall answers
    assert "single recalled fact" in hard or "one-line lookup" in hard or "one step" in hard


def test_hard_distinct_from_easy_language():
    easy = _DIFFICULTY_INSTRUCTION["easy"].lower()
    hard = _DIFFICULTY_INSTRUCTION["hard"].lower()
    # core differentiators
    assert "single" in easy and "multi-step" in hard or "two or more" in hard


# ── L5 Bloom instruction strength ────────────────────────────────────────────

def test_l5_demands_multistep_or_evaluate():
    l5 = _BLOOM_LEVEL_INSTRUCTIONS["L5"].lower()
    assert "evaluate" in l5
    assert (
        "two or more" in l5
        or "multi-step" in l5
        or "chain" in l5
        or "combine" in l5
    )
    assert "single recalled fact" in l5 or "single recall" in l5 or "one isolated computation" in l5


# ── _is_trivial_recall guard ─────────────────────────────────────────────────

def test_trivial_recall_flags_short_factual_stem():
    assert _is_trivial_recall("True/False: the area under a pdf equals one.", "L1")


def test_trivial_recall_empty_is_trivial():
    assert _is_trivial_recall("", "L5")
    assert _is_trivial_recall("   ", "")


def test_trivial_recall_spares_numeric_scenario():
    assert not _is_trivial_recall("A sample of 60 has mean 4.1 defects; find P(0).", "L1")


def test_trivial_recall_spares_scenario_cue():
    assert not _is_trivial_recall("Evaluate whether the Poisson model is justified.", "L1")


def test_trivial_recall_spares_long_stem():
    long_q = (
        "First derive the expression and then explain how the assumption of "
        "independence changes the resulting interpretation for the model overall here."
    )
    assert not _is_trivial_recall(long_q, "")


def test_trivial_recall_spares_higher_bloom():
    # bloom says L5 → not treated as trivial even if short and number-free
    assert not _is_trivial_recall("Critique the stated method here please.", "L5")


# ── difficulty stamping + bloom band logic (mirrors _generate_from_chunk) ─────

_BAND = {"easy": {"L1", "L2"}, "medium": {"L3", "L4"}, "hard": {"L5"}}
_DEFAULT_BLOOM = {"easy": "L2", "medium": "L3", "hard": "L5"}


def _apply_band(questions, difficulty):
    """Replicates the in-band stamping done in _generate_from_chunk."""
    band = _BAND[difficulty]
    default_bloom = _DEFAULT_BLOOM[difficulty]
    for q in questions:
        q["difficulty"] = difficulty
        if str(q.get("bloom_level", "")).upper() not in band:
            q["bloom_level"] = default_bloom
    return questions


def test_band_corrects_out_of_band_bloom():
    qs = [{"question_text": "x", "bloom_level": "L1"}]
    _apply_band(qs, "hard")
    assert qs[0]["difficulty"] == "hard"
    assert qs[0]["bloom_level"] == "L5"


def test_band_keeps_in_band_bloom():
    qs = [{"question_text": "x", "bloom_level": "L4"}]
    _apply_band(qs, "medium")
    assert qs[0]["bloom_level"] == "L4"
    assert qs[0]["difficulty"] == "medium"


def test_band_easy_defaults_when_missing():
    qs = [{"question_text": "x"}]
    _apply_band(qs, "easy")
    assert qs[0]["bloom_level"] == "L2"
    assert qs[0]["difficulty"] == "easy"

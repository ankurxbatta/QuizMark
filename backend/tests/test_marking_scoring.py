"""
Unit tests for the objective (MCQ / true-false) fast-path normalisation and the
LLM-mark floor in rag_pipeline, plus token-based keyword coverage in pre_scorer.

These cover the audited defects:
  • MCQ / true-false answers submitted as option text or decorated letters must
    not be spuriously zeroed.
  • A negative LLM mark must be floored at 0.
  • Keyword coverage must be word/token based, not substring.
"""
import pytest

from app.services.rag_pipeline import (
    _normalize_tf,
    _normalize_mcq_letter,
    _parse_mcq_options,
    _objective_is_correct,
    _parse_llm_json,
)
from app.services.pre_scorer import _keyword_coverage


# ── true/false normalisation ──────────────────────────────────────────────────
@pytest.mark.parametrize("val,expected", [
    ("True", True), ("true", True), ("T", True), ("t", True),
    ("yes", True), (" TRUE. ", True),
    ("False", False), ("false", False), ("F", False), ("f", False),
    ("no", False), (" false ", False),
    ("maybe", None), ("", None), (None, None),
])
def test_normalize_tf(val, expected):
    assert _normalize_tf(val) == expected


def test_tf_word_vs_letter_match():
    q = {"question_text": "The mean equals the median in a symmetric distribution."}
    # student submits letter, key is the word (and vice versa)
    assert _objective_is_correct("T", "True", "true_false", q) is True
    assert _objective_is_correct("true", "T", "true_false", q) is True
    assert _objective_is_correct("F", "True", "true_false", q) is False
    assert _objective_is_correct("no", "False", "true_false", q) is True


# ── MCQ letter normalisation ──────────────────────────────────────────────────
@pytest.mark.parametrize("val,expected", [
    ("B", "B"), ("b", "B"), ("B)", "B"), ("(B)", "B"), ("B.", "B"),
    ("B - something", "B"), ("  c ", "C"), ("[D]", "D"),
    ("The mean", None), ("", None), (None, None),
])
def test_normalize_mcq_letter_plain(val, expected):
    assert _normalize_mcq_letter(val) == expected


def test_parse_mcq_options():
    stem = (
        "What does the cdf give?\n"
        "A. The slope of the pdf\n"
        "B. The area under the pdf up to x\n"
        "C. The mode\n"
        "D. The variance"
    )
    opts = _parse_mcq_options(stem)
    assert opts["A"] == "The slope of the pdf"
    assert opts["B"] == "The area under the pdf up to x"
    assert set(opts) == {"A", "B", "C", "D"}


def test_mcq_option_text_matches_letter():
    q = {
        "question_text": (
            "What does the cdf give?\n"
            "A. The slope of the pdf\n"
            "B. The area under the pdf up to x\n"
            "C. The mode\n"
            "D. The variance"
        )
    }
    # student typed the option TEXT rather than the bare letter
    assert _objective_is_correct("The area under the pdf up to x", "B", "mcq", q) is True
    # "B) The area..." (letter + text) also resolves to B
    assert _objective_is_correct("B) The area under the pdf up to x", "B", "mcq", q) is True
    # decorated letter
    assert _objective_is_correct("(B)", "B", "mcq", q) is True
    # wrong option text
    assert _objective_is_correct("The slope of the pdf", "B", "mcq", q) is False


def test_mcq_bare_letter_still_works():
    q = {"question_text": "A. one\nB. two\nC. three\nD. four"}
    assert _objective_is_correct("B", "B", "mcq", q) is True
    assert _objective_is_correct("A", "B", "mcq", q) is False


# ── LLM mark floor ────────────────────────────────────────────────────────────
def test_llm_mark_floored_at_zero():
    res = _parse_llm_json('{"mark": -3, "feedback": "x", "flagged": false, "confidence": 0.9}', 5.0)
    assert res["mark"] == 0.0


def test_llm_mark_capped_at_max():
    res = _parse_llm_json('{"mark": 99, "feedback": "x", "flagged": false, "confidence": 0.9}', 5.0)
    assert res["mark"] == 5.0


def test_llm_mark_normal_passthrough():
    res = _parse_llm_json('{"mark": 3.5, "feedback": "ok", "flagged": false, "confidence": 0.8}', 5.0)
    assert res["mark"] == 3.5


# ── token-based keyword coverage ──────────────────────────────────────────────
def test_keyword_coverage_is_token_based_not_substring():
    # "var" must NOT match inside "variance"; "mean" must NOT match "meaning".
    coverage = _keyword_coverage("the variance has meaning", ["var", "mean"])
    assert coverage == 0.0


def test_keyword_coverage_counts_whole_tokens():
    coverage = _keyword_coverage("we compute the mean and the variance", ["mean", "variance"])
    assert coverage == 1.0


def test_keyword_coverage_empty_keywords():
    assert _keyword_coverage("anything", []) == 0.5

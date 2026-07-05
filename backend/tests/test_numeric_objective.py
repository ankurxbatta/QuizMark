"""
Numeric correctness for objective (mcq / true_false) generated questions.

The deterministic marking fast path trusts the stored correct_answer as ground
truth, but the generating LLM sometimes picks/asserts a numerically wrong value
(e.g. a Poisson probability). These passes reuse the extract+evaluate mechanism
in answer_verifier — the number always comes from Python, never the LLM — to:
  (a) re-point correct_answer at the option matching the computed value,
  (b) overwrite the marked option's text when NO option matches, and
  (c) flip a True/False key when its boolean contradicts the computed comparison.

The LLM extraction step is monkeypatched to return canned JSON, so no network.
"""
import json

import pytest

from app.services import answer_verifier
from app.services.question_generator import _split_mcq_text


def _patch_generate(monkeypatch, response: str):
    async def fake_generate(prompt: str) -> str:
        return response

    monkeypatch.setattr(answer_verifier.llm_service, "generate", fake_generate)


# ── (a) MCQ: wrong correct_answer re-pointed to the matching option ─────────────

def test_mcq_wrong_key_repointed_to_matching_option(monkeypatch):
    # Geometric: defect rate 0.01, first defect on 5th: (1-0.01)**4 * 0.01 ≈ 0.0096.
    # The matching option is C, but the question wrongly marks B.
    q = {
        "question_type": "mcq",
        "question_text": (
            "A process has defect rate 0.01. What is the probability the first "
            "defect occurs on the 5th item?\n"
            "A. 0.01\nB. 0.05\nC. 0.0096\nD. 0.99"
        ),
        "correct_answer": "B",
        "model_answer": "B. 0.05",
    }
    _patch_generate(monkeypatch, json.dumps({
        "has_computation": True,
        "expression": "(1 - 0.01)**4 * 0.01",
    }))

    import asyncio
    asyncio.run(answer_verifier.verify_objective_numeric([q]))

    assert q["correct_answer"] == "C"
    assert q["model_answer"].startswith("C")
    # Question text structure is preserved.
    _, options = _split_mcq_text(q["question_text"])
    assert set(options) == {"A", "B", "C", "D"}


# ── (b) MCQ: no option matches → overwrite the marked option's text ─────────────

def test_mcq_no_match_overwrites_marked_option(monkeypatch):
    # Poisson lambda=10, P(X=3) ≈ 0.00757 — NONE of the options is close.
    q = {
        "question_type": "mcq",
        "question_text": (
            "Emails arrive at an average of 10/day. P(exactly 3 in a day)?\n"
            "A. 0.045\nB. 0.125\nC. 0.224\nD. 0.367"
        ),
        "correct_answer": "B",
        "model_answer": "B. 0.125",
    }
    _patch_generate(monkeypatch, json.dumps({
        "has_computation": True,
        "expression": "10**3 * exp(-10) / factorial(3)",
    }))

    import asyncio
    asyncio.run(answer_verifier.verify_objective_numeric([q]))

    # correct_answer stays pointing at B, but B's text is now the computed value.
    assert q["correct_answer"] == "B"
    _, options = _split_mcq_text(q["question_text"])
    assert set(options) == {"A", "B", "C", "D"}
    # The marked option B should now equal the computed value ≈ 0.00757.
    b_val = answer_verifier._option_value(options["B"])
    assert b_val is not None
    assert abs(b_val - 0.00757) < 1e-4
    # The other options are untouched.
    assert options["A"] == "0.045"
    assert options["C"] == "0.224"
    assert options["D"] == "0.367"
    assert q["model_answer"].startswith("B")


# ── (c) True/False: contradicting boolean flipped to the computed comparison ────

def test_tf_contradiction_flipped(monkeypatch):
    # P(X=3) for lambda=5 ≈ 0.1404, which IS greater than 0.1 → statement True,
    # but the generated key wrongly says False.
    q = {
        "question_type": "true_false",
        "question_text": (
            "For a Poisson distribution with lambda=5, P(X=3) results in a value "
            "greater than 0.1."
        ),
        "correct_answer": "False",
        "model_answer": "False. Although P(X=3) ≈ 0.1404, which is greater than 0.1.",
    }
    _patch_generate(monkeypatch, json.dumps({
        "has_computation": True,
        "expression": "5**3 * exp(-5) / factorial(3)",
        "operator": "gt",
        "threshold": 0.1,
    }))

    import asyncio
    asyncio.run(answer_verifier.verify_objective_numeric([q]))

    assert q["correct_answer"] == "True"
    assert q["model_answer"].startswith("True")


def test_tf_correct_key_left_unchanged(monkeypatch):
    # 5**3 * exp(-5)/factorial(3) ≈ 0.1404 < 0.2 is True, key already True → no-op.
    q = {
        "question_type": "true_false",
        "question_text": "For lambda=5, P(X=3) is less than 0.2.",
        "correct_answer": "True",
        "model_answer": "True. P(X=3) ≈ 0.1404 < 0.2.",
    }
    _patch_generate(monkeypatch, json.dumps({
        "has_computation": True,
        "expression": "5**3 * exp(-5) / factorial(3)",
        "operator": "lt",
        "threshold": 0.2,
    }))

    import asyncio
    asyncio.run(answer_verifier.verify_objective_numeric([q]))
    assert q["correct_answer"] == "True"
    assert q["model_answer"] == "True. P(X=3) ≈ 0.1404 < 0.2."


# ── Non-fatal / no-op safety ────────────────────────────────────────────────────

def test_no_computation_leaves_question_unchanged(monkeypatch):
    q = {
        "question_type": "mcq",
        "question_text": "Which is a measure of central tendency?\nA. Mean\nB. Range\nC. Variance\nD. IQR",
        "correct_answer": "A",
        "model_answer": "A. Mean",
    }
    _patch_generate(monkeypatch, json.dumps({"has_computation": False, "expression": None}))

    import asyncio
    before = dict(q)
    asyncio.run(answer_verifier.verify_objective_numeric([q]))
    assert q == before


def test_extraction_failure_is_non_fatal(monkeypatch):
    q = {
        "question_type": "mcq",
        "question_text": "Compute it.\nA. 1\nB. 2\nC. 3\nD. 4",
        "correct_answer": "A",
        "model_answer": "A. 1",
    }

    async def boom(prompt: str) -> str:
        raise RuntimeError("network down")

    monkeypatch.setattr(answer_verifier.llm_service, "generate", boom)

    import asyncio
    before = dict(q)
    asyncio.run(answer_verifier.verify_objective_numeric([q]))
    assert q == before


def test_option_value_parses_various_formats():
    assert answer_verifier._option_value("0.045") == pytest.approx(0.045)
    assert answer_verifier._option_value("≈ 0.0096") == pytest.approx(0.0096)
    assert answer_verifier._option_value("P = 1.2e-3") == pytest.approx(1.2e-3)
    assert answer_verifier._option_value("Mean") is None

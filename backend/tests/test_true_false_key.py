"""
Regression: a generated true_false question must always persist a usable
True/False answer key. Without it, the rag_pipeline objective fast path cannot
auto-mark the answer and routes it to manual instructor review instead.
"""
from app.services.question_generator import (
    _derive_true_false_key,
    _validate_questions,
)


def _tf(model_answer: str) -> dict:
    return {
        "question_text": "The sample mean is an unbiased estimator of the population mean.",
        "question_type": "true_false",
        "model_answer": model_answer,
        "rubric": "Full marks: selects the correct option.",
        "max_marks": 2,
        "topic_tag": "Estimation",
        "difficulty": "easy",
    }


def test_explicit_true_token():
    assert _derive_true_false_key("True. The estimator is unbiased.") == "True"


def test_explicit_false_token():
    assert _derive_true_false_key("False — it is actually biased.") == "False"


def test_title_case_preserved():
    # Never lower/upper-case: the marking comparison is case-insensitive but the
    # stored key contract is title-case ("True"/"False").
    key = _derive_true_false_key("TRUE, this holds.")
    assert key == "True"


def test_negation_prose_without_token():
    # No literal "true"/"false" — infer from negation cues.
    assert _derive_true_false_key("This statement is incorrect; it does not hold.") == "False"


def test_affirmation_prose_without_token():
    assert _derive_true_false_key("Yes, this is correct and supported by the source.") == "True"


def test_empty_answer_defaults_to_true():
    # Nothing to go on: must still return a definite key, never None/empty.
    assert _derive_true_false_key("") == "True"


def test_validate_questions_always_sets_key():
    # The path that previously left correct_answer unset: prose with no
    # literal true/false token, inferred from a negation cue.
    out = _validate_questions([_tf("This claim is incorrect and does not hold.")], "true_false")
    assert len(out) == 1
    assert out[0]["correct_answer"] in {"True", "False"}
    assert out[0]["correct_answer"] == "False"


def test_validate_questions_keyless_prose_defaults_true():
    # Even with no token and no cue, a definite key must be set (never None).
    out = _validate_questions([_tf("Refer to the chapter discussion on estimators.")], "true_false")
    assert out[0].get("correct_answer") == "True"


def test_validate_questions_explicit_token():
    out = _validate_questions([_tf("True. Supported by the source section.")], "true_false")
    assert out[0]["correct_answer"] == "True"

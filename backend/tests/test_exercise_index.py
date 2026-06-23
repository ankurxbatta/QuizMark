"""Unit tests for the exercise miner (Try It parsing + embedded content capture)."""
from app.services.exercise_index import (
    split_exercises,
    embedding_text,
    _KIND_EXAMPLE,
    _KIND_TRYIT,
    _KIND_HOMEWORK,
)


def _kinds(entries):
    return [e["exercise_kind"] for e in entries]


def test_try_it_is_parsed_as_its_own_kind():
    text = (
        "Example 4.1\n"
        "A fair coin is tossed three times. Find the probability of exactly two heads.\n"
        "Solution\n"
        "There are 8 equally likely outcomes, three of which have exactly two heads, so 3/8.\n"
        "Try It 4.1\n"
        "A fair coin is tossed four times. Find the probability of exactly three heads.\n"
    )
    entries = split_exercises(text, [], [], "")
    kinds = _kinds(entries)
    assert _KIND_EXAMPLE in kinds
    assert _KIND_TRYIT in kinds

    try_it = next(e for e in entries if e["exercise_kind"] == _KIND_TRYIT)
    assert "four times" in try_it["stem"]
    assert try_it["source_label"] == "Try It 4.1"


def test_try_it_without_number_still_captured():
    text = (
        "Try It\n"
        "Compute the mean of the data set 2, 4, 6, 8, 10 and interpret the result.\n"
    )
    entries = split_exercises(text, [], [], "")
    assert any(e["exercise_kind"] == _KIND_TRYIT for e in entries)
    assert entries[0]["source_label"] == "Try It"


def test_try_it_does_not_bleed_into_following_homework():
    text = (
        "Try It 4.2\n"
        "Find the probability of rolling a sum of seven with two dice.\n"
        "HOMEWORK\n"
        "1. Find the probability of rolling a sum of eight with two dice.\n"
    )
    entries = split_exercises(text, [], [], "")
    try_it = next(e for e in entries if e["exercise_kind"] == _KIND_TRYIT)
    # the homework prompt must not be swallowed into the Try It stem
    assert "sum of eight" not in try_it["stem"]
    assert any(e["exercise_kind"] == _KIND_HOMEWORK for e in entries)


def test_embedded_math_table_figure_attached_to_exercise():
    text = (
        "Example 5.3\n"
        "Using the frequency table below, compute the expected value of X.\n"
        "Solution\n"
        "Multiply each value by its probability and sum: E(X) = 2.1.\n"
    )
    tables = ["X | P(x)\n0 | 0.3\n1 | 0.4\n2 | 0.3"]
    figures = ["Bar chart of the probability distribution of X"]
    math = "E(X) = \\sum x P(x)"
    entries = split_exercises(text, tables, figures, math)
    ex = next(e for e in entries if e["exercise_kind"] == _KIND_EXAMPLE)
    assert "P(x)" in ex["table_markdown"]
    assert "Bar chart" in ex["figure_desc"]
    assert "sum" in ex["math_text"]


def test_embedding_text_includes_math_table_figure():
    entry = {
        "source_label": "Example 5.3",
        "stem": "Compute the expected value of X.",
        "options": [],
        "math_text": "E(X) = \\sum x P(x)",
        "table_markdown": "X | P(x)\n0 | 0.3",
        "figure_desc": "Bar chart of distribution",
    }
    txt = embedding_text(entry)
    assert "formulas:" in txt
    assert "table:" in txt
    assert "figure:" in txt
    assert "Example 5.3" in txt

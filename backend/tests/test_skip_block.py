"""Regression tests for the teaching-density-aware skip filter.

A boilerplate heading (KEY TERMS / CHAPTER REVIEW / footer ...) near the top of
a buffer must only drop the buffer when it is *actually* boilerplate. Buffers
that begin with such a heading but continue into real teaching content
(formulas, definitions, worked examples) must be kept — previously the whole
buffer was discarded, losing co-located content (e.g. the normal-PDF formula
that sat on a KEY TERMS page).
"""
from app.services.pdf_service import _is_skip_block


def test_pure_footer_boilerplate_is_skipped():
    text = (
        "This OpenStax book is available for free.\n"
        "Download for free at openstax.org.\n"
        "Index\nAccess for free at openstax.org"
    )
    assert _is_skip_block(text) is True


def test_homework_dump_is_skipped():
    text = "\n".join(f"{i}. A student measures the height of plants." for i in range(1, 12))
    assert _is_skip_block(text) is True


def test_empty_block_is_skipped():
    assert _is_skip_block("   \n  ") is True


def test_key_terms_glossary_with_definitions_is_kept():
    # Begins with a KEY TERMS skip signal but is dense with definitions/formulas.
    text = (
        "KEY TERMS\n"
        "Mean: the mean is defined as the sum divided by the count.\n"
        "Standard Deviation: a measure of spread where each value differs from the mean.\n"
        "Normal Distribution: defined by the formula f(x) where mu is the mean "
        "and sigma is the standard deviation.\n"
        "Variance: the standard deviation squared."
    )
    assert _is_skip_block(text) is False


def test_normal_teaching_block_is_kept():
    text = (
        "The normal distribution has probability density f(x) where x is a value, "
        "mu is the mean and sigma equals the standard deviation. Example 6.1 shows "
        "the solution. NOTE: properties of the normal curve."
    )
    assert _is_skip_block(text) is False

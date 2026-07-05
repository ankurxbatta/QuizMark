"""
Deterministic source-label sanitiser (question_generator._strip_source_labels).

Student-facing text must never cite book-internal labels like "Table 1.9" /
"Figure 2.3" / "Example 1.15" — the student can't see the book and it leaks the
source. These pure-function tests touch no network.
"""
from app.services.question_generator import _strip_source_labels


def test_table_label_becomes_generic_reference():
    out = _strip_source_labels("According to Table 1.9, what percentage exceeds 50?")
    assert "Table 1.9" not in out
    assert "the table below" in out.lower()
    assert out.startswith("According to the table below, what")


def test_figure_label_at_sentence_start_is_capitalised():
    out = _strip_source_labels("Figure 6.1 shows the income distribution.")
    assert "Figure 6.1" not in out
    assert "the figure below" in out.lower()
    assert out.startswith("The figure below shows")


def test_example_label_is_genericised():
    out = _strip_source_labels("Use the data in Example 1.15 to find the mean.")
    assert "Example 1.15" not in out
    assert "example" in out.lower()  # genericised, label number gone


def test_exercise_and_problem_labels_dropped():
    assert "Exercise 3.4" not in _strip_source_labels("Repeat Exercise 3.4 for the new sample.")
    assert "Problem 12" not in _strip_source_labels("Solve Problem 12 using the formula.")


def test_graph_and_chart_map_to_figure():
    assert "the figure below" in _strip_source_labels("In Graph 2, the trend is upward.").lower()
    assert "the figure below" in _strip_source_labels("Chart 3.1 displays the totals.").lower()


def test_normal_sentence_unchanged():
    text = "State the formula for the mean of a binomial distribution."
    assert _strip_source_labels(text) == text


def test_bare_decimal_not_mangled():
    # No label word precedes the number → must be left untouched.
    text = "The probability is p = 1.9 and the height is 9.01 inches."
    assert _strip_source_labels(text) == text


def test_lettered_and_dotted_numbers_are_handled():
    assert "Table 1.9a" not in _strip_source_labels("See Table 1.9a for details.")
    assert "Figure 2.3.1" not in _strip_source_labels("Refer to Figure 2.3.1 here.")


def test_preceding_article_is_not_doubled():
    out = _strip_source_labels("The data in the Table 1.9 are summarised.")
    assert "the the" not in out.lower()
    assert "the table below" in out.lower()


def test_empty_string_safe():
    assert _strip_source_labels("") == ""

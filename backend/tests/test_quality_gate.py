"""
Deterministic question-quality gate (Stage A: renderability).

These cover the PURE, LLM-free checks in answer_verifier._passes_renderability:
un-renderable / incomplete / malformed questions are rejected so the top-up
loops regenerate replacements. The LLM judge (Stage B) is NOT exercised here —
no network is touched.
"""
from app.services.answer_verifier import _judge_asset_block, _passes_renderability

_CLEAN_TABLE_HTML = (
    "<table><thead><tr><th>x</th><th>f</th></tr></thead>"
    "<tbody><tr><td>1</td><td>2</td></tr><tr><td>2</td><td>5</td></tr></tbody></table>"
)


def _keep(q) -> bool:
    return _passes_renderability(q)[0]


# ── Tables ──────────────────────────────────────────────────────────────────────

def test_table_reference_without_asset_is_rejected():
    q = {
        "question_text": "Using the following table, compute the sample mean.",
        "question_type": "short_answer",
    }
    keep, reason = _passes_renderability(q)
    assert keep is False
    assert "table" in reason.lower()


def test_table_reference_with_clean_asset_is_kept():
    q = {
        "question_text": "Using the table below, compute the sample mean.",
        "question_type": "short_answer",
        "assets": [{"kind": "table", "table_html": _CLEAN_TABLE_HTML}],
    }
    assert _keep(q) is True


def test_table_reference_with_inline_markdown_table_is_kept():
    q = {
        "question_text": (
            "Using the following table, find the mode.\n"
            "| x | f |\n| 1 | 2 |\n| 2 | 5 |"
        ),
        "question_type": "short_answer",
    }
    assert _keep(q) is True


def test_garbled_table_asset_with_stray_question_mark_is_rejected():
    garbled = _CLEAN_TABLE_HTML.replace("<td>5</td>", "<td>?</td>")
    q = {
        "question_text": "The table below lists the joint frequencies for the survey.",
        "question_type": "short_answer",
        "assets": [{"kind": "table", "table_html": garbled}],
    }
    # No compute/find cue in the stem → the '?' reads as a broken cell, not a prompt.
    assert _keep(q) is False


# ── Figures ─────────────────────────────────────────────────────────────────────

def test_figure_reference_without_asset_is_rejected():
    q = {
        "question_text": "Based on the graph above, estimate the median income.",
        "question_type": "short_answer",
    }
    keep, reason = _passes_renderability(q)
    assert keep is False
    assert "figure" in reason.lower()


def test_generic_graph_verb_is_not_flagged():
    # "graph the function" does not reference a shown figure → must be kept.
    q = {
        "question_text": "Explain how to graph a linear function from its equation.",
        "question_type": "short_answer",
    }
    assert _keep(q) is True


# ── Placeholders / truncation / delimiters ───────────────────────────────────────

def test_blank_placeholder_is_rejected():
    q = {
        "question_text": "The mean of a binomial distribution is ____ when n is large.",
        "question_type": "short_answer",
    }
    keep, reason = _passes_renderability(q)
    assert keep is False
    assert "____" in reason


def test_unbalanced_dollar_latex_is_rejected():
    q = {
        "question_text": "Compute the expected value $\\mu = 5 for the process.",
        "question_type": "short_answer",
    }
    keep, reason = _passes_renderability(q)
    assert keep is False
    assert "delimiter" in reason.lower()


def test_currency_dollar_is_not_flagged():
    # Single '$' currency with no LaTeX command must not trip the $-balance check.
    q = {
        "question_text": "A vendor charges a flat fee of $25 to rent a booth. State the fixed cost.",
        "question_type": "short_answer",
    }
    assert _keep(q) is True


def test_truncated_mid_sentence_is_rejected():
    q = {
        "question_text": "State the formula for the variance of the",
        "question_type": "short_answer",
    }
    assert _keep(q) is False


# ── MCQ structure ────────────────────────────────────────────────────────────────

def test_mcq_correct_answer_not_in_options_is_rejected():
    q = {
        "question_text": (
            "What is the mean of the data set?\n"
            "A. 1\nB. 2\nC. 3\nD. 4"
        ),
        "question_type": "mcq",
        "correct_answer": "E",
    }
    keep, reason = _passes_renderability(q)
    assert keep is False
    assert "correct_answer" in reason.lower()


def test_mcq_duplicate_options_is_rejected():
    q = {
        "question_text": (
            "Which statistic measures central tendency?\n"
            "A. The mean\nB. The mean\nC. The range\nD. The variance"
        ),
        "question_type": "mcq",
        "correct_answer": "A",
    }
    assert _keep(q) is False


def test_well_formed_mcq_is_kept():
    q = {
        "question_text": (
            "A researcher increases the sample size. What happens to the standard error?\n"
            "A. It increases.\nB. It decreases.\nC. It stays the same.\nD. It becomes negative."
        ),
        "question_type": "mcq",
        "correct_answer": "B",
    }
    assert _keep(q) is True


# ── Normal self-contained question ───────────────────────────────────────────────

def test_normal_self_contained_question_is_kept():
    q = {
        "question_text": "State the formula for the mean of a binomial distribution.",
        "question_type": "short_answer",
    }
    assert _keep(q) is True


# ── Asset-aware judge input (pure block construction, no network) ────────────────

def test_judge_asset_block_includes_attached_table_html():
    q = {
        "question_text": "Using the table below, compute the sample mean.",
        "assets": [{"kind": "table", "caption": "Frequencies", "table_html": _CLEAN_TABLE_HTML}],
    }
    block = _judge_asset_block(q)
    assert "ATTACHED TABLE" in block
    assert "<table" in block.lower()
    assert "Frequencies" in block


def test_judge_asset_block_includes_figure_spec():
    spec = "Histogram; x-axis income; y-axis count; bars 4,9,12,6."
    q = {
        "question_text": "Using the figure below, estimate the median income.",
        "assets": [{"kind": "figure", "image_id": None, "_figure_spec": spec}],
    }
    block = _judge_asset_block(q)
    assert "ATTACHED FIGURE" in block
    assert spec in block


def test_judge_asset_block_reports_none_when_no_asset():
    q = {"question_text": "State the formula for the binomial mean."}
    assert "No table or figure" in _judge_asset_block(q)

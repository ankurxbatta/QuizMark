import json

from app.services.math_index import (
    embedding_text,
    formula_doc_id,
    normalise_formula,
    parse_enrichment,
    render_formulas_block,
    split_formulas,
)
from app.services.mongo_vector_store import build_vector_search_pipeline


# ── Formula splitting ───────────────────────────────────────────────────────────

def test_split_extracts_math_text_lines():
    math_text = "s = sqrt(sum((x_i - x_bar)^2) / (n - 1))\n\nz = (x - mu) / sigma"
    out = split_formulas(math_text)
    assert [e["latex"] for e in out] == [
        "s = sqrt(sum((x_i - x_bar)^2) / (n - 1))",
        "z = (x - mu) / sigma",
    ]


def test_split_dedupes_whitespace_variants():
    math_text = "z = (x - mu) / sigma\nz=(x-mu)/sigma"
    assert len(split_formulas(math_text)) == 1


def test_split_picks_formula_lines_from_prose():
    text = (
        "The standard score measures distance from the mean.\n"
        "z = (x − μ) / σ\n"
        "This is widely used in hypothesis testing because it standardises values."
    )
    out = split_formulas("", text)
    assert len(out) == 1
    assert "z =" in out[0]["latex"]


def test_split_skips_prose_with_operators():
    # A wordy sentence containing '=' must not be treated as a formula.
    text = "The result of the experiment = a clear improvement in all observed test scores overall."
    assert split_formulas("", text) == []


def test_context_sentence_attached():
    math_text = "z = (x - mu) / sigma"
    text = "Many tests exist. The z score z is computed from x, mu and sigma to standardise data."
    out = split_formulas(math_text, text)
    assert "standardise" in out[0]["context_sentence"]


def test_doc_id_deterministic_and_normalised():
    a = formula_doc_id("hash1", "chunk1", "z = (x - mu) / sigma")
    b = formula_doc_id("hash1", "chunk1", "z=(x-mu)/sigma")
    c = formula_doc_id("hash1", "chunk2", "z = (x - mu) / sigma")
    assert a == b          # whitespace-insensitive
    assert a != c          # different parent chunk → different doc
    assert len(a) == 24


def test_normalise_formula_caps_length():
    assert len(normalise_formula("x" * 1000)) == 300


# ── Enrichment parsing ──────────────────────────────────────────────────────────

def test_parse_enrichment_happy_path():
    raw = json.dumps([
        {"i": 1, "concept_label": "z score", "formula_plain": "z = (x - mu) / sigma",
         "variables": {"x": "observation", "mu": "population mean"}},
    ])
    out = parse_enrichment(raw, batch_size=1)
    assert out[1]["concept_label"] == "z score"
    assert out[1]["variables"]["mu"] == "population mean"


def test_parse_enrichment_rejects_garbage():
    assert parse_enrichment("not json at all", batch_size=3) == {}
    assert parse_enrichment(json.dumps({"i": 1}), batch_size=3) == {}  # not a list
    # out-of-range indices are dropped
    assert parse_enrichment(json.dumps([{"i": 9, "concept_label": "x"}]), batch_size=3) == {}


def test_parse_enrichment_clamps_variables():
    variables = {f"v{n}": "meaning" for n in range(20)}
    raw = json.dumps([{"i": 1, "concept_label": "many vars", "variables": variables}])
    out = parse_enrichment(raw, batch_size=1)
    assert len(out[1]["variables"]) == 6


# ── Prompt rendering ────────────────────────────────────────────────────────────

def test_render_block_empty_when_no_formulas():
    assert render_formulas_block([]) == ""


def test_render_block_contains_label_and_formula():
    block = render_formulas_block([
        {"concept_label": "sample variance", "formula_latex": "s^2 = ...",
         "context_sentence": "Used for spread."},
    ])
    assert "KEY FORMULAS" in block
    assert "sample variance: s^2 = ..." in block
    assert "(Used for spread.)" in block


def test_embedding_text_composition():
    text = embedding_text({
        "concept_label": "z score", "formula_plain": "z = (x - mu) / sigma",
        "context_sentence": "Standardises a value.",
    })
    assert text == "z score: z = (x - mu) / sigma — Standardises a value."


# ── Vector search pipeline construction ─────────────────────────────────────────

def test_pipeline_prefilters_inside_vector_search():
    pipeline = build_vector_search_pipeline(
        [0.1] * 3, k=5, index_name="idx", filters={"book_id": "b1"}
    )
    assert pipeline[0]["$vectorSearch"]["filter"] == {"book_id": "b1"}
    assert all("$match" not in stage for stage in pipeline)


def test_pipeline_legacy_postfilter_fallback():
    pipeline = build_vector_search_pipeline(
        [0.1] * 3, k=5, index_name="idx", filters={"book_id": "b1"}, pre_filter=False
    )
    assert "filter" not in pipeline[0]["$vectorSearch"]
    assert {"$match": {"book_id": "b1"}} in pipeline


def test_pipeline_no_filters():
    pipeline = build_vector_search_pipeline([0.1] * 3, k=5, index_name="idx")
    assert "filter" not in pipeline[0]["$vectorSearch"]
    assert len(pipeline) == 2  # $vectorSearch + $project

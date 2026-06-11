import json

from app.services.figure_index import (
    extract_caption,
    figure_doc_id,
    parse_classification,
    render_figures_block,
    split_figures,
)
from app.services.figure_index import embedding_text as figure_embedding_text
from app.services.table_index import (
    extract_headers,
    parse_summaries,
    render_tables_block,
    split_tables,
    table_doc_id,
)
from app.services.table_index import embedding_text as table_embedding_text


# ── Figure extraction ───────────────────────────────────────────────────────────

def test_split_figures_one_per_description():
    descs = [
        "A histogram of exam scores showing a right-skewed distribution with a peak near 70.",
        "A scatter plot of study hours versus final grade showing positive correlation.",
    ]
    out = split_figures(descs)
    assert len(out) == 2
    assert out[0]["description"].startswith("A histogram")


def test_split_figures_skips_short_and_duplicates():
    descs = ["tiny", "A histogram of exam scores over five intervals.",
             "A  histogram of exam   scores over five intervals."]  # whitespace dupe
    assert len(split_figures(descs)) == 1


def test_caption_extracted_from_prose():
    text = "Some intro text.\nFigure 3.2 Distribution of sample means\nMore prose follows here."
    out = split_figures(["A bell-shaped curve centred at the population mean value."], text)
    assert out[0]["caption"].startswith("Figure 3.2")


def test_caption_empty_when_absent():
    assert extract_caption("No figures mentioned in this prose at all.") == ""


def test_figure_doc_id_deterministic():
    a = figure_doc_id("h1", "c1", "A histogram of scores.")
    b = figure_doc_id("h1", "c1", "A  histogram   of scores.")
    assert a == b
    assert len(a) == 24


# ── Figure classification parsing ───────────────────────────────────────────────

def test_parse_classification_happy_path():
    raw = json.dumps([{"i": 1, "figure_kind": "histogram",
                       "axis_summary": "x: score, y: frequency — right-skewed"}])
    out = parse_classification(raw, batch_size=1)
    assert out[1]["figure_kind"] == "histogram"


def test_parse_classification_unknown_kind_becomes_other():
    raw = json.dumps([{"i": 1, "figure_kind": "hologram", "axis_summary": "x"}])
    assert parse_classification(raw, batch_size=1)[1]["figure_kind"] == "other"


def test_parse_classification_rejects_garbage():
    assert parse_classification("nope", batch_size=2) == {}
    assert parse_classification(json.dumps([{"i": 7, "figure_kind": "bar"}]), batch_size=2) == {}


# ── Figure rendering ────────────────────────────────────────────────────────────

def test_render_figures_block():
    block = render_figures_block([{
        "figure_kind": "histogram", "caption": "Figure 2.1 Exam scores",
        "axis_summary": "x: score, y: frequency", "description": "A histogram…",
        "chapter_num": 2, "page": 41,
    }])
    assert "FIGURES FROM THE TEXTBOOK" in block
    assert "[histogram] Figure 2.1 Exam scores — x: score, y: frequency" in block
    assert render_figures_block([]) == ""


def test_figure_embedding_text_contains_kind_and_caption():
    text = figure_embedding_text({
        "figure_kind": "scatter", "caption": "Figure 5.3", "axis_summary": "x vs y",
        "description": "A scatter plot.",
    })
    assert text.startswith("scatter — Figure 5.3 — x vs y")


# ── Table extraction ────────────────────────────────────────────────────────────

MD_TABLE = "| Interval | Frequency |\n| --- | --- |\n| 0-10 | 4 |\n| 10-20 | 9 |"


def test_split_tables_and_headers():
    out = split_tables([MD_TABLE])
    assert len(out) == 1
    assert out[0]["headers"] == ["Interval", "Frequency"]


def test_split_tables_skips_short_and_duplicates():
    assert len(split_tables(["| a |", MD_TABLE, MD_TABLE])) == 1


def test_extract_headers_skips_separator_rows():
    assert extract_headers("| --- | --- |\n| a | b |") == []


def test_table_doc_id_deterministic():
    a = table_doc_id("h1", "c1", MD_TABLE)
    b = table_doc_id("h1", "c1", MD_TABLE.replace(" ", "  "))
    assert a == b


# ── Table summarisation parsing ─────────────────────────────────────────────────

def test_parse_summaries():
    raw = json.dumps([{"i": 1, "table_summary": "frequency distribution of scores"}])
    assert parse_summaries(raw, batch_size=1) == {1: "frequency distribution of scores"}
    assert parse_summaries("garbage", batch_size=1) == {}


# ── Table rendering ─────────────────────────────────────────────────────────────

def test_render_tables_block_includes_rows():
    block = render_tables_block([{
        "table_summary": "frequency distribution of scores",
        "table_markdown": MD_TABLE, "chapter_num": 1, "page": 12,
    }])
    assert "TABLES FROM THE TEXTBOOK" in block
    assert "| Interval | Frequency |" in block
    assert render_tables_block([]) == ""


def test_table_embedding_text():
    text = table_embedding_text({
        "table_summary": "frequency distribution", "headers": ["Interval", "Frequency"],
        "table_markdown": MD_TABLE,
    })
    assert text.startswith("frequency distribution — headers: Interval, Frequency")

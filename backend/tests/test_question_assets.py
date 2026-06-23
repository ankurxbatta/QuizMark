import asyncio

from app.services.question_assets import (
    build_table_asset,
    markdown_table_to_html,
    render_table_html,
)

# A "find the missing probability" table: the P(x) for x=3 is left blank so the
# student computes it from the constraint that the values sum to 1.
MISSING_PX = "x | P(x)\n0 | 0.03\n1 | 0.50\n2 | 0.24\n3 |\n4 | 0.07\n5 | 0.04"

FULL_TABLE = "| Interval | Frequency |\n| --- | --- |\n| 0-10 | 4 |\n| 10-20 | 9 |"


# ── render_table_html ───────────────────────────────────────────────────────────

def test_render_table_html_counts_blank_body_cells():
    html, n_blanks = render_table_html(MISSING_PX)
    assert n_blanks == 1
    # The blank P(x) for x=3 renders as a "?" placeholder, not an empty cell.
    assert "<td>3</td><td>?</td>" in html


def test_render_table_html_complete_table_has_no_blanks():
    html, n_blanks = render_table_html(FULL_TABLE)
    assert n_blanks == 0
    assert "?" not in html


def test_render_table_html_columns_stay_aligned_when_row_short():
    # A short data row must pad on the right, never shift values into the wrong
    # column — the missing value lands in P(x), not in x.
    html, n_blanks = render_table_html("a | b | c\n1 | 2 | 3\n4 | 5")
    assert n_blanks == 1
    assert "<td>4</td><td>5</td><td>?</td>" in html


def test_render_table_html_empty_header_cell_not_placeheld():
    # Placeholder is only for body cells; a blank header stays empty.
    html, _ = render_table_html("a | \nb | c")
    assert "<th>a</th><th></th>" in html


def test_markdown_table_to_html_returns_html_only():
    assert markdown_table_to_html(FULL_TABLE) == render_table_html(FULL_TABLE)[0]


# ── build_table_asset caption annotation ────────────────────────────────────────

def test_build_table_asset_annotates_caption_on_blank():
    asset = asyncio.run(build_table_asset(MISSING_PX, caption="Probability distribution"))
    assert 'Find the value(s) shown as "?".' in asset["caption"]
    assert asset["caption"].startswith("Probability distribution")


def test_build_table_asset_no_annotation_when_complete():
    asset = asyncio.run(build_table_asset(FULL_TABLE, caption="Frequency table"))
    assert asset["caption"] == "Frequency table"
    assert "?" not in asset["table_html"]


def test_build_table_asset_annotates_even_without_caption():
    asset = asyncio.run(build_table_asset(MISSING_PX))
    assert 'Find the value(s) shown as "?".' in asset["caption"]

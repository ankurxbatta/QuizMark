"""
question_assets.py — build optional, bounded assets attached to generated questions.

Two asset kinds:
  • table  — a data table rendered as deterministic HTML from stored markdown
             (no LLM, no image; always accurate).
  • figure — an AI-generated chart image, redrawn from a stored figure's
             word-description ADAPTED to the specific generated question.

Everything here is fully defensive: any failure on one question must never
break the batch, and assets degrade cleanly (omitted) when image generation
is disabled or unavailable.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from uuid import uuid4

from app.core.config import settings

logger = logging.getLogger(__name__)

# Heuristic: question text that references a visual/data asset.
_ASSET_HINT_RE = re.compile(
    r"\b(table|figure|chart|graph|histogram|following data|shown below|the data below)\b",
    re.IGNORECASE,
)
# Sub-classifiers: decide table vs figure from the question wording.
_TABLE_HINT_RE = re.compile(r"\b(table|following data|the data below|data below)\b", re.IGNORECASE)
_FIGURE_HINT_RE = re.compile(r"\b(figure|chart|graph|histogram|plot|diagram)\b", re.IGNORECASE)

_ADAPT_PROMPT = """You are writing an image-generation prompt for a textbook figure.

A statistics question references a chart/figure. Using the source figure
description and axis summary below, write ONE concise image-generation prompt
that redraws the figure so its axes, labels and values match THIS question.

Question:
{question_text}

Source figure description:
{figure_desc}

Axis summary:
{axis_summary}

Produce a clean, labeled statistical chart suitable for a textbook. Axes,
labels and values must match the question. No watermark, no text paragraphs.
Respond with ONLY the image-generation prompt text (under 700 characters)."""


# ── Markdown table → HTML (deterministic) ───────────────────────────────────────

# Placeholder shown for a blank body cell — a value the exercise asks the
# student to find (e.g. a missing probability), so it reads as intentional
# rather than as a broken empty cell.
_BLANK_CELL = "?"


_MATH_SPAN_RE = re.compile(r"(\$\$.+?\$\$|\\\[.+?\\\]|\\\(.+?\\\)|\$[^$\n]+?\$)", re.DOTALL)


def _escape_preserving_math(text: str) -> str:
    """HTML-escape cell text but leave LaTeX math spans ($...$, \\(...\\), etc.)
    verbatim, so the frontend can render them with KaTeX instead of receiving
    mangled delimiters."""
    parts = _MATH_SPAN_RE.split(text)
    # split() with one capture group yields: text, math, text, math, ... so the
    # odd indices are the math spans to keep untouched.
    return "".join(p if i % 2 else html.escape(p) for i, p in enumerate(parts))


def render_table_html(md: str) -> tuple[str, int]:
    """Convert a markdown/pipe table to clean minimal HTML.

    First parsed row becomes <th>. Cell content is HTML-escaped. Tolerant of the
    loosely-formatted tables in this book: rows may be plain whitespace-separated
    lines without pipes. If no grid can be parsed, the raw text is wrapped in
    <pre> rather than dropped.

    Returns (html, n_blanks) where n_blanks counts the body cells rendered as the
    "?" placeholder — so callers can flag/annotate a table that has gaps rather
    than letting a missing value read as a silently incomplete table.
    """
    text = (md or "").strip()
    if not text:
        return "", 0

    rows: list[list[str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Skip markdown separator rows like |---|---| or :--: dividers.
        if set(line) <= {"-", ":", "|", " "}:
            continue
        if "|" in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
        else:
            # Fall back to splitting on runs of 2+ spaces or tabs.
            parts = re.split(r"\s{2,}|\t+", line)
            cells = [c.strip() for c in parts if c.strip()]
            if len(cells) < 2:
                # Single-column line — not a usable grid row on its own.
                cells = [line]
        rows.append(cells)

    # Need at least a header + one data row, with some multi-column structure.
    usable = [r for r in rows if r]
    if len(usable) < 2 or max((len(r) for r in usable), default=0) < 2:
        return f"<pre>{html.escape(text)}</pre>", 0

    ncols = max(len(r) for r in usable)
    n_blanks = 0

    def _cells(cells: list[str], tag: str) -> str:
        nonlocal n_blanks
        padded = list(cells) + [""] * (ncols - len(cells))
        out: list[str] = []
        for c in padded:
            if c:
                out.append(f"<{tag}>{_escape_preserving_math(c)}</{tag}>")
            elif tag == "td":
                n_blanks += 1
                out.append(f"<{tag}>{_BLANK_CELL}</{tag}>")
            else:
                out.append(f"<{tag}></{tag}>")
        return "".join(out)

    head = usable[0]
    body = usable[1:]
    parts = ["<table>", "<thead>", "<tr>", _cells(head, "th"), "</tr>", "</thead>", "<tbody>"]
    for r in body:
        parts.extend(["<tr>", _cells(r, "td"), "</tr>"])
    parts.extend(["</tbody>", "</table>"])
    return "".join(parts), n_blanks


def markdown_table_to_html(md: str) -> str:
    """Convert a markdown/pipe table to clean minimal HTML (html only)."""
    return render_table_html(md)[0]


# ── Asset builders ──────────────────────────────────────────────────────────────

async def build_table_asset(table_markdown: str, caption: str = "") -> dict:
    """Build a deterministic table asset from stored markdown.

    Source textbook tables are sometimes "find the missing value" exercises with
    a blank cell (e.g. a probability distribution where one P(x) is left out so it
    can be computed from the constraint that the values sum to 1). Such a cell is
    rendered as "?". When that happens we append a one-line note to the caption so
    the gap reads as a deliberate prompt to the student, not an incomplete table.
    """
    table_html, n_blanks = render_table_html(table_markdown)
    caption = caption or ""
    if n_blanks:
        note = (
            'Find the value(s) shown as "?".'
            if "?" not in caption
            else ""
        )
        caption = (f"{caption} — {note}".strip(" —") if note else caption)
    return {
        "kind": "table",
        "caption": caption,
        "alt_text": caption or "Data table",
        "table_html": table_html,
        "image_id": None,
        "source_page": None,
    }


async def build_figure_asset(
    figure_desc: str,
    axis_summary: str,
    question_text: str,
    caption: str = "",
) -> dict | None:
    """Build a figure asset by generating an AI image adapted to the question.

    Returns None (degrade) when image generation is disabled or fails at any step.
    """
    if not settings.IMAGE_GEN_ENABLED:
        return None

    from app.services.llm_service import generate_image, llm_service

    # 1. Adapt the stored description into a question-specific image prompt.
    try:
        adapted = (await llm_service.generate(_ADAPT_PROMPT.format(
            question_text=(question_text or "")[:800],
            figure_desc=(figure_desc or "")[:800],
            axis_summary=(axis_summary or "")[:400],
        ))).strip()
    except Exception as exc:
        logger.warning(f"[asset] figure prompt adaptation failed (degrading): {exc}")
        return None
    if not adapted:
        return None
    adapted = adapted[:800]

    # 2. Generate the image; degrade to None on any failure / empty result.
    try:
        png = await generate_image(adapted)
    except Exception as exc:
        logger.warning(f"[asset] image generation failed (degrading): {exc}")
        return None
    if not png:
        return None

    # 3. Persist to GridFS.
    from app.services.mongo_vector_store import save_question_asset
    asset_id = uuid4().hex
    saved = await save_question_asset(asset_id, png)
    if not saved:
        return None

    return {
        "kind": "figure",
        "caption": caption,
        "alt_text": (adapted[:200] or caption or "Generated figure"),
        "table_html": None,
        "image_id": asset_id,
        "source_page": None,
    }


# ── Batch attachment ────────────────────────────────────────────────────────────

_NOT_CHART_RE = re.compile(r"no[_ ]?chart|does not contain a (?:statistical )?(?:chart|graph)|textual content", re.IGNORECASE)


def _is_real_chart(description: str) -> bool:
    """False for figure-index entries that are really text pages (the vision
    pass self-reports NO_CHART / 'does not contain a chart')."""
    desc = (description or "").strip()
    if len(desc) < 20:
        return False
    return not _NOT_CHART_RE.search(desc[:200])


async def _embed_query(text: str) -> list[float]:
    from app.services.llm_service import slm_service
    return await slm_service.embed(text)


async def _build_one_asset(q: dict, book_id, chapter_num) -> dict | None:
    """Find a relevant source table/figure for this question and build ONE asset."""
    q_text = q.get("question_text", "") or ""
    wants_table = bool(_TABLE_HINT_RE.search(q_text))
    wants_figure = bool(_FIGURE_HINT_RE.search(q_text))
    # Ambiguous (or only the generic L4 signal) → prefer table (accurate).
    if not wants_table and not wants_figure:
        wants_table = True
    prefer_table = wants_table or not wants_figure

    try:
        q_emb = await _embed_query(q_text)
    except Exception as exc:
        logger.debug(f"[asset] embed failed for question (skip): {exc}")
        return None
    if not q_emb:
        return None

    if prefer_table:
        try:
            from app.services.table_index import retrieve_tables
            tables = await retrieve_tables(q_emb, book_id=book_id, chapter_num=chapter_num, k=1)
        except Exception as exc:
            logger.debug(f"[asset] retrieve_tables failed (skip): {exc}")
            tables = []
        if tables:
            t = tables[0]
            return await build_table_asset(
                t.get("table_markdown", ""),
                caption=t.get("table_summary", "") or "",
            )
        # No table found — fall through to figure if the question hinted one.

    try:
        from app.services.figure_index import retrieve_figures
        figures = await retrieve_figures(q_emb, book_id=book_id, chapter_num=chapter_num, k=3)
    except Exception as exc:
        logger.debug(f"[asset] retrieve_figures failed (skip): {exc}")
        figures = []
    # Skip text pages misclassified as figures during ingestion (their vision
    # description self-reports NO_CHART) — only build from a real chart.
    for f in figures:
        if not _is_real_chart(f.get("description", "")):
            continue
        return await build_figure_asset(
            figure_desc=f.get("description", "") or "",
            axis_summary=f.get("axis_summary", "") or "",
            question_text=q_text,
            caption=f.get("caption", "") or "",
        )
    return None


async def attach_assets_to_questions(
    questions: list[dict],
    chapter_num=None,
    book_id=None,
) -> list[dict]:
    """Attach at most settings.ASSET_MAX_PER_CHAPTER assets across the batch.

    Only questions whose text references a table/figure (or are bloom_level L4)
    are candidates. Each gets at most one asset. Fully defensive: any single
    failure is swallowed so the batch always returns intact.
    """
    if not questions:
        return questions

    limit = max(0, int(settings.ASSET_MAX_PER_CHAPTER))
    if limit == 0:
        return questions

    # Only attach to questions that EXPLICITLY reference a table/figure/data —
    # the bloom-L4 signal alone was too loose and bolted tables onto unrelated
    # questions (e.g. a binomial word problem that never mentions a table).
    candidates = [
        q for q in questions
        if _ASSET_HINT_RE.search(q.get("question_text", "") or "")
    ]
    if not candidates:
        return questions

    # Cap by SUCCESSFUL attachments, not by candidate slice: many candidates
    # legitimately yield no asset (no real chart, image-gen disabled, embed
    # fail), so slicing candidates first could attach zero even when later
    # questions reference real tables. Process in bounded batches and stop once
    # `limit` assets are actually attached.
    sem = asyncio.Semaphore(2)
    attached = 0

    async def _try_attach(q: dict) -> bool:
        async with sem:
            try:
                asset = await _build_one_asset(q, book_id, chapter_num)
            except Exception as exc:
                logger.warning(f"[asset] build failed (non-fatal): {exc}")
                asset = None
        if asset:
            q["assets"] = [asset]
            return True
        return False

    batch = max(1, limit)
    for start in range(0, len(candidates), batch):
        if attached >= limit:
            break
        window = candidates[start:start + batch]
        results = await asyncio.gather(*[_try_attach(q) for q in window])
        attached += sum(1 for ok in results if ok)
    return questions

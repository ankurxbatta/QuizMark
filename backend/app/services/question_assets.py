"""
question_assets.py — build optional, bounded assets attached to generated questions.

Two asset kinds:
  • table  — a data table rendered as deterministic HTML from stored markdown
             (no LLM, no image; always accurate).
  • figure — an AI-generated chart image. Generation produces a TEXT spec only;
             the actual image is rendered post-quality-gate by
             realize_figure_images() for the questions that survived.

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


# ── Post-gate figure image realization ───────────────────────────────────────────
# A figure asset is produced during generation as a TEXT spec only (kind 'figure',
# image_id=None, _figure_spec=...). The actual gpt-image-1 image is expensive, so we
# render it ONLY here — after the quality gate — for the questions that survived.

_SPEC_IMAGE_PROMPT = (
    "Draw a clean, minimal, labeled statistical diagram for a textbook that illustrates the SHAPE or PATTERN "
    "described below (e.g. a skewed or symmetric distribution curve, a normal bell curve, a scatter pattern, or "
    "a boxplot). Show clearly-labeled axes and the qualitative shape; do NOT invent precise numeric tick values. "
    "No watermark, no extra explanatory text.\n\nSpecification:\n{spec}"
)


def _figure_spec_asset(q: dict) -> dict | None:
    """The question's figure asset still awaiting an image (spec only), if any."""
    for asset in q.get("assets") or []:
        if isinstance(asset, dict) and asset.get("kind") == "figure" and not asset.get("image_id"):
            return asset
    return None


async def _realize_one_figure(q: dict) -> bool:
    """Generate + attach the image for a question's figure-spec asset.
    Returns True when the asset now carries an image (or there was nothing to do)."""
    asset = _figure_spec_asset(q)
    if asset is None:
        return True  # no pending figure
    spec = (asset.get("_figure_spec") or asset.get("alt_text") or asset.get("caption") or "").strip()
    if not settings.IMAGE_GEN_ENABLED or not spec:
        return False

    from app.services.llm_service import generate_image
    try:
        png = await generate_image(_SPEC_IMAGE_PROMPT.format(spec=spec[:700]))
    except Exception as exc:
        logger.warning(f"[asset] figure image generation failed (degrading): {exc}")
        return False
    if not png:
        return False

    from app.services.mongo_vector_store import save_question_asset
    asset_id = uuid4().hex
    if not await save_question_asset(asset_id, png):
        return False
    asset["image_id"] = asset_id
    asset["alt_text"] = asset.get("alt_text") or spec[:200]
    return True


def _drop_unrendered_figures(questions: list[dict]) -> list[dict]:
    """Drop questions whose figure asset has no image (image gen disabled/failed),
    so the frontend never shows a 'see the figure below' with nothing to render.
    Also strips the transient ``_figure_spec`` key from surviving assets."""
    kept: list[dict] = []
    for q in questions:
        if _figure_spec_asset(q) is not None:
            logger.info(
                "[asset] dropping figure question with no rendered image: %r",
                (q.get("question_text", "") or "")[:80],
            )
            continue
        for asset in q.get("assets") or []:
            if isinstance(asset, dict):
                asset.pop("_figure_spec", None)
        kept.append(q)
    return kept


class ImageBudget:
    """Mutable, cumulative cap on the number of figure images realized across
    MULTIPLE realize_figure_images calls within a single orchestration run.

    realize_figure_images is called once per gate pass and again on every
    post-gate top-up round, so a per-CALL cap (ASSET_MAX_PER_CHAPTER) would let a
    chapter realize up to ASSET_MAX_PER_CHAPTER × (1 + top-up rounds) gpt-image-1
    images. Threading one ImageBudget through every call makes the cap apply to
    the SUM across the run instead."""

    def __init__(self, remaining: int):
        self.remaining = max(0, int(remaining))

    def take(self, n: int) -> None:
        self.remaining = max(0, self.remaining - max(0, int(n)))


async def realize_figure_images(
    questions: list[dict],
    chapter_num=None,
    book_id=None,
    budget: "ImageBudget | None" = None,
) -> list[dict]:
    """Generate the actual chart image for every gate-surviving question that
    carries a figure SPEC asset. Bounded by ASSET_MAX_PER_CHAPTER per call, and —
    when a cumulative ``budget`` is supplied — by the run-wide remaining budget so
    the total across repeated calls (top-up loop) can't exceed the chapter cap.
    Fully defensive: any failure degrades to dropping that figure question."""
    if not questions:
        return questions

    pending = [q for q in questions if _figure_spec_asset(q) is not None]
    limit = max(0, int(settings.ASSET_MAX_PER_CHAPTER))
    if budget is not None:
        # Cumulative cap: never realize more than the run has left, regardless of
        # how many times this function is invoked across the top-up loop.
        limit = min(limit, budget.remaining)
    if not pending or limit == 0 or not settings.IMAGE_GEN_ENABLED:
        return _drop_unrendered_figures(questions)

    sem = asyncio.Semaphore(2)
    realized = 0

    async def _try(q: dict) -> bool:
        async with sem:
            try:
                return await _realize_one_figure(q)
            except Exception as exc:
                logger.warning(f"[asset] figure realization failed (non-fatal): {exc}")
                return False

    batch = max(1, limit)
    for start in range(0, len(pending), batch):
        if realized >= limit:
            break
        window = pending[start:start + batch]
        results = await asyncio.gather(*[_try(q) for q in window])
        realized += sum(1 for ok in results if ok)

    if budget is not None:
        budget.take(realized)

    return _drop_unrendered_figures(questions)

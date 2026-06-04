"""
pdf_extractor.py  —  Full-fidelity PDF extraction using pymupdf.

Extracts text, tables, embedded images (with OCR), and math formulas.
Returns EnhancedChunk objects (subclass of TextChunk) so all existing
callers of pdf_service.parse_pdf_into_chunks() work without any changes.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

try:
    import fitz  # PyMuPDF
    _PYMUPDF_AVAILABLE = True
except ImportError:
    _PYMUPDF_AVAILABLE = False
    logger.warning("PyMuPDF not installed — enhanced PDF extraction unavailable")

try:
    import pytesseract
    from PIL import Image as PILImage
    _OCR_AVAILABLE = True
except ImportError:
    _OCR_AVAILABLE = False
    logger.warning("pytesseract/Pillow not installed — image OCR unavailable")

# Import helpers and TextChunk from pdf_service (no duplication)
from app.services.pdf_service import (
    TextChunk,
    _find_chapter_match,
    _is_toc_like_page,
    _resolve_topic,
    _extract_key_terms,
    _teaching_density,
    _is_skip_block,
    _split_into_sub_chunks,
    _FORMULA_RE,
    _EXAMPLE_RE,
    _SECTION_RE,
)

# Font name fragments that indicate a math/symbol font.
# Covers common math font families: STIX (OpenType math), CM (TeX Computer Modern),
# Symbol, MathJax, and generic italic (single-letter math variables).
_MATH_FONT_FRAGMENTS = (
    "STIX",          # STIX General/Two — widely used in academic/OpenStax PDFs
    "CMMI", "CMSY", "CMEX",  # TeX Computer Modern math fonts
    "Symbol",        # PostScript Symbol font
    "MathJax",       # MathJax SVG/font fallbacks
    "NimbusRomNo9L", # Ghostscript math fallback
)


@dataclass
class EnhancedChunk(TextChunk):
    """TextChunk enriched with image OCR, table text, and math formula content."""
    image_texts: list[str] = field(default_factory=list)
    table_texts: list[str] = field(default_factory=list)
    math_text: str = ""
    has_images: bool = False
    has_tables: bool = False
    has_math_font: bool = False   # True if math fonts detected on any page in range
    graph_page_nums: list[int] = field(default_factory=list)  # pages with vector graphics
    figure_rects: list[dict] = field(default_factory=list)  # {"page_num": int, "rect": [x0, y0, x1, y1]}
    math_rects: list[dict] = field(default_factory=list)    # {"page_num": int, "rect": [x0, y0, x1, y1]}


# ── OCR helper ────────────────────────────────────────────────────────────────

def _safe_ocr(pil_img) -> str:
    """
    Run pytesseract on a PIL image, returning the extracted text or "".
    Handles the macOS pytesseract bug where stderr contains binary bytes
    (causes UnicodeDecodeError inside pytesseract's get_errors()).
    """
    import tempfile, os, subprocess, shutil
    try:
        # Preferred path: use the Python API (fast)
        return pytesseract.image_to_string(pil_img, timeout=15).strip()
    except UnicodeDecodeError:
        pass  # Fall through to file-based path below
    except pytesseract.TesseractNotFoundError:
        raise
    except Exception:
        return ""

    # Fallback: write to a temp file and invoke tesseract CLI directly
    try:
        tesseract_cmd = shutil.which("tesseract") or "tesseract"
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_in:
            pil_img.save(tmp_in.name, format="PNG")
            tmp_out = tmp_in.name + "_out"
        try:
            result = subprocess.run(
                [tesseract_cmd, tmp_in.name, tmp_out, "-l", "eng"],
                capture_output=True, timeout=15,
            )
            out_file = tmp_out + ".txt"
            if os.path.exists(out_file):
                with open(out_file, "r", encoding="utf-8", errors="replace") as f:
                    return f.read().strip()
        finally:
            try:
                os.unlink(tmp_in.name)
            except OSError:
                pass
            for ext in (".txt",):
                try:
                    os.unlink(tmp_out + ext)
                except OSError:
                    pass
    except Exception as exc:
        logger.debug(f"Fallback OCR failed: {exc}")
    return ""


# ── Per-page extraction ────────────────────────────────────────────────────────

def _extract_page_data(page, doc, ocr_available: bool) -> dict:
    """
    Extract all content from a single fitz.Page.
    Returns a dict: {text, table_texts, image_texts, math_spans}
    """
    # ── Text (markdown mode preserves headings, bullet lists) ─────────────
    try:
        text = page.get_text("markdown") or ""
    except Exception:
        text = page.get_text("text") or ""

    # Fall back if markdown output is mostly non-ASCII garbage
    if text and len(text.encode("ascii", errors="ignore")) / max(len(text.encode()), 1) < 0.4:
        text = page.get_text("text") or ""

    # ── Tables ────────────────────────────────────────────────────────────────
    table_texts: list[str] = []
    try:
        finder = page.find_tables()
        for tbl in finder.tables:
            rows = tbl.extract()  # list[list[str|None]]
            if not rows or len(rows) < 2:
                continue
            # Skip sparse tables — they're usually OpenStax coloured formatting boxes,
            # not real data tables (real tables have ≥30% of cells filled).
            total_cells = sum(len(r) for r in rows)
            filled_cells = sum(1 for r in rows for c in r if (c or "").strip())
            if total_cells > 0 and filled_cells / total_cells < 0.3:
                continue
            md_rows = []
            for row in rows:
                cells = [str(c).strip() if c else "" for c in row]
                row_text = " | ".join(cells)
                if row_text.strip(" |"):  # skip entirely empty rows
                    md_rows.append(row_text)
            if len(md_rows) >= 2:
                table_texts.append("\n".join(md_rows))
    except Exception as exc:
        logger.debug(f"Table extraction failed on page {page.number}: {exc}")

    # ── Math detection (must run before image OCR which references has_math_font_on_page) ─
    math_spans: list[str] = []
    page_math_rects: list[dict] = []
    has_math_font_on_page = False
    try:
        rawdict = page.get_text("rawdict")
        for block in rawdict.get("blocks", []):
            block_has_math = False
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    font = span.get("font", "")
                    is_math_font = any(frag in font for frag in _MATH_FONT_FRAGMENTS)
                    if is_math_font:
                        has_math_font_on_page = True
                        block_has_math = True
                        span_text = span.get("text", "").strip()
                        if span_text and len(span_text) > 1:
                            math_spans.append(span_text)
            if block_has_math and "bbox" in block:
                page_math_rects.append({"page_num": page.number + 1, "rect": list(block["bbox"])})
    except Exception as exc:
        logger.debug(f"rawdict math extraction failed on page {page.number}: {exc}")

    # If math fonts detected but no text decoded, extract formula lines from text layer
    if has_math_font_on_page and not math_spans:
        import re as _re
        _formula_line_re = _re.compile(
            r"[=÷×±√∑∫µσ²]|"
            r"\b\d+\s*/\s*\d+\b|"           # fractions like 1/2
            r"\b[A-Za-z]\s*=\s*[\d(]|"      # variable = number
            r"\b(?:P|E|Var|SD|SE)\s*\(|"    # P(), E(), Var(), etc.
            r"z\s*=|t\s*=|χ²|α\s*=|β\s*="
        )
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and _formula_line_re.search(stripped):
                math_spans.append(stripped)

    # ── Images → OCR ──────────────────────────────────────────────────────────
    # Strategy:
    #   1. Try embedded raster images (fast, precise)
    #   2. If page has NO extractable text at all, render whole page → OCR
    #      (handles fully scanned/image-based PDFs)
    image_texts: list[str] = []
    if ocr_available:
        # Path 1: embedded raster images
        try:
            for img_ref in page.get_images(full=True):
                xref = img_ref[0]
                try:
                    img_info = doc.extract_image(xref)
                    img_bytes = img_info.get("image", b"")
                    if not img_bytes:
                        continue
                    pil_img = PILImage.open(io.BytesIO(img_bytes)).convert("RGB")
                    if pil_img.width < 80 or pil_img.height < 80:
                        continue  # skip tiny icons/bullets
                    ocr_text = _safe_ocr(pil_img)
                    if ocr_text:
                        image_texts.append(ocr_text)
                except (pytesseract.TesseractNotFoundError, OSError):
                    return {
                        "text": text,
                        "table_texts": table_texts,
                        "image_texts": [],
                        "math_spans": math_spans,
                        "has_math_font": has_math_font_on_page,
                        "has_vector_graphics": False,
                        "_ocr_disabled": True,
                    }
                except Exception as exc:
                    logger.debug(f"Embedded image OCR failed: {exc}")
        except Exception as exc:
            logger.debug(f"get_images failed on page {page.number}: {exc}")

        # Path 2: if page appears blank (scanned PDF) render the whole page and OCR it
        if not text.strip() and not image_texts:
            try:
                mat = fitz.Matrix(2.0, 2.0)  # 2× zoom for better OCR accuracy
                pix = page.get_pixmap(matrix=mat, alpha=False)
                pil_img = PILImage.frombytes("RGB", (pix.width, pix.height), pix.samples)
                ocr_text = _safe_ocr(pil_img)
                if ocr_text:
                    image_texts.append(ocr_text)
                    text = ocr_text  # use as page text too
            except Exception as exc:
                logger.debug(f"Whole-page render OCR failed on page {page.number}: {exc}")

    # ── Vector graphic detection & Figure snipping ───────────────────────────
    has_vector_graphics = False
    page_figure_rects = []
    try:
        rects_to_merge = []
        for img in page.get_image_info():
            if "bbox" in img:
                rects_to_merge.append(fitz.Rect(img["bbox"]))
        for d in page.get_drawings():
            if "rect" in d:
                rects_to_merge.append(fitz.Rect(d["rect"]))
                
        merged_rects = []
        for r in rects_to_merge:
            r = fitz.Rect(r)
            r.x0 -= 10; r.y0 -= 10; r.x1 += 10; r.y1 += 10
            intersected = False
            for m in merged_rects:
                if r.intersects(m):
                    m.include_rect(r)
                    intersected = True
                    break
            if not intersected:
                merged_rects.append(r)
                
        for m in merged_rects:
            m.x0 += 10; m.y0 += 10; m.x1 -= 10; m.y1 -= 10
            m.intersect(page.rect)
            # Filter out tiny icons and extreme aspect ratios (lines, borders)
            if m.width > 40 and m.height > 40:
                aspect = m.width / m.height
                if 0.1 < aspect < 10.0:
                    page_figure_rects.append({"page_num": page.number + 1, "rect": list(m)})

        page_area = page.rect.width * page.rect.height
        if page_area > 0:
            graphic_area = sum(m.width * m.height for m in merged_rects)
            if graphic_area / page_area > 0.03:
                has_vector_graphics = True
    except Exception as exc:
        logger.debug(f"Vector graphic detection failed on page {page.number}: {exc}")

    return {
        "text": text,
        "table_texts": table_texts,
        "image_texts": image_texts,
        "math_spans": math_spans,
        "has_math_font": has_math_font_on_page,
        "has_vector_graphics": has_vector_graphics,
        "figure_rects": page_figure_rects,
        "math_rects": page_math_rects,
        "_ocr_disabled": False,
    }


# ── Resumable accumulator ──────────────────────────────────────────────────────

class ChunkAccumulator:
    """
    Serialisable state machine that turns a sequence of pages into EnhancedChunks.

    Drives chapter/section detection, accumulates a rolling buffer, and flushes
    EnhancedChunks at section boundaries or when the buffer exceeds max_chunk_chars.
    The full internal state is JSON-serialisable via `serialize()`, so an ingest
    can checkpoint between page windows and resume from a stored dict.
    """

    _STATE_KEYS = (
        "current_chapter_num", "current_chapter_title", "current_section_title",
        "current_topic",
        "buffer_lines", "buffer_image_texts", "buffer_table_texts",
        "buffer_math_spans", "buffer_has_math_font", "buffer_graph_pages",
        "buffer_figure_rects", "buffer_math_rects", "buffer_page_start",
    )

    def __init__(
        self,
        min_chunk_chars: int = 300,
        max_chunk_chars: int = 3000,
        state: dict | None = None,
    ):
        self.min_chunk_chars = min_chunk_chars
        self.max_chunk_chars = max_chunk_chars
        if state:
            self._load_state(state)
        else:
            self.current_chapter_num = 0
            self.current_chapter_title = "Unknown"
            self.current_section_title = "Introduction"
            self.current_topic = "General"
            self.buffer_lines: list[str] = []
            self.buffer_image_texts: list[str] = []
            self.buffer_table_texts: list[str] = []
            self.buffer_math_spans: list[str] = []
            self.buffer_has_math_font: bool = False
            self.buffer_graph_pages: list[int] = []
            self.buffer_figure_rects: list[dict] = []
            self.buffer_math_rects: list[dict] = []
            self.buffer_page_start: int = 1

    def _load_state(self, state: dict) -> None:
        for k in self._STATE_KEYS:
            if k in state:
                setattr(self, k, state[k])
        # Re-default anything missing (forward-compatible)
        self.current_chapter_num = getattr(self, "current_chapter_num", 0)
        self.current_chapter_title = getattr(self, "current_chapter_title", "Unknown")
        self.current_section_title = getattr(self, "current_section_title", "Introduction")
        self.current_topic = getattr(self, "current_topic", "General")
        self.buffer_lines = list(getattr(self, "buffer_lines", []))
        self.buffer_image_texts = list(getattr(self, "buffer_image_texts", []))
        self.buffer_table_texts = list(getattr(self, "buffer_table_texts", []))
        self.buffer_math_spans = list(getattr(self, "buffer_math_spans", []))
        self.buffer_has_math_font = bool(getattr(self, "buffer_has_math_font", False))
        self.buffer_graph_pages = list(getattr(self, "buffer_graph_pages", []))
        self.buffer_figure_rects = list(getattr(self, "buffer_figure_rects", []))
        self.buffer_math_rects = list(getattr(self, "buffer_math_rects", []))
        self.buffer_page_start = int(getattr(self, "buffer_page_start", 1))

    def serialize(self) -> dict:
        return {k: getattr(self, k) for k in self._STATE_KEYS}

    def _flush(self, page_end: int) -> list[EnhancedChunk]:
        """Emit chunks for whatever is in the buffer; reset the buffer."""
        out: list[EnhancedChunk] = []
        text = "\n".join(self.buffer_lines).strip()
        if len(text) < self.min_chunk_chars or _is_skip_block(text):
            self._reset_buffer()
            return out

        math_text = " ".join(dict.fromkeys(self.buffer_math_spans))[:500]
        for sub in _split_into_sub_chunks(text, self.max_chunk_chars):
            if len(sub) < self.min_chunk_chars:
                continue
            out.append(EnhancedChunk(
                chapter_num=self.current_chapter_num,
                chapter_title=self.current_chapter_title,
                section_title=self.current_section_title,
                topic_tag=self.current_topic,
                text=sub.strip(),
                page_start=self.buffer_page_start,
                page_end=page_end,
                has_formula=bool(_FORMULA_RE.search(sub)) or bool(self.buffer_math_spans),
                has_example=bool(_EXAMPLE_RE.search(sub)),
                teaching_density=_teaching_density(sub),
                key_terms=_extract_key_terms(sub),
                image_texts=list(self.buffer_image_texts),
                table_texts=list(self.buffer_table_texts),
                math_text=math_text,
                has_images=bool(self.buffer_image_texts),
                has_tables=bool(self.buffer_table_texts),
                has_math_font=self.buffer_has_math_font,
                graph_page_nums=list(self.buffer_graph_pages),
                figure_rects=list(self.buffer_figure_rects),
                math_rects=list(self.buffer_math_rects),
            ))
        self._reset_buffer()
        return out

    def _reset_buffer(self) -> None:
        self.buffer_lines = []
        self.buffer_image_texts = []
        self.buffer_table_texts = []
        self.buffer_math_spans = []
        self.buffer_has_math_font = False
        self.buffer_graph_pages = []
        self.buffer_figure_rects = []
        self.buffer_math_rects = []

    def feed_page(self, page, doc, ocr_active: bool) -> tuple[list[EnhancedChunk], bool]:
        """
        Process a single fitz page. Returns (chunks_flushed_on_this_page, ocr_active_after).
        `ocr_active_after` is False if this page hit a Tesseract-not-found condition.
        """
        page_num = page.number + 1  # 1-based
        out: list[EnhancedChunk] = []

        try:
            page_data = _extract_page_data(page, doc, ocr_active)
        except Exception as exc:
            logger.warning(f"Page {page_num} extraction failed: {exc}")
            try:
                page.clean_contents()
            except Exception:
                pass
            return out, ocr_active

        if page_data.get("_ocr_disabled"):
            ocr_active = False

        raw_text = page_data["text"]
        if not raw_text.strip():
            try:
                page.clean_contents()
            except Exception:
                pass
            return out, ocr_active

        ch_match = None if _is_toc_like_page(raw_text) else _find_chapter_match(raw_text)
        if ch_match:
            out.extend(self._flush(page_num - 1))
            self.buffer_page_start = page_num
            self.current_chapter_num, self.current_chapter_title = ch_match
            self.current_topic = _resolve_topic(self.current_chapter_title)
            self.current_section_title = "Introduction"

        for sec_m in _SECTION_RE.finditer(raw_text):
            out.extend(self._flush(page_num - 1))
            self.buffer_page_start = page_num
            self.current_section_title = sec_m.group(2).strip()

        self.buffer_lines.extend(raw_text.splitlines())
        self.buffer_image_texts.extend(page_data["image_texts"])
        self.buffer_table_texts.extend(page_data["table_texts"])
        self.buffer_math_spans.extend(page_data["math_spans"])
        self.buffer_has_math_font = self.buffer_has_math_font or page_data.get("has_math_font", False)
        if page_data.get("has_vector_graphics"):
            self.buffer_graph_pages.append(page_num)
        if page_data.get("figure_rects"):
            self.buffer_figure_rects.extend(page_data["figure_rects"])
        if page_data.get("math_rects"):
            self.buffer_math_rects.extend(page_data["math_rects"])

        if sum(len(l) for l in self.buffer_lines) >= self.max_chunk_chars:
            out.extend(self._flush(page_num))
            self.buffer_page_start = page_num + 1

        try:
            page.clean_contents()
        except Exception:
            pass
        return out, ocr_active

    def finalize(self, last_page: int) -> list[EnhancedChunk]:
        """Final flush — call once after the last page has been fed."""
        return self._flush(last_page)


def process_page_window(
    doc,
    start_page: int,
    end_page: int,
    accumulator: "ChunkAccumulator",
    ocr_active: bool,
) -> tuple[list[EnhancedChunk], bool]:
    """
    Feed pages [start_page, end_page) (0-based, exclusive end) into the accumulator.
    Returns (flushed_chunks_in_this_window, ocr_active_after).
    """
    out: list[EnhancedChunk] = []
    end_page = min(end_page, doc.page_count)
    for page in doc.pages(start_page, end_page):
        page_chunks, ocr_active = accumulator.feed_page(page, doc, ocr_active)
        out.extend(page_chunks)
    return out, ocr_active


# ── Main public API ────────────────────────────────────────────────────────────

def extract_enhanced_chunks(
    file_bytes: bytes,
    max_pages: int | None = None,
    concurrency: int = 1,
    min_chunk_chars: int = 300,
    max_chunk_chars: int = 3000,
) -> list[EnhancedChunk]:
    """
    Parse a PDF into EnhancedChunk objects using pymupdf (one-shot, synchronous).

    For resumable, windowed processing, build a ChunkAccumulator yourself and
    drive it with process_page_window() — that's what the Celery ingest task does.
    """
    if not _PYMUPDF_AVAILABLE:
        raise RuntimeError("PyMuPDF (fitz) is not installed")

    accumulator = ChunkAccumulator(
        min_chunk_chars=min_chunk_chars,
        max_chunk_chars=max_chunk_chars,
    )
    chunks: list[EnhancedChunk] = []
    ocr_active = _OCR_AVAILABLE

    doc = fitz.open(stream=file_bytes, filetype="pdf")
    last_page = min(max_pages or doc.page_count, doc.page_count)
    window_chunks, _ = process_page_window(doc, 0, last_page, accumulator, ocr_active)
    chunks.extend(window_chunks)
    chunks.extend(accumulator.finalize(last_page))
    doc.close()

    logger.info(
        f"Enhanced extraction: {len(chunks)} chunks, "
        f"{sum(1 for c in chunks if c.has_images)} with images, "
        f"{sum(1 for c in chunks if c.has_tables)} with tables, "
        f"{sum(1 for c in chunks if c.has_math_font)} with math fonts, "
        f"{sum(1 for c in chunks if c.graph_page_nums)} with vector graphics"
    )

    return chunks


async def describe_graph_chunks(
    chunks: list,
    pdf_bytes: bytes,
    concurrency: int = 3,
    job_id: str | None = None,
) -> None:
    """
    For each chunk that has figure_rects, render those specific rects and call
    a vision model to describe the charts. Descriptions are appended to
    chunk.image_texts in-place. Non-fatal — errors are logged and skipped.
    """
    import asyncio as _asyncio
    from app.core.config import settings as _settings

    try:
        from app.services.llm_service import GeminiClient
        vision_client = GeminiClient()
    except Exception as exc:
        logger.warning(f"describe_graph_chunks: could not initialise vision client: {exc}")
        return

    if not _PYMUPDF_AVAILABLE:
        return

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    semaphore = _asyncio.Semaphore(concurrency)

    # Hash cache: render → MD5 → check MongoDB before calling API.
    # Same page content (re-upload, second edition) reuses cached description.
    import hashlib as _hashlib
    _cache_collection = None
    try:
        if getattr(_settings, "MONGODB_ENABLED", False):
            from app.services.mongo_vector_store import _get_collection as _get_mongo
            _cache_db = (await _get_mongo("page_description_cache")).database
            _cache_collection = _cache_db["page_description_cache"]
    except Exception:
        pass  # cache unavailable — just call API every time

    async def _describe_fig(fig_dict: dict, context: str) -> str:
        async with semaphore:
            try:
                page_num = fig_dict["page_num"]
                rect = fitz.Rect(fig_dict["rect"])
                page = doc[page_num - 1]  # fitz is 0-indexed
                mat = fitz.Matrix(2.0, 2.0) # slightly higher res for snippets
                pix = page.get_pixmap(matrix=mat, alpha=False, clip=rect)
                img_bytes = pix.tobytes("png")

                # Check hash cache before calling API
                page_hash = _hashlib.md5(img_bytes).hexdigest()
                if _cache_collection is not None:
                    try:
                        cached = await _cache_collection.find_one({"_id": page_hash})
                        if cached:
                            logger.debug(f"Vision cache hit for figure on page {page_num}")
                            return cached.get("description", "")
                    except Exception:
                        pass

                delay = max(getattr(_settings, "GEMINI_VISION_DELAY_SECONDS", 0.0), 0.0)
                if delay:
                    await _asyncio.sleep(delay)
                
                # Context prompt explicitly asks for description
                full_prompt = f"{context}\n\nPlease describe this chart/figure in detail."
                description = await vision_client.describe_image(img_bytes, context=full_prompt)

                # Store in cache (fire-and-forget)
                if _cache_collection is not None and description:
                    try:
                        from datetime import datetime, timezone
                        await _cache_collection.replace_one(
                            {"_id": page_hash},
                            {"_id": page_hash, "description": description,
                             "created_at": datetime.now(timezone.utc)},
                            upsert=True,
                        )
                    except Exception:
                        pass

                return description
            except Exception as exc:
                logger.debug(f"Vision description failed for figure on page {fig_dict.get('page_num')}: {exc}")
                return ""

    chunk_fig_map = []  # (chunk_index, fig_dict, context)

    for i, chunk in enumerate(chunks):
        if not getattr(chunk, "figure_rects", None):
            continue
        context = f"Context: {chunk.chapter_title} — {chunk.section_title} (pp. {chunk.page_start}–{chunk.page_end})"
        for fig in chunk.figure_rects:
            chunk_fig_map.append((i, fig, context))

    if not chunk_fig_map:
        doc.close()
        return

    logger.info(f"describe_graph_chunks: sending {len(chunk_fig_map)} figure snips to Gemini Vision")

    descriptions = []
    total = len(chunk_fig_map)
    for i in range(0, total, concurrency):
        batch = chunk_fig_map[i:i+concurrency]
        batch_results = await _asyncio.gather(
            *[_describe_fig(fig, ctx) for (_, fig, ctx) in batch],
            return_exceptions=True,
        )
        descriptions.extend(batch_results)
        if (i // concurrency) % 5 == 0:
            logger.info(f"describe_graph_chunks: {min(i + len(batch), total)}/{total} figures described")

    for (chunk_idx, _, _), desc in zip(chunk_fig_map, descriptions):
        if isinstance(desc, str) and desc:
            chunks[chunk_idx].image_texts.append(desc)
            chunks[chunk_idx].has_images = True

    doc.close()
    logger.info(
        f"describe_graph_chunks: added descriptions to "
        f"{sum(1 for c in chunks if c.has_images)} chunks"
    )

async def transcribe_math_chunks(
    chunks: list,
    pdf_bytes: bytes,
    concurrency: int = 3,
    job_id: str | None = None,
) -> None:
    """
    For each chunk that has math_rects, render those specific rects and call
    a vision model to extract the Math formulas as LaTeX. 
    LaTeX is appended to chunk.math_text.
    """
    import asyncio as _asyncio
    from app.core.config import settings as _settings

    try:
        from app.services.llm_service import OpenAIClient, AnthropicClient
        from app.core.config import settings as _cfg
        if _cfg.OPENAI_API_KEY:
            vision_client = OpenAIClient()
        elif _cfg.ANTHROPIC_API_KEY:
            vision_client = AnthropicClient()
        else:
            logger.warning("transcribe_math_chunks: no vision provider available (set OPENAI_API_KEY or ANTHROPIC_API_KEY)")
            return
    except Exception as exc:
        logger.warning(f"transcribe_math_chunks: could not initialise vision client: {exc}")
        return

    if not _PYMUPDF_AVAILABLE:
        return

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    semaphore = _asyncio.Semaphore(concurrency)

    async def _transcribe_math(math_dict: dict) -> str:
        async with semaphore:
            try:
                page_num = math_dict["page_num"]
                rect = fitz.Rect(math_dict["rect"])
                # Add a small padding to math rects
                rect.x0 -= 5; rect.y0 -= 5; rect.x1 += 5; rect.y1 += 5
                page = doc[page_num - 1]
                mat = fitz.Matrix(2.5, 2.5) # higher res for small math
                pix = page.get_pixmap(matrix=mat, alpha=False, clip=rect)
                img_bytes = pix.tobytes("png")

                delay = max(getattr(_settings, "GEMINI_VISION_DELAY_SECONDS", 0.0), 0.0)
                if delay:
                    await _asyncio.sleep(delay)
                
                prompt = (
                    "Transcribe every mathematical formula visible in this image into valid LaTeX. "
                    "Output ONLY the LaTeX — no explanations, no prose. "
                    "Surround inline math with $...$ and display math with $$...$$. "
                    "If no formula is visible respond with exactly: NO_MATH"
                )
                latex = await vision_client.describe_image(img_bytes, context=prompt)
                if latex == "NO_MATH":
                    latex = ""
                return latex
            except Exception as exc:
                logger.debug(f"Math vision transcription failed for page {math_dict.get('page_num')}: {exc}")
                return ""

    chunk_math_map = []
    for i, chunk in enumerate(chunks):
        if not getattr(chunk, "math_rects", None):
            continue
        for mrect in chunk.math_rects:
            chunk_math_map.append((i, mrect))

    if not chunk_math_map:
        doc.close()
        return

    logger.info(f"transcribe_math_chunks: sending {len(chunk_math_map)} math snips to Gemini Vision")

    results = []
    total = len(chunk_math_map)
    for i in range(0, total, concurrency):
        batch = chunk_math_map[i:i+concurrency]
        batch_results = await _asyncio.gather(
            *[_transcribe_math(m) for (_, m) in batch],
            return_exceptions=True,
        )
        results.extend(batch_results)
        if (i // concurrency) % 5 == 0:
            logger.info(f"transcribe_math_chunks: {min(i + len(batch), total)}/{total} formulas transcribed")

    for (chunk_idx, _), latex in zip(chunk_math_map, results):
        if isinstance(latex, str) and latex:
            # Append to existing math text
            chunks[chunk_idx].math_text += f"\n{latex}\n"

    doc.close()
    logger.info(f"transcribe_math_chunks: finished processing math snippets")

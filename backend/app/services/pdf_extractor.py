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
    # ── Text (markdown mode preserves headings, bullet lists) ─────────────────
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
            if not rows:
                continue
            md_rows = []
            for row in rows:
                cells = [str(c).strip() if c else "" for c in row]
                md_rows.append(" | ".join(cells))
            if md_rows:
                table_texts.append("\n".join(md_rows))
    except Exception as exc:
        logger.debug(f"Table extraction failed on page {page.number}: {exc}")

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
                        "math_spans": [],
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

    # ── Math detection ────────────────────────────────────────────────────────
    # Also collects text-block bounding boxes (reused by vector detection below
    # to distinguish charts from coloured text-box formatting).
    math_spans: list[str] = []
    has_math_font_on_page = False
    text_block_rects: list[tuple] = []   # (x0, y0, x1, y1) of every text block
    try:
        rawdict = page.get_text("rawdict")
        for block in rawdict.get("blocks", []):
            if block.get("type") == 0:  # text block
                b = block.get("bbox")
                if b:
                    text_block_rects.append(b)
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    font = span.get("font", "")
                    is_math_font = any(frag in font for frag in _MATH_FONT_FRAGMENTS)
                    if is_math_font:
                        has_math_font_on_page = True
                        span_text = span.get("text", "").strip()
                        if span_text and len(span_text) > 1:
                            math_spans.append(span_text)
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

    # ── Vector graphic detection ─────────────────────────────────────────────
    # Goal: detect pages with genuine charts/graphs while ignoring coloured
    # text-box formatting (learning-objective boxes, example callouts, etc.)
    # that is common in textbooks such as OpenStax.
    #
    # Key insight:
    #   - Coloured formatting box  → filled rectangle that CONTAINS text blocks
    #   - Chart bar / pie slice    → filled shape with NO text blocks inside it
    #
    # Algorithm:
    #   For each filled shape, compute how much of its area overlaps with
    #   text blocks (re-using rawdict already fetched for math detection).
    #   Only shapes with < 30% text-overlap are counted as "graphic area".
    #   Flag the page when graphic area > 5% of the page.
    has_vector_graphics = False
    try:
        page_area = page.rect.width * page.rect.height
        if page_area > 0:
            graphic_area = 0.0
            for d in page.get_drawings():
                if "f" not in d.get("type", ""):    # skip pure stroked lines/borders
                    continue
                r = d.get("rect")
                if not r:
                    continue
                rx0, ry0, rx1, ry1 = r[0], r[1], r[2], r[3]
                w, h = abs(rx1 - rx0), abs(ry1 - ry0)
                if w < 5 or h < 5:                  # skip dots / tick marks
                    continue
                shape_area = w * h

                # Sum the area of this shape that is covered by text blocks
                text_overlap = 0.0
                for tx0, ty0, tx1, ty1 in text_block_rects:
                    ix = max(0.0, min(rx1, tx1) - max(rx0, tx0))
                    iy = max(0.0, min(ry1, ty1) - max(ry0, ty0))
                    text_overlap += ix * iy

                # If ≥ 30% of the shape is covered by text → formatting, not chart
                if text_overlap / shape_area < 0.30:
                    graphic_area += shape_area

            has_vector_graphics = (graphic_area / page_area) > 0.05
    except Exception as exc:
        logger.debug(f"Drawing detection failed on page {page.number}: {exc}")

    return {
        "text": text,
        "table_texts": table_texts,
        "image_texts": image_texts,
        "math_spans": math_spans,
        "has_math_font": has_math_font_on_page,
        "has_vector_graphics": has_vector_graphics,
        "_ocr_disabled": False,
    }


# ── Main public API ────────────────────────────────────────────────────────────

def extract_enhanced_chunks(
    file_bytes: bytes,
    max_pages: int = 620,
    min_chunk_chars: int = 300,
    max_chunk_chars: int = 3000,
) -> list[EnhancedChunk]:
    """
    Parse a PDF into EnhancedChunk objects using pymupdf.

    Raises RuntimeError if pymupdf is not installed (caller falls back to
    the pdfplumber-based parser in pdf_service.py).
    """
    if not _PYMUPDF_AVAILABLE:
        raise RuntimeError("PyMuPDF (fitz) is not installed")

    chunks: list[EnhancedChunk] = []

    # ── State ──────────────────────────────────────────────────────────────────
    current_chapter_num = 0
    current_chapter_title = "Unknown"
    current_section_title = "Introduction"
    current_topic = "General"

    buffer_lines: list[str] = []
    buffer_image_texts: list[str] = []
    buffer_table_texts: list[str] = []
    buffer_math_spans: list[str] = []
    buffer_has_math_font: bool = False
    buffer_graph_pages: list[int] = []
    buffer_page_start = 1

    ocr_active = _OCR_AVAILABLE

    doc = fitz.open(stream=file_bytes, filetype="pdf")
    pages = doc.pages(0, min(max_pages, doc.page_count))

    # Lazy import of section regex (defined in pdf_service)
    import re
    _SECTION_RE = re.compile(
        r"^(\d{1,2}\.\d{1,2})\s+([A-Z][^\n]{5,80})$",
        re.MULTILINE,
    )

    def _flush(page_end: int):
        nonlocal buffer_lines, buffer_image_texts, buffer_table_texts, buffer_math_spans, buffer_has_math_font, buffer_graph_pages, buffer_page_start

        text = "\n".join(buffer_lines).strip()
        if len(text) < min_chunk_chars or _is_skip_block(text):
            buffer_lines = []
            buffer_image_texts = []
            buffer_table_texts = []
            buffer_math_spans = []
            buffer_has_math_font = False
            buffer_graph_pages = []
            return

        sub_chunks = _split_into_sub_chunks(text, max_chunk_chars)
        math_text = " ".join(dict.fromkeys(buffer_math_spans))[:500]  # deduplicate + cap

        for sub in sub_chunks:
            if len(sub) < min_chunk_chars:
                continue
            chunks.append(EnhancedChunk(
                chapter_num=current_chapter_num,
                chapter_title=current_chapter_title,
                section_title=current_section_title,
                topic_tag=current_topic,
                text=sub.strip(),
                page_start=buffer_page_start,
                page_end=page_end,
                has_formula=bool(_FORMULA_RE.search(sub)) or bool(buffer_math_spans),
                has_example=bool(_EXAMPLE_RE.search(sub)),
                teaching_density=_teaching_density(sub),
                key_terms=_extract_key_terms(sub),
                # Enhanced fields
                image_texts=list(buffer_image_texts),
                table_texts=list(buffer_table_texts),
                math_text=math_text,
                has_images=bool(buffer_image_texts),
                has_tables=bool(buffer_table_texts),
                has_math_font=buffer_has_math_font,
                graph_page_nums=list(buffer_graph_pages),
            ))

        buffer_lines = []
        buffer_image_texts = []
        buffer_table_texts = []
        buffer_math_spans = []
        buffer_has_math_font = False
        buffer_graph_pages = []

    for page in pages:
        page_num = page.number + 1  # 1-based

        try:
            page_data = _extract_page_data(page, doc, ocr_active)
        except Exception as exc:
            logger.warning(f"Page {page_num} extraction failed: {exc}")
            page.clean_contents()
            continue

        if page_data.get("_ocr_disabled"):
            ocr_active = False

        raw_text = page_data["text"]
        if not raw_text.strip():
            page.clean_contents()
            continue

        # Detect chapter start (skip TOC pages)
        ch_match = None if _is_toc_like_page(raw_text) else _find_chapter_match(raw_text)
        if ch_match:
            _flush(page_num - 1)
            buffer_page_start = page_num
            current_chapter_num, current_chapter_title = ch_match
            current_topic = _resolve_topic(current_chapter_title)
            current_section_title = "Introduction"

        # Detect section changes
        sec_matches = list(_SECTION_RE.finditer(raw_text))
        if sec_matches:
            for sec_m in sec_matches:
                _flush(page_num - 1)
                buffer_page_start = page_num
                current_section_title = sec_m.group(2).strip()

        # Accumulate
        buffer_lines.extend(raw_text.splitlines())
        buffer_image_texts.extend(page_data["image_texts"])
        buffer_table_texts.extend(page_data["table_texts"])
        buffer_math_spans.extend(page_data["math_spans"])
        buffer_has_math_font = buffer_has_math_font or page_data.get("has_math_font", False)
        if page_data.get("has_vector_graphics"):
            buffer_graph_pages.append(page_num)

        # Flush if buffer is large
        total_chars = sum(len(l) for l in buffer_lines)
        if total_chars >= max_chunk_chars:
            _flush(page_num)
            buffer_page_start = page_num + 1

        page.clean_contents()

    # Final flush
    _flush(max_pages)
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
) -> None:
    """
    For each chunk that has vector-graphic pages, render those pages and call
    GPT-4o Vision to describe the charts. Descriptions are appended to
    chunk.image_texts in-place. Non-fatal — errors are logged and skipped.

    Requires OPENAI_API_KEY to be set in settings.
    """
    import asyncio as _asyncio

    try:
        from app.services.llm_service import OpenAIClient
        from app.core.config import settings
        if not settings.OPENAI_API_KEY:
            logger.info("OPENAI_API_KEY not set — skipping graph vision descriptions")
            return
        vision_client = OpenAIClient()
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
        from app.core.config import settings as _settings
        if getattr(_settings, "MONGODB_ENABLED", False):
            from app.services.mongo_vector_store import _get_collection as _get_mongo
            _cache_collection = await _get_mongo()
            # Use a sibling collection for descriptions
            _cache_collection = _cache_collection.database["page_description_cache"]
    except Exception:
        pass  # cache unavailable — just call API every time

    async def _describe_page(page_num: int, context: str) -> str:
        async with semaphore:
            try:
                page = doc[page_num - 1]  # fitz is 0-indexed
                mat = fitz.Matrix(1.5, 1.5)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                img_bytes = pix.tobytes("png")

                # Check hash cache before calling API
                page_hash = _hashlib.md5(img_bytes).hexdigest()
                if _cache_collection is not None:
                    try:
                        cached = await _cache_collection.find_one({"_id": page_hash})
                        if cached:
                            logger.debug(f"Vision cache hit for page {page_num}")
                            return cached.get("description", "")
                    except Exception:
                        pass

                description = await vision_client.describe_image(img_bytes, context=context)

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
                logger.debug(f"Vision description failed for page {page_num}: {exc}")
                return ""

    tasks = []
    chunk_page_map = []  # (chunk_index, page_num, context)

    for i, chunk in enumerate(chunks):
        if not chunk.graph_page_nums:
            continue
        context = f"{chunk.chapter_title} — {chunk.section_title} (pp. {chunk.page_start}–{chunk.page_end})"
        for page_num in chunk.graph_page_nums:
            chunk_page_map.append((i, page_num, context))

    if not chunk_page_map:
        doc.close()
        return

    logger.info(f"describe_graph_chunks: sending {len(chunk_page_map)} pages to GPT-4o Vision")

    descriptions = await _asyncio.gather(
        *[_describe_page(pn, ctx) for (_, pn, ctx) in chunk_page_map],
        return_exceptions=True,
    )

    for (chunk_idx, _, _ctx), desc in zip(chunk_page_map, descriptions):
        if isinstance(desc, str) and desc:
            chunks[chunk_idx].image_texts.append(desc)
            chunks[chunk_idx].has_images = True

    doc.close()
    logger.info(
        f"describe_graph_chunks: added descriptions to "
        f"{sum(1 for c in chunks if c.has_images)} chunks"
    )

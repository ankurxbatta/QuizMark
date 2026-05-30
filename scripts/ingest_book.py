#!/usr/bin/env python3
"""
ingest_book.py — Standalone script to ingest a PDF textbook directly into MongoDB.

Usage:
    python3 scripts/ingest_book.py [path/to/book.pdf]

Reads the PDF page-by-page, extracts text/tables/math/images, generates
embeddings via Ollama (nomic-embed-text), describes charts via Gemini Vision,
then stores everything directly in MongoDB (pdf_chunks collection).

No OpenAI key required. Reads credentials from .env in the project root.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ingest_book")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ── Load .env ──────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).parent.parent
_ENV_FILE = _REPO_ROOT / ".env"

def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not _ENV_FILE.exists():
        return env
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env

_ENV = _load_env()

MONGODB_URL    = os.environ.get("MONGODB_URL", _ENV.get("MONGODB_URL", "mongodb://localhost:27017"))
MONGODB_DB     = os.environ.get("MONGODB_DB_NAME", _ENV.get("MONGODB_DB_NAME", "marking_tools"))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", _ENV.get("OPENAI_API_KEY", ""))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", _ENV.get("GEMINI_API_KEY", ""))
GEMINI_MODEL   = os.environ.get("GENERATION_LLM_MODEL", _ENV.get("GENERATION_LLM_MODEL", "gemini-2.5-flash"))
GEMINI_BASE    = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_EMBED_MODEL = "gemini-embedding-001"  # truncated to 768-dim via outputDimensionality

MAX_PAGES      = 700
MIN_CHUNK_CHARS = 300
MAX_CHUNK_CHARS = 3000
BOOK_ID        = "IntroductoryBusinessStatistics-OP"

# ── Third-party imports ────────────────────────────────────────────────────────
try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("PyMuPDF not installed. Run: pip3 install PyMuPDF")

try:
    import httpx
except ImportError:
    sys.exit("httpx not installed. Run: pip3 install httpx")

try:
    import pymongo
    from pymongo import MongoClient
except ImportError:
    sys.exit("pymongo not installed. Run: pip3 install pymongo")

try:
    from PIL import Image as PILImage
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

try:
    import pytesseract
    _OCR_AVAILABLE = True
except ImportError:
    _OCR_AVAILABLE = False
    log.warning("pytesseract not available — raster image OCR disabled")


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class Chunk:
    chapter_num: int
    chapter_title: str
    section_title: str
    topic_tag: str
    text: str
    page_start: int
    page_end: int
    has_formula: bool
    has_example: bool
    teaching_density: float
    key_terms: list[str]
    image_texts: list[str] = field(default_factory=list)
    table_texts: list[str] = field(default_factory=list)
    math_text: str = ""
    has_images: bool = False
    has_tables: bool = False
    has_math_font: bool = False
    graph_page_nums: list[int] = field(default_factory=list)


# ── Regex patterns ─────────────────────────────────────────────────────────────

_CHAPTER_PATTERNS = [
    re.compile(r"^[ \t]*[Cc]hapter[ \t]+(\d{1,3})[ \t]*[:|]?[ \t]+([A-Z][^\n]{3,80})", re.MULTILINE),
    re.compile(r"^[ \t]*CHAPTER[ \t]+(\d{1,3})[ \t]*[:|]?[ \t]+([A-Z][^\n]{3,80})", re.MULTILINE),
    re.compile(r"^[ \t]*(\d{1,2})\.?[ \t]{1,4}([A-Z][a-zA-Z ]{5,70})[ \t]*$", re.MULTILINE),
]
_SECTION_RE = re.compile(r"^(\d{1,2}\.\d{1,2})\s+([A-Z][^\n]{5,80})$", re.MULTILINE)
_FORMULA_RE = re.compile(
    r"[=÷×±√∑∫µσ²]|"
    r"\b(?:s²|σ²|μ|x̄|ȳ|Σ|√)\b|"
    r"\b\w+\s*=\s*[\w\d()\[\]]+\s*/|"
    r"[A-Za-z]\s*[₀₁₂]\b|"
    r"\bz\s*=\s*|t\s*=\s*|F\s*=\s*|χ²",
    re.UNICODE,
)
_EXAMPLE_RE = re.compile(r"\bExample\s+\d+", re.IGNORECASE)
_TOC_ENTRY_RE = re.compile(
    r"^\s*(?:(?:chapter\s+)?\d{1,2}\.?\s+[A-Z][^\n]{3,90}|[A-Z][A-Za-z ]{3,90})"
    r"\s*(?:\.{2,}|\s{3,})\s*\d{1,4}\s*$",
    re.IGNORECASE | re.VERBOSE,
)
_FRONT_MATTER_RE = re.compile(
    r"^(?:preface|foreword|acknowledgments?|table\s+of\s+contents|contents|"
    r"copyright|dedication|about\s+the\s+authors?|answer\s+key|answers?|"
    r"solutions?|glossary|references|bibliography|index)$",
    re.IGNORECASE,
)
_TEACHING_SIGNALS = re.compile(
    r"\bdefin(?:ition|e[sd]?)\b|\btheor(?:em|y)\b|\bformula\b|"
    r"\bexample\s+\d+\b|\bsolution\b|\bnote\b|\bproperties?\s+of\b|"
    r"\bmean\b|\bprobability\b|\bdistribution\b|\bhypothesis\b|"
    r"\bregression\b|\bvariance\b|\bconfidence\b|\bcorrelation\b",
    re.IGNORECASE,
)
_SKIP_SIGNALS = re.compile(
    r"\bpractice\s+test\b|\bhomework\b|\breview\s+questions?\b|"
    r"\bchapter\s+review\b|\bkey\s+terms?\b|\bthis\s+openstax\b|"
    r"\bdownload\s+for\s+free\b|\btable\s+of\s+contents\b|\bappendix\b|\bindex\b",
    re.IGNORECASE | re.MULTILINE,
)
_MATH_FONT_FRAGMENTS = ("STIX", "CMMI", "CMSY", "CMEX", "Symbol", "MathJax", "NimbusRomNo9L")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_toc_like(text: str) -> bool:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return False
    first = " ".join(lines[:12]).lower()
    if "table of contents" in first:
        return True
    return sum(1 for l in lines[:80] if _TOC_ENTRY_RE.match(l)) >= 4


def _find_chapter(text: str) -> Optional[tuple[int, str]]:
    for pat in _CHAPTER_PATTERNS:
        for m in pat.finditer(text):
            try:
                num = int(m.group(1))
            except (IndexError, ValueError):
                continue
            if not (1 <= num <= 50):
                continue
            title = re.sub(r"\s*\.{2,}\s*\d+\s*$", "", m.group(2).strip()).strip()
            title = re.sub(r"\s+\d{1,4}\s*$", "", title).strip()
            if len(title) < 4 or _FRONT_MATTER_RE.match(re.sub(r"\s+", " ", title)):
                continue
            return num, title
    return None


def _is_skip_block(text: str) -> bool:
    if not text.strip():
        return True
    lines = text.splitlines()
    ex = sum(1 for l in lines if re.match(r"^\s*\d{1,3}\.\s+[A-Z]", l))
    if lines and ex / len(lines) > 0.35:
        return True
    return bool(_SKIP_SIGNALS.search(text[:300]))


def _teaching_density(text: str) -> float:
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return 0.0
    return sum(1 for l in lines if _TEACHING_SIGNALS.search(l)) / len(lines)


def _key_terms(text: str) -> list[str]:
    terms = [m.group(1) for m in re.finditer(
        r"([A-Z][a-z]+(?:\s+[A-Z]?[a-z]+){0,4})\s+(?:is|are)\s+(?:defined|called|known)", text)]
    stat_re = re.compile(
        r"\b(Standard Deviation|Variance|Mean|Median|Mode|Probability|Distribution|"
        r"Hypothesis|Regression|Correlation|Confidence Interval|Central Limit Theorem|"
        r"Normal Distribution|Binomial|Poisson|Chi-Square|ANOVA|p-value|t-test|z-score)\b",
        re.IGNORECASE,
    )
    terms += [m.group(1) for m in stat_re.finditer(text)]
    return list(dict.fromkeys(t.lower() for t in terms if len(t) > 3))


def _split_chunks(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    paras = re.split(r"\n{2,}", text)
    result, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 2 <= max_chars:
            cur = (cur + "\n\n" + p).strip()
        else:
            if cur:
                result.append(cur)
            cur = p
    if cur:
        result.append(cur)
    return result or [text[:max_chars]]


# ── PDF page extraction ────────────────────────────────────────────────────────

def _extract_page(page, doc, ocr_active: bool) -> dict:
    """Extract all content from a single fitz.Page."""
    # Text
    try:
        text = page.get_text("markdown") or ""
    except Exception:
        text = page.get_text("text") or ""
    if text and len(text.encode("ascii", errors="ignore")) / max(len(text.encode()), 1) < 0.4:
        text = page.get_text("text") or ""

    # Tables — skip sparse OpenStax formatting boxes (< 30% cells filled)
    table_texts: list[str] = []
    try:
        for tbl in page.find_tables().tables:
            rows = tbl.extract()
            if not rows or len(rows) < 2:
                continue
            total = sum(len(r) for r in rows)
            filled = sum(1 for r in rows for c in r if (c or "").strip())
            if total > 0 and filled / total < 0.3:
                continue
            md = []
            for r in rows:
                row_text = " | ".join(str(c).strip() if c else "" for c in r)
                if row_text.strip(" |"):
                    md.append(row_text)
            if len(md) >= 2:
                table_texts.append("\n".join(md))
    except Exception:
        pass

    # Image OCR
    image_texts: list[str] = []
    ocr_disabled = False
    if ocr_active and _OCR_AVAILABLE and _PIL_AVAILABLE:
        try:
            for img_ref in page.get_images(full=True):
                xref = img_ref[0]
                try:
                    info = doc.extract_image(xref)
                    img_bytes = info.get("image", b"")
                    if not img_bytes:
                        continue
                    pil = PILImage.open(io.BytesIO(img_bytes)).convert("RGB")
                    if pil.width < 80 or pil.height < 80:
                        continue
                    t = pytesseract.image_to_string(pil, timeout=15).strip()
                    if t:
                        image_texts.append(t)
                except pytesseract.TesseractNotFoundError:
                    ocr_disabled = True
                    break
                except Exception:
                    pass
        except Exception:
            pass

    # Math fonts
    math_spans: list[str] = []
    has_math_font = False
    text_block_rects: list[tuple] = []
    try:
        rawdict = page.get_text("rawdict")
        for block in rawdict.get("blocks", []):
            if block.get("type") == 0:
                b = block.get("bbox")
                if b:
                    text_block_rects.append(b)
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    font = span.get("font", "")
                    is_math = any(f in font for f in _MATH_FONT_FRAGMENTS)
                    if is_math:
                        has_math_font = True
                        t = span.get("text", "").strip()
                        if t and len(t) > 1:
                            math_spans.append(t)
    except Exception:
        pass

    # Formula lines fallback
    if has_math_font and not math_spans:
        fl = re.compile(
            r"[=÷×±√∑∫µσ²]|\b\d+\s*/\s*\d+\b|\b[A-Za-z]\s*=\s*[\d(]|"
            r"\b(?:P|E|Var|SD|SE)\s*\(|z\s*=|t\s*=|χ²|α\s*=|β\s*="
        )
        for ln in text.splitlines():
            s = ln.strip()
            if s and fl.search(s):
                math_spans.append(s)

    # Vector graphic detection (charts vs coloured formatting boxes)
    has_vector = False
    try:
        page_area = page.rect.width * page.rect.height
        if page_area > 0:
            graphic_area = 0.0
            for d in page.get_drawings():
                if "f" not in d.get("type", ""):
                    continue
                r = d.get("rect")
                if not r:
                    continue
                rx0, ry0, rx1, ry1 = r[0], r[1], r[2], r[3]
                w, h = abs(rx1 - rx0), abs(ry1 - ry0)
                if w < 5 or h < 5:
                    continue
                shape_area = w * h
                text_overlap = sum(
                    max(0.0, min(rx1, tx1) - max(rx0, tx0)) *
                    max(0.0, min(ry1, ty1) - max(ry0, ty0))
                    for tx0, ty0, tx1, ty1 in text_block_rects
                )
                if text_overlap / shape_area < 0.30:
                    graphic_area += shape_area
            has_vector = (graphic_area / page_area) > 0.05
    except Exception:
        pass

    return {
        "text": text,
        "table_texts": table_texts,
        "image_texts": image_texts,
        "math_spans": math_spans,
        "has_math_font": has_math_font,
        "has_vector": has_vector,
        "ocr_disabled": ocr_disabled,
    }


# ── Gemini Vision ──────────────────────────────────────────────────────────────

async def _describe_with_gemini(image_bytes: bytes, context: str) -> str:
    if not GEMINI_API_KEY:
        return ""
    b64 = base64.b64encode(image_bytes).decode()
    prompt = (
        "You are an expert at reading statistical charts and graphs in a business statistics textbook. "
        "Describe exactly what this chart or graph shows: the type of visualisation, "
        "axis labels, key values/ranges, data trends, and what statistical concept it demonstrates. "
        "Be specific and quantitative. If no meaningful chart is visible, respond: NO_CHART"
    )
    if OPENAI_API_KEY:
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"Context: {context}\n\n{prompt}"},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
                    ]
                }
            ],
            "max_tokens": 400
        }
        for attempt in range(4):
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                        json=payload,
                    )
                if resp.status_code in {429, 500, 503} and attempt < 3:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"].strip()
                return "" if text == "NO_CHART" else text
            except Exception as exc:
                log.debug(f"OpenAI vision failed on attempt {attempt+1}: {exc}")
                if attempt < 3:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                return ""
        return ""
    else:
        payload = {
            "contents": [{"parts": [
                {"text": f"Context: {context}\n\n{prompt}"},
                {"inline_data": {"mime_type": "image/png", "data": b64}},
            ]}],
            "generationConfig": {
                "maxOutputTokens": 400,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        for attempt in range(4):
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent",
                        params={"key": GEMINI_API_KEY},
                        json=payload,
                    )
                if resp.status_code in {429, 500, 503} and attempt < 3:
                    await asyncio.sleep(4 * (attempt + 1))
                    continue
                resp.raise_for_status()
                text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                return "" if text == "NO_CHART" else text
            except Exception as exc:
                log.debug(f"Gemini vision failed on attempt {attempt+1}: {exc}")
                if attempt < 3:
                    await asyncio.sleep(4 * (attempt + 1))
                    continue
                return ""
        return ""


async def _add_graph_descriptions(chunks: list[Chunk], pdf_bytes: bytes) -> None:
    """Render graph pages and describe via Gemini Vision."""
    if not GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY not set — chart descriptions skipped")
        return

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    semaphore = asyncio.Semaphore(1)
    graph_chunks = [(i, c) for i, c in enumerate(chunks) if c.graph_page_nums]
    if not graph_chunks:
        doc.close()
        return

    total_pages = sum(len(c.graph_page_nums) for _, c in graph_chunks)
    log.info(f"Describing {total_pages} chart pages via Gemini Vision…")

    async def _do_page(chunk_idx: int, page_num: int, context: str) -> tuple[int, str]:
        async with semaphore:
            try:
                page = doc[page_num - 1]
                mat = fitz.Matrix(1.5, 1.5)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                img_bytes = pix.tobytes("png")
                desc = await _describe_with_gemini(img_bytes, context)
                if desc:
                    log.info(f"  Chart p.{page_num}: {desc[:80]}…")
                return chunk_idx, desc
            except Exception as exc:
                log.debug(f"Page {page_num} vision error: {exc}")
                return chunk_idx, ""
            finally:
                await asyncio.sleep(2)  # Base delay between vision API calls

    tasks = []
    for ci, chunk in graph_chunks:
        ctx = f"{chunk.chapter_title} — {chunk.section_title} (pp.{chunk.page_start}–{chunk.page_end})"
        for pn in chunk.graph_page_nums:
            tasks.append(_do_page(ci, pn, ctx))

    results = await asyncio.gather(*tasks)
    for ci, desc in results:
        if desc:
            chunks[ci].image_texts.append(desc)
            chunks[ci].has_images = True

    doc.close()
    described = sum(1 for _, desc in results if desc)
    log.info(f"Chart descriptions: {described}/{total_pages} pages described")


# ── PDF → Chunks ───────────────────────────────────────────────────────────────

def parse_pdf(pdf_bytes: bytes) -> list[Chunk]:
    """Parse a PDF into Chunk objects using PyMuPDF."""
    chunks: list[Chunk] = []
    ch_num, ch_title = 0, "Unknown"
    sec_title = "Introduction"
    topic = "General"

    buf_lines: list[str] = []
    buf_imgs: list[str] = []
    buf_tables: list[str] = []
    buf_math: list[str] = []
    buf_has_math = False
    buf_graph_pages: list[int] = []
    buf_page_start = 1
    ocr_active = _OCR_AVAILABLE

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = doc.pages(0, min(MAX_PAGES, doc.page_count))

    def _flush(page_end: int):
        nonlocal buf_lines, buf_imgs, buf_tables, buf_math, buf_has_math, buf_graph_pages, buf_page_start
        text = "\n".join(buf_lines).strip()
        if len(text) < MIN_CHUNK_CHARS or _is_skip_block(text):
            buf_lines, buf_imgs, buf_tables, buf_math, buf_graph_pages = [], [], [], [], []
            buf_has_math = False
            return
        math_text = " ".join(dict.fromkeys(buf_math))[:500]
        for sub in _split_chunks(text, MAX_CHUNK_CHARS):
            if len(sub) < MIN_CHUNK_CHARS:
                continue
            chunks.append(Chunk(
                chapter_num=ch_num, chapter_title=ch_title,
                section_title=sec_title, topic_tag=topic,
                text=sub.strip(), page_start=buf_page_start, page_end=page_end,
                has_formula=bool(_FORMULA_RE.search(sub)) or bool(buf_math),
                has_example=bool(_EXAMPLE_RE.search(sub)),
                teaching_density=_teaching_density(sub),
                key_terms=_key_terms(sub),
                image_texts=list(buf_imgs),
                table_texts=list(buf_tables),
                math_text=math_text,
                has_images=bool(buf_imgs),
                has_tables=bool(buf_tables),
                has_math_font=buf_has_math,
                graph_page_nums=list(buf_graph_pages),
            ))
        buf_lines, buf_imgs, buf_tables, buf_math, buf_graph_pages = [], [], [], [], []
        buf_has_math = False

    log.info(f"Parsing {min(MAX_PAGES, doc.page_count)} pages…")
    for page in pages:
        pn = page.number + 1
        if pn % 50 == 0:
            log.info(f"  Page {pn}…")
        try:
            data = _extract_page(page, doc, ocr_active)
        except Exception as exc:
            log.warning(f"Page {pn} failed: {exc}")
            page.clean_contents()
            continue

        if data["ocr_disabled"]:
            ocr_active = False

        raw = data["text"]
        if not raw.strip():
            page.clean_contents()
            continue

        ch_match = None if _is_toc_like(raw) else _find_chapter(raw)
        if ch_match:
            _flush(pn - 1)
            buf_page_start = pn
            ch_num, ch_title = ch_match
            topic = ch_title.strip()
            sec_title = "Introduction"

        for sm in list(_SECTION_RE.finditer(raw)):
            _flush(pn - 1)
            buf_page_start = pn
            sec_title = sm.group(2).strip()

        buf_lines.extend(raw.splitlines())
        buf_imgs.extend(data["image_texts"])
        buf_tables.extend(data["table_texts"])
        buf_math.extend(data["math_spans"])
        buf_has_math = buf_has_math or data["has_math_font"]
        if data["has_vector"]:
            buf_graph_pages.append(pn)

        if sum(len(l) for l in buf_lines) >= MAX_CHUNK_CHARS:
            _flush(pn)
            buf_page_start = pn + 1

        page.clean_contents()

    _flush(MAX_PAGES)
    doc.close()

    log.info(
        f"Parsed {len(chunks)} chunks — "
        f"{sum(1 for c in chunks if c.has_images)} with images, "
        f"{sum(1 for c in chunks if c.has_tables)} with tables, "
        f"{sum(1 for c in chunks if c.has_math_font)} with math fonts, "
        f"{sum(1 for c in chunks if c.graph_page_nums)} with vector graphics"
    )
    return chunks


# ── Embeddings via Gemini ──────────────────────────────────────────────────────

async def _embed(text: str) -> list[float]:
    """768-dim embeddings via Gemini text-embedding-004 (matches MongoDB vector index)."""
    if not GEMINI_API_KEY:
        return []
    payload = {
        "model": f"models/{GEMINI_EMBED_MODEL}",
        "content": {"parts": [{"text": text[:2048]}]},
        "taskType": "SEMANTIC_SIMILARITY",
        "outputDimensionality": 768,
    }
    for attempt in range(3):
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{GEMINI_BASE}/models/{GEMINI_EMBED_MODEL}:embedContent",
                params={"key": GEMINI_API_KEY},
                json=payload,
            )
        if resp.status_code in {429, 500, 503} and attempt < 2:
            await asyncio.sleep(2 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()["embedding"]["values"]
    return []


# ── MongoDB insertion ──────────────────────────────────────────────────────────

def _mongo_insert_chunks(chunks: list[Chunk], embeddings: list[list[float]]) -> int:
    client = MongoClient(MONGODB_URL, serverSelectionTimeoutMS=10000, directConnection=True)
    db = client[MONGODB_DB]
    col = db["pdf_chunks"]

    # Remove existing chunks for this book
    existing = col.count_documents({"book_id": BOOK_ID})
    if existing:
        log.info(f"Removing {existing} existing chunks for {BOOK_ID}…")
        col.delete_many({"book_id": BOOK_ID})

    docs = []
    for chunk, emb in zip(chunks, embeddings):
        docs.append({
            "book_id": BOOK_ID,
            "chapter_num": chunk.chapter_num,
            "chapter_title": chunk.chapter_title,
            "section_title": chunk.section_title,
            "topic_tag": chunk.topic_tag,
            "text": chunk.text,
            "image_texts": chunk.image_texts,
            "table_texts": chunk.table_texts,
            "math_text": chunk.math_text,
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            "has_images": chunk.has_images,
            "has_tables": chunk.has_tables,
            "has_math": chunk.has_math_font,
            "has_formula": chunk.has_formula,
            "has_example": chunk.has_example,
            "teaching_density": chunk.teaching_density,
            "key_terms": chunk.key_terms,
            "embedding": emb,
            "created_at": datetime.now(timezone.utc),
        })

    if docs:
        result = col.insert_many(docs)
        log.info(f"Inserted {len(result.inserted_ids)} chunks into MongoDB pdf_chunks")
    client.close()
    return len(docs)


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    # Determine PDF path
    if len(sys.argv) > 1:
        pdf_path = Path(sys.argv[1])
    else:
        pdf_path = _REPO_ROOT / "Book" / "IntroductoryBusinessStatistics-OP.pdf"

    if not pdf_path.exists():
        sys.exit(f"PDF not found: {pdf_path}")

    log.info(f"Book: {pdf_path.name} ({pdf_path.stat().st_size // 1024 // 1024} MB)")
    log.info(f"MongoDB: {MONGODB_URL}/{MONGODB_DB}")
    log.info(f"Embeddings: Gemini {GEMINI_EMBED_MODEL} (768-dim)")
    log.info(f"Gemini Vision: {'ENABLED' if GEMINI_API_KEY else 'DISABLED (no GEMINI_API_KEY)'}")

    pdf_bytes = pdf_path.read_bytes()

    # ── Step 1: Parse PDF ─────────────────────────────────────────────────────
    log.info("Step 1/4: Parsing PDF (text + tables + math + graph detection)…")
    chunks = parse_pdf(pdf_bytes)
    if not chunks:
        sys.exit("No usable chunks extracted from PDF.")

    # ── Step 2: Gemini Vision for chart pages ─────────────────────────────────
    log.info("Step 2/4: Describing charts/graphs via Gemini Vision…")
    await _add_graph_descriptions(chunks, pdf_bytes)

    # ── Step 3: Embeddings via Gemini ─────────────────────────────────────────
    if GEMINI_API_KEY:
        log.info(f"Step 3/4: Generating embeddings via Gemini {GEMINI_EMBED_MODEL}…")
    else:
        log.warning("Step 3/4: GEMINI_API_KEY not set — skipping embeddings")

    embeddings: list[list[float]] = []
    for i, chunk in enumerate(chunks):
        if i % 50 == 0:
            log.info(f"  Embedding chunk {i + 1}/{len(chunks)}…")
        try:
            if i:
                await asyncio.sleep(0.8)
            embed_text = "\n\n".join(part for part in [
                f"{chunk.chapter_title} {chunk.section_title}",
                chunk.text[:1500],
                ("Tables:\n" + "\n".join(chunk.table_texts)[:1200]) if chunk.table_texts else "",
                ("Images and charts:\n" + "\n".join(chunk.image_texts)[:1200]) if chunk.image_texts else "",
                (f"Formula snippets:\n{chunk.math_text[:800]}") if chunk.math_text else "",
            ] if part)
            emb = await _embed(embed_text)
            embeddings.append(emb)
        except Exception as exc:
            log.warning(f"Chunk {i} embed failed: {exc}")
            embeddings.append([])

    # ── Step 4: MongoDB ───────────────────────────────────────────────────────
    log.info("Step 4/4: Inserting into MongoDB…")
    inserted = _mongo_insert_chunks(chunks, embeddings)

    # ── Summary ───────────────────────────────────────────────────────────────
    chapters = sorted({c.chapter_num for c in chunks if c.chapter_num > 0})
    log.info("=" * 60)
    log.info(f"DONE — {inserted} chunks stored in MongoDB ({MONGODB_DB}.pdf_chunks)")
    log.info(f"Chapters found: {len(chapters)} ({min(chapters) if chapters else '?'}–{max(chapters) if chapters else '?'})")
    log.info(f"  With chart/image descriptions: {sum(1 for c in chunks if c.has_images)}")
    log.info(f"  With tables: {sum(1 for c in chunks if c.has_tables)}")
    log.info(f"  With math/formulae: {sum(1 for c in chunks if c.has_formula)}")
    log.info(f"  With embeddings: {sum(1 for e in embeddings if e)}")
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())

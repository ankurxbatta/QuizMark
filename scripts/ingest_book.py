#!/usr/bin/env python3
"""
ingest_book.py — Standalone script to ingest a PDF textbook directly into MongoDB.

Usage:
    python3 scripts/ingest_book.py [path/to/book.pdf]

Reads the PDF page-by-page, extracts text/tables/math/images, generates
embeddings via Gemini, describes charts and transcribes math formulas via the
vision fallback chain (OpenAI → Anthropic → Gemini), then stores everything
directly in MongoDB (pdf_chunks collection).

No local models required. Reads credentials from .env in the project root.
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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", _ENV.get("GEMINI_API_KEY", ""))
GEMINI_MODEL   = "gemini-2.5-flash"
GEMINI_BASE    = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_EMBED_MODEL = "gemini-embedding-001"

# OpenAI — primary paid provider for embeddings, vision, math
OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", _ENV.get("OPENAI_API_KEY", "")).strip()
OPENAI_BASE     = "https://api.openai.com/v1"
OPENAI_EMBED_MODEL = "text-embedding-3-small"
OPENAI_VISION_MODEL = "gpt-4o-mini"

# Anthropic — paid fallback for vision
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", _ENV.get("ANTHROPIC_API_KEY", "")).strip()
ANTHROPIC_VISION_MODEL = "claude-haiku-4-5-20251001"

MAX_PAGES         = 700
MIN_CHUNK_CHARS   = 300
MAX_CHUNK_CHARS   = 3000
BOOK_ID           = "IntroductoryBusinessStatistics-OP"

# Pages before this are treated as front matter — chapter detection disabled.
# Avoids TOC entries (e.g. "12  Introduction…") being mistaken for chapter headers.
FRONT_MATTER_PAGES = 20

# ── Third-party imports ────────────────────────────────────────────────────────
try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("PyMuPDF not installed. Run: pip3 install PyMuPDF")

# Text cleaner — inline copy so the script runs without importing the backend package
import unicodedata as _ud
def _clean_text(text: str) -> str:
    if not text: return text
    # Ligatures
    for bad, good in [("ﬀ","ff"),("ﬁ","fi"),("ﬂ","fl"),("ﬃ","ffi"),("ﬄ","ffl"),("ſ","s")]:
        text = text.replace(bad, good)
    # Mojibake (UTF-8 read as Latin-1) — use unicode escapes to avoid source encoding issues
    for bad, good in [
        ("â\x80\x99", "’"), ("â\x80\x98", "‘"),
        ("â\x80\x9c", "“"), ("â\x80\x9d", "”"),
        ("â\x80\x94", "—"), ("â\x80\x93", "–"),
        ("â\x80\xa6", "…"), ("â\x82\xac", "€"),
        ("\xc3\xa9", "\xe9"), ("\xc3\xa8", "\xe8"),
        ("\xef\xbf\xbd", ""),  # replacement char
    ]:
        if bad in text: text = text.replace(bad, good)
    # Normalize smart quotes/dashes to ASCII
    text = text.replace("‘", "'").replace("’", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("—", "--").replace("–", "-").replace("−", "-")
    # Zero-width and control chars
    text = re.sub(r"[​‌‍﻿­]", "", text)
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)
    text = _ud.normalize("NFC", text)
    # Soft-hyphen line-breaks: "distri-\nbution" -> "distribution"
    text = re.sub(r"-\s*\n\s*([a-z])", r"\1", text)
    # Inline line-breaks (single \n inside a sentence)
    text = re.sub(r"(?<![.!?:;])\n(?=[a-z])", " ", text)
    # Boilerplate
    text = re.sub(r"^[^\n]*(This OpenStax book is available|Access for free at openstax|CC BY 4\.0)[^\n]*",
                  "", text, flags=re.IGNORECASE|re.MULTILINE)
    text = re.sub(r"^(?:\d{1,4}\s+)?Chapter\s+\d{1,2}\s*\|[^\n]{0,80}$",
                  "", text, flags=re.IGNORECASE|re.MULTILINE)
    text = re.sub(r"^\s*\d{1,4}\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"[^\S\n]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

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
    math_page_nums: list[int] = field(default_factory=list)


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


def _find_chapter(text: str, current_chapter_num: int = 0) -> Optional[tuple[int, str]]:
    """Find a chapter heading in page text.

    Validates that the new chapter number is a reasonable advance from the
    current chapter — prevents TOC page entries (e.g. '12 Introduction …')
    from hijacking the chapter counter.
    """
    for pat in _CHAPTER_PATTERNS:
        for m in pat.finditer(text):
            try:
                num = int(m.group(1))
            except (IndexError, ValueError):
                continue
            if not (1 <= num <= 50):
                continue
            # Chapter must advance monotonically; allow same chapter (sub-match)
            # but reject jumps of more than 3 when we already have a chapter.
            if current_chapter_num > 0 and num < current_chapter_num:
                continue
            if current_chapter_num > 0 and num > current_chapter_num + 3:
                continue
            title = re.sub(r"\s*\.{2,}\s*\d+\s*$", "", m.group(2).strip()).strip()
            title = re.sub(r"\s+\d{1,4}\s*$", "", title).strip()
            if len(title) < 4 or _FRONT_MATTER_RE.match(re.sub(r"\s+", " ", title)):
                continue
            return num, title
    return None


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

    # Raster image detection — track presence even if OCR produces no text
    has_raster_images = False
    image_texts: list[str] = []
    ocr_disabled = False
    try:
        raster_refs = page.get_images(full=True)
        for img_ref in raster_refs:
            try:
                info = doc.extract_image(img_ref[0])
                img_bytes = info.get("image", b"")
                if not img_bytes:
                    continue
                if _PIL_AVAILABLE:
                    pil = PILImage.open(io.BytesIO(img_bytes))
                    if pil.width < 80 or pil.height < 80:
                        continue
                has_raster_images = True  # meaningful image found
                if ocr_active and _OCR_AVAILABLE and _PIL_AVAILABLE:
                    try:
                        pil = pil.convert("RGB")
                        t = pytesseract.image_to_string(pil, timeout=15).strip()
                        if t:
                            image_texts.append(t)
                    except pytesseract.TesseractNotFoundError:
                        ocr_disabled = True
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass

    # Math fonts — collect spans and record which page has math
    math_spans: list[str] = []
    has_math_font = False
    try:
        rawdict = page.get_text("rawdict")
        for block in rawdict.get("blocks", []):
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

    # Fallback: pull formula-looking lines from the text layer
    if has_math_font and not math_spans:
        fl = re.compile(
            r"[=÷×±√∑∫µσ²]|\b\d+\s*/\s*\d+\b|\b[A-Za-z]\s*=\s*[\d(]|"
            r"\b(?:P|E|Var|SD|SE)\s*\(|z\s*=|t\s*=|χ²|α\s*=|β\s*="
        )
        for ln in text.splitlines():
            s = ln.strip()
            if s and fl.search(s):
                math_spans.append(s)

    # Vector graphic detection — flag pages with significant filled drawing area.
    # No text-overlap filter: chart axis labels and value labels overlap with
    # drawing bboxes and the old 30% threshold incorrectly excluded real charts.
    # Gemini Vision will say NO_CHART for coloured text boxes sent by mistake.
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
                w, h = abs(r[2] - r[0]), abs(r[3] - r[1])
                if w < 5 or h < 5:
                    continue
                graphic_area += w * h
            has_vector = (graphic_area / page_area) > 0.03
    except Exception:
        pass

    return {
        "text": text,
        "table_texts": table_texts,
        "image_texts": image_texts,
        "has_raster_images": has_raster_images,
        "math_spans": math_spans,
        "has_math_font": has_math_font,
        "has_vector": has_vector,
        "ocr_disabled": ocr_disabled,
    }


# ── Provider health tracking (quota / rate-limit rotation) ────────────────────

import time as _time

class _ProviderState:
    def __init__(self, name: str):
        self.name = name
        self.ok = True
        self.cooldown_until: float = 0.0
        self.requests = 0
        self.errors = 0

    @property
    def available(self) -> bool:
        return _time.time() > self.cooldown_until

    def rate_limited(self, seconds: float = 60) -> None:
        self.cooldown_until = _time.time() + seconds
        self.errors += 1
        log.warning(f"[rotation] {self.name} rate-limited — cooldown {seconds:.0f}s")

    def quota_exhausted(self, msg: str = "") -> None:
        self.cooldown_until = _time.time() + 3600
        self.errors += 1
        log.warning(f"[rotation] {self.name} quota exhausted — cooldown 1hr. {msg[:120]}")

    def success(self) -> None:
        self.requests += 1

    def status(self) -> str:
        if not self.available:
            rem = int(self.cooldown_until - _time.time())
            return f"cooldown {rem}s"
        return "ok"


_gemini_embed_state   = _ProviderState("gemini_embed")
_openai_embed_state   = _ProviderState("openai_embed")
_gemini_vision_state  = _ProviderState("gemini_vision")
_openai_vision_state  = _ProviderState("openai_vision")
_anthropic_vis_state  = _ProviderState("anthropic_vision")

def _print_provider_status() -> None:
    states = [
        _gemini_embed_state, _openai_embed_state,
        _gemini_vision_state, _openai_vision_state, _anthropic_vis_state,
    ]
    log.info("Provider status: " + " | ".join(
        f"{s.name}:{s.status()} (req={s.requests},err={s.errors})" for s in states
    ))


def _is_quota_error(body: str) -> bool:
    body_lower = body.lower()
    return any(p in body_lower for p in (
        "quota", "exhausted", "resource_exhausted", "billing",
        "insufficient_quota", "rate_limit_exceeded", "daily limit",
    ))


# ── Vision (chart/image descriptions) with Gemini → OpenAI → Anthropic fallback

_VISION_PROMPT = (
    "You are an expert at reading statistical charts and graphs in a business statistics textbook. "
    "Describe exactly what this chart or graph shows: the type of visualisation, "
    "axis labels, key values/ranges, data trends, and what statistical concept it demonstrates. "
    "Be specific and quantitative. If no meaningful chart or image is visible, respond: NO_CHART"
)


async def _vision_gemini(image_bytes: bytes, context: str) -> str:
    if not GEMINI_API_KEY or not _gemini_vision_state.available:
        raise RuntimeError("gemini_vision unavailable")
    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "contents": [{"parts": [
            {"text": f"Context: {context}\n\n{_VISION_PROMPT}"},
            {"inline_data": {"mime_type": "image/png", "data": b64}},
        ]}],
        "generationConfig": {"maxOutputTokens": 500, "thinkingConfig": {"thinkingBudget": 0}},
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent",
            params={"key": GEMINI_API_KEY}, json=payload,
        )
    if resp.status_code == 429:
        body = resp.text
        if _is_quota_error(body):
            _gemini_vision_state.quota_exhausted(body)
        else:
            _gemini_vision_state.rate_limited(60)
        raise RuntimeError(f"Gemini vision 429: {body[:120]}")
    resp.raise_for_status()
    _gemini_vision_state.success()
    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    return "" if text == "NO_CHART" else text


async def _vision_openai(image_bytes: bytes, context: str) -> str:
    if not OPENAI_API_KEY or not _openai_vision_state.available:
        raise RuntimeError("openai_vision unavailable")
    b64 = base64.b64encode(image_bytes).decode()
    prompt = f"Context: {context}\n\n{_VISION_PROMPT}" if context else _VISION_PROMPT
    payload = {
        "model": OPENAI_VISION_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}],
        "max_tokens": 500,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{OPENAI_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json=payload,
        )
    if resp.status_code == 429:
        _openai_vision_state.rate_limited(30)
        raise RuntimeError(f"OpenAI vision 429")
    resp.raise_for_status()
    _openai_vision_state.success()
    text = resp.json()["choices"][0]["message"]["content"].strip()
    return "" if text == "NO_CHART" else text


async def _vision_anthropic(image_bytes: bytes, context: str) -> str:
    if not ANTHROPIC_API_KEY or not _anthropic_vis_state.available:
        raise RuntimeError("anthropic_vision unavailable")
    b64 = base64.b64encode(image_bytes).decode()
    prompt = f"Context: {context}\n\n{_VISION_PROMPT}" if context else _VISION_PROMPT
    payload = {
        "model": ANTHROPIC_VISION_MODEL,
        "max_tokens": 500,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
            {"type": "text", "text": prompt},
        ]}],
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
        )
    if resp.status_code == 429:
        _anthropic_vis_state.rate_limited(30)
        raise RuntimeError("Anthropic vision 429")
    resp.raise_for_status()
    _anthropic_vis_state.success()
    text = resp.json()["content"][0]["text"].strip()
    return "" if text == "NO_CHART" else text


async def _describe_with_gemini(image_bytes: bytes, context: str) -> str:
    """Vision with automatic Gemini → OpenAI → Anthropic fallback."""
    for fn in [_vision_gemini, _vision_openai, _vision_anthropic]:
        try:
            return await fn(image_bytes, context)
        except Exception as exc:
            log.debug(f"Vision provider failed: {exc}")
    return ""


async def _add_graph_descriptions(chunks: list[Chunk], pdf_bytes: bytes) -> None:
    """Render graph pages and describe via Gemini Vision."""
    if not GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY not set — chart descriptions skipped")
        return

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    # Paid API (OpenAI/Anthropic): 5 concurrent — ~5× faster than free-tier serial
    semaphore = asyncio.Semaphore(5)
    graph_chunks = [(i, c) for i, c in enumerate(chunks) if c.graph_page_nums]
    if not graph_chunks:
        doc.close()
        return

    total_pages = sum(len(c.graph_page_nums) for _, c in graph_chunks)
    log.info(f"Describing {total_pages} chart pages (concurrency=5)…")

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
                await asyncio.sleep(0.2)  # minimal courtesy delay for paid tier

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


# ── OpenAI Vision (math formula extraction) — paid, high concurrency ──────────

_MATH_PROMPT = (
    "You are an expert at reading mathematical formulas from statistics textbooks. "
    "Extract ALL mathematical formulas, equations, and statistical expressions visible "
    "on this page. Write each formula in proper LaTeX notation. Focus on:\n"
    "- Statistical formulas: mean, variance, standard deviation, z-score, t-statistic\n"
    "- Probability formulas and distribution functions (normal, binomial, Poisson)\n"
    "- Regression and correlation equations\n"
    "- Hypothesis testing formulas (chi-square, ANOVA, p-value calculations)\n"
    "- Any other mathematical equations present\n\n"
    "Format: one LaTeX formula per line, e.g. $\\bar{x} = \\frac{\\sum x_i}{n}$\n"
    "Do NOT include explanatory text — only the LaTeX formulas.\n"
    "If no mathematical formulas are present on this page, respond exactly: NO_MATH"
)

_openai_math_state = _ProviderState("openai_math")


async def _extract_math_with_openai(image_bytes: bytes, context: str) -> str:
    """Extract LaTeX math formulas using OpenAI GPT-4o-mini vision (paid, 500 RPM)."""
    if not OPENAI_API_KEY or not _openai_math_state.available:
        raise RuntimeError("openai_math unavailable")
    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "model": OPENAI_VISION_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": f"Page context: {context}\n\n{_MATH_PROMPT}"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}],
        "max_tokens": 1000,
        "temperature": 0.1,
    }
    for attempt in range(4):
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{OPENAI_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json=payload,
            )
        if resp.status_code == 429:
            _openai_math_state.rate_limited(15)
            await asyncio.sleep(15)
            continue
        if resp.status_code in {500, 502, 503} and attempt < 3:
            await asyncio.sleep(3 * (attempt + 1))
            continue
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        _openai_math_state.success()
        return "" if text == "NO_MATH" else text
    return ""


async def _extract_math_with_anthropic(image_bytes: bytes, context: str) -> str:
    """Anthropic Claude Haiku fallback for math extraction."""
    if not ANTHROPIC_API_KEY or not _anthropic_vis_state.available:
        raise RuntimeError("anthropic_math unavailable")
    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "model": ANTHROPIC_VISION_MODEL,
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
            {"type": "text", "text": f"Page context: {context}\n\n{_MATH_PROMPT}"},
        ]}],
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json=payload,
        )
    if resp.status_code == 429:
        _anthropic_vis_state.rate_limited(15)
        raise RuntimeError("Anthropic math 429")
    resp.raise_for_status()
    _anthropic_vis_state.success()
    text = resp.json()["content"][0]["text"].strip()
    return "" if text == "NO_MATH" else text


async def _extract_math(image_bytes: bytes, context: str) -> str:
    """Math extraction with OpenAI → Anthropic fallback."""
    for fn in [_extract_math_with_openai, _extract_math_with_anthropic]:
        try:
            return await fn(image_bytes, context)
        except Exception as exc:
            log.debug(f"Math provider failed: {exc}")
    return ""


async def _add_math_descriptions(chunks: list[Chunk], pdf_bytes: bytes) -> None:
    """
    Extract LaTeX formulas from math-font pages via OpenAI GPT-4o-mini (primary)
    and Anthropic Claude Haiku (fallback). Concurrency=5 — paid APIs support 500 RPM.
    """
    if not OPENAI_API_KEY and not ANTHROPIC_API_KEY:
        log.warning("No paid math provider available — skipping math extraction")
        return

    math_chunks = [(i, c) for i, c in enumerate(chunks) if c.math_page_nums]
    if not math_chunks:
        log.info("No math-font pages found — skipping math extraction")
        return

    total_pages = sum(len(c.math_page_nums) for _, c in math_chunks)
    log.info(f"Extracting math from {total_pages} pages via OpenAI→Anthropic (concurrency=5)…")

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    # Paid APIs: 5 concurrent — ~5× faster, well within 500 RPM limit
    semaphore = asyncio.Semaphore(5)

    async def _do_page(chunk_idx: int, page_num: int, context: str) -> tuple[int, str]:
        async with semaphore:
            try:
                page = doc[page_num - 1]
                mat = fitz.Matrix(2.0, 2.0)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                img_bytes = pix.tobytes("png")
                result = await _extract_math(img_bytes, context)
                if result:
                    log.info(f"  Math p.{page_num}: {result[:100]}…")
                return chunk_idx, result
            except Exception as exc:
                log.debug(f"Page {page_num} math failed: {exc}")
                return chunk_idx, ""
            finally:
                await asyncio.sleep(0.1)  # minimal delay for paid tier

    tasks = []
    for ci, chunk in math_chunks:
        ctx = f"{chunk.chapter_title} — {chunk.section_title} (pp.{chunk.page_start}–{chunk.page_end})"
        for pn in chunk.math_page_nums:
            tasks.append(_do_page(ci, pn, ctx))

    results = await asyncio.gather(*tasks)

    # Collect LaTeX per chunk (may span multiple pages), then replace
    # the text-layer math_text entirely — vision extraction is the authoritative source.
    chunk_latex: dict[int, list[str]] = {}
    for ci, math_text in results:
        if math_text:
            chunk_latex.setdefault(ci, []).append(math_text)

    for ci, latex_pages in chunk_latex.items():
        combined = "\n".join(latex_pages)
        deduped = list(dict.fromkeys(l for l in combined.splitlines() if l.strip()))
        chunks[ci].math_text = "\n".join(deduped)

    doc.close()
    described = sum(1 for _, m in results if m)
    log.info(f"Math extraction: {described}/{len(tasks)} pages yielded formulas")


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
    buf_math_pages: list[int] = []
    buf_has_raster = False
    buf_page_start = 1
    ocr_active = _OCR_AVAILABLE

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = doc.pages(0, min(MAX_PAGES, doc.page_count))

    def _flush(page_end: int):
        nonlocal buf_lines, buf_imgs, buf_tables, buf_math, buf_has_math
        nonlocal buf_graph_pages, buf_math_pages, buf_has_raster, buf_page_start
        text = "\n".join(buf_lines).strip()
        if len(text) < MIN_CHUNK_CHARS:
            buf_lines, buf_imgs, buf_tables, buf_math = [], [], [], []
            buf_graph_pages, buf_math_pages = [], []
            buf_has_math = False
            buf_has_raster = False
            return
        math_text = " ".join(dict.fromkeys(buf_math))[:500]
        # A chunk has images if: Gemini described a chart, OCR found text in raster
        # images, OR we detected raster/vector images (even without a description yet).
        chunk_has_images = bool(buf_imgs) or bool(buf_graph_pages) or buf_has_raster
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
                has_images=chunk_has_images,
                has_tables=bool(buf_tables),
                has_math_font=buf_has_math,
                graph_page_nums=list(buf_graph_pages),
                math_page_nums=list(buf_math_pages),
            ))
        buf_lines, buf_imgs, buf_tables, buf_math = [], [], [], []
        buf_graph_pages, buf_math_pages = [], []
        buf_has_math = False
        buf_has_raster = False

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

        # Chapter detection: disabled in front matter, and validates monotone advance.
        ch_match = None
        if pn > FRONT_MATTER_PAGES and not _is_toc_like(raw):
            ch_match = _find_chapter(raw, current_chapter_num=ch_num)
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
        if data["has_math_font"]:
            buf_math_pages.append(pn)
        if data["has_raster_images"]:
            buf_has_raster = True
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
        f"{sum(1 for c in chunks if c.graph_page_nums)} with vector graphics, "
        f"{sum(1 for c in chunks if c.math_page_nums)} with math pages"
    )
    return chunks


# ── Embeddings with Gemini → OpenAI fallback (both 768-dim) ───────────────────

async def _embed_gemini(text: str) -> list[float]:
    if not GEMINI_API_KEY or not _gemini_embed_state.available:
        raise RuntimeError("gemini_embed unavailable")
    payload = {
        "model": f"models/{GEMINI_EMBED_MODEL}",
        "content": {"parts": [{"text": text[:2048]}]},
        "taskType": "SEMANTIC_SIMILARITY",
        "outputDimensionality": 768,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{GEMINI_BASE}/models/{GEMINI_EMBED_MODEL}:embedContent",
            params={"key": GEMINI_API_KEY},
            json=payload,
        )
    if resp.status_code == 429:
        body = resp.text
        if _is_quota_error(body):
            _gemini_embed_state.quota_exhausted(body)
        else:
            _gemini_embed_state.rate_limited(60)
        raise RuntimeError(f"Gemini embed 429: {body[:120]}")
    resp.raise_for_status()
    _gemini_embed_state.success()
    return resp.json()["embedding"]["values"]


async def _embed_openai(text: str) -> list[float]:
    if not OPENAI_API_KEY or not _openai_embed_state.available:
        raise RuntimeError("openai_embed unavailable")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{OPENAI_BASE}/embeddings",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": OPENAI_EMBED_MODEL, "input": text[:8191], "dimensions": 768},
        )
    if resp.status_code == 429:
        _openai_embed_state.rate_limited(30)
        raise RuntimeError("OpenAI embed 429")
    resp.raise_for_status()
    _openai_embed_state.success()
    return resp.json()["data"][0]["embedding"]


async def _embed(text: str) -> list[float]:
    """768-dim embedding with automatic Gemini → OpenAI fallback.
    Both providers output exactly 768 dimensions so MongoDB vector index works with either."""
    for fn in [_embed_gemini, _embed_openai]:
        try:
            return await fn(text)
        except Exception as exc:
            log.debug(f"Embed provider failed: {exc}")
    log.error("All embedding providers failed — chunk will have no vector")
    return []


# ── MongoDB insertion ──────────────────────────────────────────────────────────

def _mongo_insert_chunks(chunks: list[Chunk], embeddings: list[list[float]]) -> int:
    client = MongoClient(MONGODB_URL, serverSelectionTimeoutMS=10000, directConnection=True)
    db = client[MONGODB_DB]
    col = db["pdf_chunks"]

    existing = col.count_documents({"book_id": BOOK_ID})
    if existing:
        log.info(f"Removing {existing} existing chunks for {BOOK_ID}…")
        col.delete_many({"book_id": BOOK_ID})

    docs = []
    for chunk, emb in zip(chunks, embeddings):
        # Apply noise cleaning before storing
        clean_t      = _clean_text(chunk.text)
        clean_math   = _clean_text(chunk.math_text)
        clean_imgs   = [_clean_text(t) for t in chunk.image_texts]
        clean_tables = [_clean_text(t) for t in chunk.table_texts]
        docs.append({
            "book_id": BOOK_ID,
            "chapter_num": chunk.chapter_num,
            "chapter_title": chunk.chapter_title,
            "section_title": chunk.section_title,
            "topic_tag": chunk.topic_tag,
            "text": clean_t,
            "image_texts": clean_imgs,
            "table_texts": clean_tables,
            "math_text": clean_math,
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            "has_images": chunk.has_images,
            "has_tables": chunk.has_tables,
            "has_math": chunk.has_math_font,
            "has_formula": chunk.has_formula,
            "has_example": chunk.has_example,
            "teaching_density": chunk.teaching_density,
            "key_terms": chunk.key_terms,
            "graph_page_nums": chunk.graph_page_nums,
            "math_page_nums": chunk.math_page_nums,
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
    if len(sys.argv) > 1:
        pdf_path = Path(sys.argv[1])
    else:
        pdf_path = _REPO_ROOT / "Book" / "IntroductoryBusinessStatistics-OP.pdf"

    if not pdf_path.exists():
        sys.exit(f"PDF not found: {pdf_path}")

    log.info(f"Book: {pdf_path.name} ({pdf_path.stat().st_size // 1024 // 1024} MB)")
    log.info(f"MongoDB: {MONGODB_URL}/{MONGODB_DB}")
    log.info("── API Provider Configuration ──────────────────────────────")
    log.info(f"  Gemini key:    {'SET' if GEMINI_API_KEY else 'NOT SET'}")
    log.info(f"  OpenAI key:    {'SET' if OPENAI_API_KEY else 'NOT SET'} (paid fallback)")
    log.info(f"  Anthropic key: {'SET' if ANTHROPIC_API_KEY else 'NOT SET'} (paid fallback)")
    log.info(f"  Embed chain:   Gemini {GEMINI_EMBED_MODEL} (768-dim) → OpenAI {OPENAI_EMBED_MODEL} (768-dim, batch)")
    log.info(f"  Vision chain:  Gemini {GEMINI_MODEL} → OpenAI {OPENAI_VISION_MODEL} → Anthropic {ANTHROPIC_VISION_MODEL}")
    log.info(f"  Math chain:    OpenAI {OPENAI_VISION_MODEL} → Anthropic {ANTHROPIC_VISION_MODEL} (concurrency=5)")
    log.info("─────────────────────────────────────────────────────────────")

    pdf_bytes = pdf_path.read_bytes()

    # ── Step 1: Parse PDF ─────────────────────────────────────────────────────
    log.info("Step 1/5: Parsing PDF (text + tables + math detection + image detection)…")
    chunks = parse_pdf(pdf_bytes)
    if not chunks:
        sys.exit("No usable chunks extracted from PDF.")

    # ── Step 2: Gemini Vision for chart/graph pages ───────────────────────────
    log.info("Step 2/5: Describing charts/graphs via Gemini Vision…")
    await _add_graph_descriptions(chunks, pdf_bytes)

    # ── Step 3: DeepSeek math formula extraction ──────────────────────────────
    log.info("Step 3/5: Extracting math formulas via DeepSeek API…")
    await _add_math_descriptions(chunks, pdf_bytes)

    # ── Step 4: Batch embeddings (Gemini → OpenAI fallback, one round-trip) ──────
    log.info(f"Step 4/5: Batch embedding {len(chunks)} chunks…")

    def _embed_text(chunk) -> str:
        return "\n\n".join(p for p in [
            f"{chunk.chapter_title} {chunk.section_title}",
            chunk.text[:1500],
            ("Tables:\n" + "\n".join(chunk.table_texts)[:1200]) if chunk.table_texts else "",
            ("Images:\n" + "\n".join(chunk.image_texts)[:1200]) if chunk.image_texts else "",
            (f"Math:\n{chunk.math_text[:800]}") if chunk.math_text else "",
        ] if p)

    embed_texts = [_embed_text(c) for c in chunks]

    # Try Gemini batch first (free), fall back to OpenAI batch (paid)
    embeddings: list[list[float]] = []
    try:
        if GEMINI_API_KEY and _gemini_embed_state.available:
            log.info("  Using Gemini batch embed…")
            # Gemini batch: up to 100 per call — process in chunks of 100
            BATCH = 100
            for start in range(0, len(embed_texts), BATCH):
                batch = embed_texts[start:start + BATCH]
                model_path = f"models/{GEMINI_EMBED_MODEL}"
                payload = {"requests": [
                    {"model": model_path, "content": {"parts": [{"text": t[:2048]}]},
                     "taskType": "SEMANTIC_SIMILARITY", "outputDimensionality": 768}
                    for t in batch
                ]}
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        f"{GEMINI_BASE}/{model_path}:batchEmbedContents",
                        params={"key": GEMINI_API_KEY}, json=payload,
                    )
                if resp.status_code == 429 or resp.status_code >= 400:
                    body = resp.text
                    _gemini_embed_state.quota_exhausted(body)
                    raise RuntimeError(f"Gemini batch embed failed ({resp.status_code})")
                data = resp.json()
                embeddings.extend(item.get("values", []) for item in data.get("embeddings", []))
                log.info(f"  Gemini embedded {min(start + BATCH, len(embed_texts))}/{len(embed_texts)}")
                await asyncio.sleep(0.3)
        else:
            raise RuntimeError("Gemini unavailable")
    except Exception as exc:
        log.warning(f"Gemini batch embed failed ({exc}) — using OpenAI batch…")
        embeddings = []
        if not OPENAI_API_KEY:
            log.error("No embedding provider available — chunks will have no vectors!")
        else:
            # OpenAI supports up to 2048 inputs per call
            BATCH = 500
            for start in range(0, len(embed_texts), BATCH):
                batch = embed_texts[start:start + BATCH]
                async with httpx.AsyncClient(timeout=90) as client:
                    resp = await client.post(
                        f"{OPENAI_BASE}/embeddings",
                        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                        json={"model": OPENAI_EMBED_MODEL, "input": [t[:8191] for t in batch], "dimensions": 768},
                    )
                resp.raise_for_status()
                items = sorted(resp.json()["data"], key=lambda x: x["index"])
                embeddings.extend(item["embedding"] for item in items)
                log.info(f"  OpenAI embedded {min(start + BATCH, len(embed_texts))}/{len(embed_texts)}")

    # Pad any missing embeddings
    while len(embeddings) < len(chunks):
        embeddings.append([])

    _print_provider_status()

    # ── Step 5: MongoDB ───────────────────────────────────────────────────────
    log.info("Step 5/5: Inserting into MongoDB…")
    inserted = _mongo_insert_chunks(chunks, embeddings)

    # ── Summary ───────────────────────────────────────────────────────────────
    chapters = sorted({c.chapter_num for c in chunks if c.chapter_num > 0})
    log.info("=" * 60)
    log.info(f"DONE — {inserted} chunks stored in MongoDB ({MONGODB_DB}.pdf_chunks)")
    log.info(f"Chapters found: {len(chapters)} ({min(chapters) if chapters else '?'}–{max(chapters) if chapters else '?'})")
    log.info(f"  With chart/image descriptions: {sum(1 for c in chunks if c.has_images)}")
    log.info(f"  With tables: {sum(1 for c in chunks if c.has_tables)}")
    log.info(f"  With math formulas (DeepSeek): {sum(1 for c in chunks if c.math_text)}")
    log.info(f"  With embeddings: {sum(1 for e in embeddings if e)}")
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())

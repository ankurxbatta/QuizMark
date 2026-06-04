"""
text_cleaner.py — PDF noise removal for extracted textbook chunks.

Handles the most common artifacts from PyMuPDF extraction:
  • Unicode ligatures (ﬁ ﬂ ﬀ ﬃ ﬄ → fi fl ff ffi ffl)
  • Soft hyphens and broken hyphenation across line breaks
  • Zero-width / invisible Unicode characters
  • Mojibake encoding artifacts (â€™ → ', â€" → —, etc.)
  • Repeated OpenStax / copyright boilerplate lines
  • Page number / chapter header noise (e.g. "28 Chapter 1 | Sampling and Data")
  • Excessive whitespace / blank lines
  • Control characters (except tab / newline)
  • Smart-quote normalisation
  • Subscript/superscript digit noise from formula extraction
"""
from __future__ import annotations

import re
import unicodedata

# ── Unicode ligature map ───────────────────────────────────────────────────────
_LIGATURES: dict[str, str] = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬅ": "st",
    "ﬆ": "st",
    # Long-s ligatures
    "ſ": "s",
    # Latin small letter dotless i
    "ı": "i",
}
_LIG_TABLE = str.maketrans(_LIGATURES)

# ── Mojibake patterns — UTF-8 bytes misread as Latin-1 ───────────────────────
_MOJIBAKE: list[tuple[str, str]] = [
    ("â€™", "'"),   # RIGHT SINGLE QUOTATION MARK
    ("â€˜", "'"),   # LEFT SINGLE QUOTATION MARK
    ("â€œ", '"'),   # LEFT DOUBLE QUOTATION MARK
    ("â€\x9d", '"'), # RIGHT DOUBLE QUOTATION MARK
    ("â€"", "—"),   # EM DASH
    ("â€"", "–"),   # EN DASH
    ("â€¦", "…"),   # HORIZONTAL ELLIPSIS
    ("Ã©", "é"),
    ("Ã¨", "è"),
    ("Ã ", "à"),
    ("Ã¢", "â"),
    ("Ãª", "ê"),
    ("Ã®", "î"),
    ("Ã´", "ô"),
    ("Ã»", "û"),
    ("Ã§", "ç"),
    ("Ã¼", "ü"),
    ("Ã¶", "ö"),
    ("Ã¤", "ä"),
    ("�", ""),  # replacement character
]

# ── Smart-quote normalisation ─────────────────────────────────────────────────
_QUOTES: dict[str, str] = {
    "‘": "'",  # LEFT SINGLE
    "’": "'",  # RIGHT SINGLE
    "“": '"',  # LEFT DOUBLE
    "”": '"',  # RIGHT DOUBLE
    "′": "'",  # PRIME
    "″": '"',  # DOUBLE PRIME
    "´": "'",  # ACUTE ACCENT
    "`": "'",  # GRAVE ACCENT
}
_QUOTE_TABLE = str.maketrans(_QUOTES)

# ── Dash normalisation ────────────────────────────────────────────────────────
_DASHES: dict[str, str] = {
    "–": "-",  # EN DASH
    "—": "--", # EM DASH
    "―": "--", # HORIZONTAL BAR
    "−": "-",  # MINUS SIGN
}
_DASH_TABLE = str.maketrans(_DASHES)

# ── Zero-width / invisible characters ────────────────────────────────────────
_ZERO_WIDTH_RE = re.compile(
    r"[​‌‍‎‏﻿­⁠᠎͏]"
)

# ── Control characters (keep \t \n \r) ────────────────────────────────────────
_CONTROL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")

# ── Soft-hyphen line-break joining: "distri-\nbu tion" → "distribution" ──────
_SOFT_HYPHEN_RE = re.compile(r"-\s*\n\s*([a-z])")

# ── Hard line-break inside a sentence (not a paragraph boundary) ─────────────
_INLINE_BREAK_RE = re.compile(r"(?<![.!?:;])\n(?=[a-z])")

# ── Excessive blank lines ─────────────────────────────────────────────────────
_MULTI_BLANK_RE = re.compile(r"\n{3,}")

# ── Repeated whitespace (not newlines) ───────────────────────────────────────
_MULTI_SPACE_RE = re.compile(r"[^\S\n]{2,}")

# ── OpenStax / copyright boilerplate lines ────────────────────────────────────
_BOILERPLATE_RE = re.compile(
    r"^[^\n]*(?:"
    r"This OpenStax book is available for free|"
    r"Access for free at openstax\.org|"
    r"Access for free at cnx\.org|"
    r"OpenStax is part of Rice University|"
    r"This work is licensed under a Creative Commons|"
    r"Attribution 4\.0 International|"
    r"CC BY 4\.0|"
    r"©\s*\d{4}\s*Rice University"
    r")[^\n]*",
    re.IGNORECASE | re.MULTILINE,
)

# ── Page-number / chapter-header artifacts ────────────────────────────────────
# Patterns like: "28 Chapter 1 | Sampling and Data" or "Chapter 1 | Introduction  28"
_PAGE_HEADER_RE = re.compile(
    r"^(?:\d{1,4}\s+)?Chapter\s+\d{1,2}\s*\|[^\n]{0,80}$|"
    r"^[^\n]{0,80}\|\s*Chapter\s+\d{1,2}\s*(?:\d{1,4})?$|"
    r"^\s*\d{1,4}\s*$",           # bare page numbers on their own line
    re.IGNORECASE | re.MULTILINE,
)

# ── Table-pipe noise: lines that are just "| |  |  |" ────────────────────────
_TABLE_NOISE_RE = re.compile(r"^\s*[|\s]{3,}\s*$", re.MULTILINE)

# ── Repeated identical short lines (OCR / header repeats) ────────────────────
def _dedup_repeated_lines(text: str, max_repeats: int = 2) -> str:
    lines = text.split("\n")
    seen: dict[str, int] = {}
    out: list[str] = []
    for line in lines:
        key = line.strip().lower()
        if not key:
            out.append(line)
            continue
        count = seen.get(key, 0)
        if count < max_repeats:
            out.append(line)
            seen[key] = count + 1
    return "\n".join(out)


def clean_text(text: str) -> str:
    """
    Full cleaning pipeline for a single text field.
    Returns cleaned text. Never raises — returns original on unexpected error.
    """
    if not text:
        return text
    try:
        return _clean(text)
    except Exception:
        return text


def _clean(text: str) -> str:
    # 1. Mojibake first (before any Unicode normalisation loses context)
    for bad, good in _MOJIBAKE:
        if bad in text:
            text = text.replace(bad, good)

    # 2. Ligatures
    text = text.translate(_LIG_TABLE)

    # 3. Smart quotes + dashes
    text = text.translate(_QUOTE_TABLE)
    text = text.translate(_DASH_TABLE)

    # 4. Zero-width / invisible chars
    text = _ZERO_WIDTH_RE.sub("", text)

    # 5. Control characters
    text = _CONTROL_RE.sub("", text)

    # 6. Unicode normalise to NFC (canonical composed form)
    text = unicodedata.normalize("NFC", text)

    # 7. Soft-hyphen word-joins (must come before inline-break removal)
    text = _SOFT_HYPHEN_RE.sub(r"\1", text)

    # 8. Join inline line-breaks (single \n inside a sentence)
    text = _INLINE_BREAK_RE.sub(" ", text)

    # 9. Remove boilerplate lines
    text = _BOILERPLATE_RE.sub("", text)

    # 10. Remove page-header/footer noise
    text = _PAGE_HEADER_RE.sub("", text)

    # 11. Remove empty table-pipe lines
    text = _TABLE_NOISE_RE.sub("", text)

    # 12. Deduplicate repeated lines (OCR / header repeats)
    text = _dedup_repeated_lines(text)

    # 13. Collapse whitespace
    text = _MULTI_SPACE_RE.sub(" ", text)
    text = _MULTI_BLANK_RE.sub("\n\n", text)

    # 14. Strip leading/trailing whitespace per line
    lines = [l.rstrip() for l in text.split("\n")]
    text = "\n".join(lines)

    return text.strip()


def clean_chunk_doc(doc: dict) -> dict:
    """
    Clean all text fields of a MongoDB pdf_chunks document in-place.
    Returns the mutated document (same object).
    """
    for field in ("text", "math_text"):
        if doc.get(field):
            doc[field] = clean_text(doc[field])

    for list_field in ("image_texts", "table_texts", "key_terms"):
        if doc.get(list_field):
            doc[list_field] = [
                clean_text(item) if isinstance(item, str) else item
                for item in doc[list_field]
            ]

    return doc


def estimate_noise_ratio(text: str) -> float:
    """
    Returns a 0–1 score of how noisy the text is.
    Useful for deciding whether to re-clean or flag a chunk.
    """
    if not text:
        return 0.0
    total = len(text)
    noise = 0
    noise += sum(1 for c in text if unicodedata.category(c) in ("Cc", "Cf", "Cs"))
    noise += len(_ZERO_WIDTH_RE.findall(text)) * 3
    noise += len(_BOILERPLATE_RE.findall(text)) * 50
    noise += sum(len(bad) for bad, _ in _MOJIBAKE if bad in text)
    return min(1.0, noise / max(total, 1))

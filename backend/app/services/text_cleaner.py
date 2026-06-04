# -*- coding: utf-8 -*-
"""
text_cleaner.py -- PDF noise removal for extracted textbook chunks.

All special characters represented as \\uXXXX escapes so the file is
safe on any Python version and any system locale.
"""
from __future__ import annotations

import re
import unicodedata

# ── Ligatures -> ASCII equivalents ────────────────────────────────────────────
_LIGATURES: dict[str, str] = {
    "ﬀ": "ff",   # LATIN SMALL LIGATURE FF
    "ﬁ": "fi",   # LATIN SMALL LIGATURE FI
    "ﬂ": "fl",   # LATIN SMALL LIGATURE FL
    "ﬃ": "ffi",  # LATIN SMALL LIGATURE FFI
    "ﬄ": "ffl",  # LATIN SMALL LIGATURE FFL
    "ﬅ": "st",   # LATIN SMALL LIGATURE LONG S T
    "ﬆ": "st",   # LATIN SMALL LIGATURE ST
    "ſ": "s",    # LATIN SMALL LETTER LONG S
    "ı": "i",    # LATIN SMALL LETTER DOTLESS I
}
_LIG_TABLE = str.maketrans(_LIGATURES)

# ── Mojibake: UTF-8 bytes decoded as Latin-1 ─────────────────────────────────
# Each pair: (mangled_string, correct_replacement)
# Source bytes are represented as \xNN escapes -- no literal non-ASCII.
_MOJIBAKE: list[tuple[str, str]] = [
    ("\xc3\xa2\xc2\x80\xc2\x99", "’"),   # RIGHT SINGLE QUOTATION MARK '
    ("\xc3\xa2\xc2\x80\xc2\x98", "‘"),   # LEFT SINGLE QUOTATION MARK  '
    ("\xc3\xa2\xc2\x80\xc2\x9c", "“"),   # LEFT DOUBLE QUOTATION MARK  "
    ("\xc3\xa2\xc2\x80\xc2\x9d", "”"),   # RIGHT DOUBLE QUOTATION MARK "
    ("\xc3\xa2\xc2\x80\xc2\x94", "--"),        # EM DASH
    ("\xc3\xa2\xc2\x80\xc2\x93", "-"),         # EN DASH
    ("\xc3\xa2\xc2\x80\xc2\xa6", "..."),       # HORIZONTAL ELLIPSIS
    ("\xc3\xa2\xc2\x82\xc2\xac", "€"),   # EURO SIGN
    # Simple Latin accented letters
    ("\xc3\xa9", "\xe9"),  # e acute
    ("\xc3\xa8", "\xe8"),  # e grave
    ("\xc3\xa0", "\xe0"),  # a grave
    ("\xc3\xa2", "\xe2"),  # a circumflex
    ("\xc3\xaa", "\xea"),  # e circumflex
    ("\xc3\xae", "\xee"),  # i circumflex
    ("\xc3\xb4", "\xf4"),  # o circumflex
    ("\xc3\xbb", "\xfb"),  # u circumflex
    ("\xc3\xa7", "\xe7"),  # c cedilla
    ("\xc3\xbc", "\xfc"),  # u umlaut
    ("\xc3\xb6", "\xf6"),  # o umlaut
    ("\xc3\xa4", "\xe4"),  # a umlaut
    ("\xef\xbf\xbd", ""),  # UTF-8 replacement character
]

# ── Smart quotes -> straight quotes ───────────────────────────────────────────
_QUOTES: dict[str, str] = {
    "‘": "'",   # LEFT SINGLE QUOTATION MARK
    "’": "'",   # RIGHT SINGLE QUOTATION MARK
    "“": '"',   # LEFT DOUBLE QUOTATION MARK
    "”": '"',   # RIGHT DOUBLE QUOTATION MARK
    "′": "'",   # PRIME
    "″": '"',   # DOUBLE PRIME
    "´": "'",   # ACUTE ACCENT
}
_QUOTE_TABLE = str.maketrans(_QUOTES)

# ── Dashes -> ASCII hyphens ────────────────────────────────────────────────────
_DASHES: dict[str, str] = {
    "–": "-",   # EN DASH
    "—": "--",  # EM DASH
    "―": "--",  # HORIZONTAL BAR
    "−": "-",   # MINUS SIGN
}
_DASH_TABLE = str.maketrans(_DASHES)

# ── Zero-width / invisible characters ────────────────────────────────────────
_ZERO_WIDTH_RE = re.compile(
    "[​‌‍‎‏﻿­⁠᠎͏]"
)

# ── Control characters (keep \t \n \r) ────────────────────────────────────────
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# ── Soft-hyphen line-break: "distri-\nbu tion" -> "distribution" ─────────────
_SOFT_HYPHEN_RE = re.compile(r"-\s*\n\s*([a-z])")

# ── Inline line-break inside a sentence ──────────────────────────────────────
_INLINE_BREAK_RE = re.compile(r"(?<![.!?:;])\n(?=[a-z])")

# ── Excessive blank lines ─────────────────────────────────────────────────────
_MULTI_BLANK_RE = re.compile(r"\n{3,}")

# ── Multiple spaces (not newlines) ────────────────────────────────────────────
_MULTI_SPACE_RE = re.compile(r"[^\S\n]{2,}")

# ── OpenStax boilerplate ──────────────────────────────────────────────────────
_BOILERPLATE_RE = re.compile(
    r"^[^\n]*(?:"
    r"This OpenStax book is available for free|"
    r"Access for free at openstax\.org|"
    r"Access for free at cnx\.org|"
    r"OpenStax is part of Rice University|"
    r"This work is licensed under a Creative Commons|"
    r"Attribution 4\.0 International|"
    r"CC BY 4\.0|"
    r"\xc2\xa9\s*\d{4}\s*Rice University"
    r")[^\n]*",
    re.IGNORECASE | re.MULTILINE,
)

# ── Page-number / chapter-header noise ───────────────────────────────────────
_PAGE_HEADER_RE = re.compile(
    r"^(?:\d{1,4}\s+)?Chapter\s+\d{1,2}\s*\|[^\n]{0,80}$|"
    r"^[^\n]{0,80}\|\s*Chapter\s+\d{1,2}\s*(?:\d{1,4})?$|"
    r"^\s*\d{1,4}\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# ── Table-pipe noise ─────────────────────────────────────────────────────────
_TABLE_NOISE_RE = re.compile(r"^\s*[|\s]{3,}\s*$", re.MULTILINE)


def _dedup_repeated_lines(text: str, max_repeats: int = 2) -> str:
    seen: dict[str, int] = {}
    out: list[str] = []
    for line in text.split("\n"):
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
    """Full cleaning pipeline. Never raises -- returns original on error."""
    if not text:
        return text
    try:
        return _clean(text)
    except Exception:
        return text


def _clean(text: str) -> str:
    # 1. Mojibake
    for bad, good in _MOJIBAKE:
        if bad in text:
            text = text.replace(bad, good)
    # 2. Ligatures
    text = text.translate(_LIG_TABLE)
    # 3. Smart quotes + dashes
    text = text.translate(_QUOTE_TABLE)
    text = text.translate(_DASH_TABLE)
    # 4. Zero-width chars
    text = _ZERO_WIDTH_RE.sub("", text)
    # 5. Control chars
    text = _CONTROL_RE.sub("", text)
    # 6. NFC normalise
    text = unicodedata.normalize("NFC", text)
    # 7. Soft hyphen joins
    text = _SOFT_HYPHEN_RE.sub(r"\1", text)
    # 8. Inline line-breaks
    text = _INLINE_BREAK_RE.sub(" ", text)
    # 9. Boilerplate
    text = _BOILERPLATE_RE.sub("", text)
    # 10. Page headers/footers
    text = _PAGE_HEADER_RE.sub("", text)
    # 11. Table pipe noise
    text = _TABLE_NOISE_RE.sub("", text)
    # 12. Deduplicate repeated lines
    text = _dedup_repeated_lines(text)
    # 13. Collapse whitespace
    text = _MULTI_SPACE_RE.sub(" ", text)
    text = _MULTI_BLANK_RE.sub("\n\n", text)
    # 14. Strip trailing spaces per line
    text = "\n".join(l.rstrip() for l in text.split("\n"))
    return text.strip()


def clean_chunk_doc(doc: dict) -> dict:
    """Clean all text fields of a MongoDB pdf_chunks document. Returns mutated doc."""
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
    """Returns 0-1 score of how noisy the text is."""
    if not text:
        return 0.0
    total = len(text)
    noise = 0
    noise += sum(1 for c in text if unicodedata.category(c) in ("Cc", "Cf", "Cs"))
    noise += len(_ZERO_WIDTH_RE.findall(text)) * 3
    noise += len(_BOILERPLATE_RE.findall(text)) * 50
    return min(1.0, noise / max(total, 1))

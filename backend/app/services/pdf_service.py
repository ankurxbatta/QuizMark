"""
pdf_service.py  —  Deep intelligent PDF processing for textbooks.

Key improvements over the naive approach:
  1. Chapter-aware parsing  — detects chapter/section boundaries using
     multiple flexible regex patterns that handle varied PDF formatting.
  2. Content filtering     — separates teaching content (definitions,
     theorems, formulas, worked examples) from exercises and
     boilerplate (TOC, index, copyright, footnotes).
  3. Formula preservation  — keeps mathematical notation intact
     rather than stripping it.
  4. Structured chunks     — returns a list of TextChunk objects,
     each with metadata, ready for targeted question generation.
  5. Keyword extraction    — identifies the key statistical terms
     in each section for downstream SLM scoring.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Optional

import pdfplumber
from pypdf import PdfReader


# ── No built-in topic mapping ──────────────────────────────────────────────────
# Topics are extracted directly from chapter titles in the PDF
# No normalization is applied — raw chapter titles are used as topic tags

# Patterns that identify TEACHING content worth keeping
_TEACHING_SIGNALS = re.compile(
    r"""
    (?:
        \bdefin(?:ition|e[sd]?)\b       |  # "Definition", "defined"
        \btheor(?:em|y)\b               |  # Theorem, Theory
        \bformula\b                     |  # Formula
        \bexample\s+\d+\b               |  # Example 2.3
        \bsolution\b                    |  # Solution block
        \bnote\b                        |  # NOTE callouts
        \bproperties?\s+of\b            |  # "Properties of ..."
        \bif\s+x\s+is\b                 |  # formal probability statements
        \bwhere\b                       |  # formula explanations
        \bequals?\b                     |  # definitions
        \bstandard\s+deviation\b        |
        \bmean\b                        |
        \bprobability\b                 |
        \bdistribution\b                |
        \bhypothesis\b                  |
        \bregression\b                  |
        \bvariance\b                    |
        \bconfidence\b                  |
        \bcorrelation\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Patterns that identify EXERCISE / BOILERPLATE content to skip
_SKIP_SIGNALS = re.compile(
    r"""
    (?:
        \bpractice\s+test\b             |  # practice test sections
        \bbring(?:ing)?\s+it\s+together\b |
        \bhomework\b                    |  # homework sections
        \breview\s+questions?\b         |
        \bchapter\s+review\b            |
        \bkey\s+terms?\b                |  # key terms glossary
        \bthis\s+openstax\b             |  # footer text
        \bdownload\s+for\s+free\b       |
        \btable\s+of\s+contents\b       |
        \bappendix\b                    |
        \bindex\b
    )
    """,
    re.IGNORECASE | re.VERBOSE | re.MULTILINE,
)


@dataclass
class TextChunk:
    """A semantically coherent block of text from the textbook."""
    chapter_num: int
    chapter_title: str
    section_title: str
    topic_tag: str
    text: str
    page_start: int
    page_end: int
    has_formula: bool
    has_example: bool
    teaching_density: float          # fraction of lines with teaching signals
    key_terms: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"Ch{self.chapter_num} § {self.section_title}"

    def to_prompt_block(self) -> str:
        """Format for injection into an LLM prompt."""
        parts = [
            f"[SOURCE: {self.label} | Topic: {self.topic_tag} | "
            f"Pages {self.page_start}–{self.page_end}]",
        ]
        if self.has_formula:
            parts.append("[Contains: mathematical formulas]")
        if self.has_example:
            parts.append("[Contains: worked examples]")
        parts.append("")
        parts.append(self.text)
        return "\n".join(parts)


# ── Page-level extraction ─────────────────────────────────────────────────────

def _extract_page_text(page) -> str:
    """Extract text from a pdfplumber page, preserving table structure."""
    # Try table extraction first — important for formula tables
    tables = page.extract_tables()
    table_texts = []
    if tables:
        for table in tables:
            rows = []
            for row in table:
                if row:
                    cleaned = " | ".join(cell.strip() if cell else "" for cell in row)
                    if cleaned.strip(" |"):
                        rows.append(cleaned)
            if rows:
                table_texts.append("\n".join(rows))

    # Main text
    text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""

    # Merge: replace table placeholders with structured table text
    if table_texts:
        text = text + "\n\n[TABLE]\n" + "\n\n[TABLE]\n".join(table_texts)

    return text


# ── Chapter / section detection ───────────────────────────────────────────────
#
# We use a list of patterns tried in order (most specific first).
# This handles all common textbook formats:
#   "Chapter 1 Sampling and Data"
#   "Chapter 1: Sampling and Data"
#   "Chapter 1 | Sampling and Data"
#   "CHAPTER 1 SAMPLING AND DATA"
#   "1 Sampling and Data"          ← numbered heading without "Chapter" keyword
#   "1. Sampling and Data"
#
_CHAPTER_PATTERNS = [
    # With "Chapter" keyword — colon, pipe, or just whitespace separator
    re.compile(
        r"^[ \t]*[Cc]hapter[ \t]+(\d{1,3})[ \t]*[:|]?[ \t]+([A-Z][^\n]{3,80})",
        re.MULTILINE,
    ),
    # CHAPTER (all-caps) keyword
    re.compile(
        r"^[ \t]*CHAPTER[ \t]+(\d{1,3})[ \t]*[:|]?[ \t]+([A-Z][^\n]{3,80})",
        re.MULTILINE,
    ),
    # Standalone "1 Title" or "1. Title" on its own line (title must start with capital)
    # Requires at least one word after the number to avoid matching page numbers.
    re.compile(
        r"^[ \t]*(\d{1,2})\.?[ \t]{1,4}([A-Z][a-zA-Z ]{5,70})[ \t]*$",
        re.MULTILINE,
    ),
]

_FRONT_MATTER_TITLES_RE = re.compile(
    r"""
    ^(?:
        preface                         |
        foreword                        |
        acknowledg(?:e)?ments?          |
        table\s+of\s+contents           |
        contents                        |
        copyright                       |
        dedication                      |
        about\s+the\s+authors?          |
        answer\s+key                    |
        answers?                        |
        solutions?                      |
        glossary                        |
        references                      |
        bibliography                    |
        index
    )$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_TOC_ENTRY_RE = re.compile(
    r"""
    ^\s*
    (?:
        (?:chapter\s+)?\d{1,2}\.?\s+[A-Z][^\n]{3,90}
        |
        [A-Z][A-Za-z ]{3,90}
    )
    \s*(?:\.{2,}|\s{3,})\s*
    \d{1,4}\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_SECTION_RE = re.compile(
    r"^(\d{1,2}\.\d{1,2})\s+([A-Z][^\n]{5,80})$",
    re.MULTILINE,
)
_EXAMPLE_RE = re.compile(r"\bExample\s+\d+", re.IGNORECASE)
_FORMULA_RE = re.compile(
    r"[=÷×±√∑∫µσ²]|"           # common math symbols
    r"\b(?:s²|σ²|μ|x̄|ȳ|Σ|√)\b|"
    r"\b\w+\s*=\s*[\w\d()\[\]]+\s*/|"   # fraction-like  a = b/c
    r"[A-Za-z]\s*[₀₁₂]\b|"              # subscript notation
    r"\bz\s*=\s*|t\s*=\s*|F\s*=\s*|χ²",
    re.UNICODE,
)


def _find_chapter_match(text: str):
    """
    Try each chapter pattern on the given text.
    Returns (chapter_number: int, chapter_title: str) or None.
    """
    for pattern in _CHAPTER_PATTERNS:
        for m in pattern.finditer(text):
            try:
                num = int(m.group(1))
            except (IndexError, ValueError):
                continue
            # Sanity check: chapter numbers are usually 1-50
            if num < 1 or num > 50:
                continue
            title = _clean_heading_title(m.group(2))
            # Reject very short or obviously wrong titles
            if len(title) < 4 or _is_front_matter_title(title):
                continue
            return num, title
    return None


def _clean_heading_title(title: str) -> str:
    """Remove table-of-contents leaders and running-header page numbers."""
    title = re.sub(r"\s*\.{2,}\s*\d+\s*$", "", title.strip())
    title = re.sub(r"\s+\d{1,4}\s*$", "", title)
    return title.strip()


def _is_front_matter_title(title: str) -> bool:
    """True for non-chapter book matter that is often numbered like pages."""
    normalized = re.sub(r"\s+", " ", title.strip())
    return bool(_FRONT_MATTER_TITLES_RE.match(normalized))


def _is_toc_like_page(text: str) -> bool:
    """Return true when extracted text looks like a table of contents page."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False

    first_lines = " ".join(lines[:12]).lower()
    if "table of contents" in first_lines:
        return True

    toc_entries = sum(1 for line in lines[:80] if _TOC_ENTRY_RE.match(line))
    return toc_entries >= 4


def _resolve_topic(chapter_title: str) -> str:
    """Return chapter title as-is as the topic. No normalization applied."""
    return chapter_title.strip() if chapter_title else "General"


def _extract_key_terms(text: str) -> list[str]:
    """Pull statistical terms (capitalized multi-word phrases, defined terms)."""
    definition_re = re.compile(
        r"([A-Z][a-z]+(?:\s+[A-Z]?[a-z]+){0,4})\s+(?:is|are)\s+(?:defined|called|known)",
        re.MULTILINE,
    )
    terms = [m.group(1).strip() for m in definition_re.finditer(text)]
    stat_terms_re = re.compile(
        r"\b(Standard Deviation|Variance|Mean|Median|Mode|Probability|"
        r"Distribution|Hypothesis|Regression|Correlation|Confidence Interval|"
        r"Central Limit Theorem|Normal Distribution|Binomial|Poisson|"
        r"Chi-Square|ANOVA|p-value|t-test|z-score|F-ratio|Type I|Type II)\b",
        re.IGNORECASE,
    )
    terms += [m.group(1) for m in stat_terms_re.finditer(text)]
    return list(dict.fromkeys(t.lower() for t in terms if len(t) > 3))


def _teaching_density(text: str) -> float:
    """Fraction of non-empty lines that contain teaching signals."""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return 0.0
    hits = sum(1 for l in lines if _TEACHING_SIGNALS.search(l))
    return hits / len(lines)


def _is_skip_block(text: str) -> bool:
    """True if this block is mostly exercises / boilerplate."""
    if not text.strip():
        return True
    lines = text.splitlines()
    exercise_lines = sum(
        1 for l in lines if re.match(r"^\s*\d{1,3}\.\s+[A-Z]", l)
    )
    if len(lines) > 0 and exercise_lines / len(lines) > 0.35:
        return True
    return bool(_SKIP_SIGNALS.search(text[:300]))


# ── Main public API ───────────────────────────────────────────────────────────

def parse_pdf_into_chunks(
    file_bytes: bytes,
    max_pages: int = 600,
    min_chunk_chars: int = 300,
    max_chunk_chars: int = 3000,
) -> list[TextChunk]:
    """
    Parse an entire textbook PDF into semantically labelled TextChunk objects.
    """
    chunks: list[TextChunk] = []
    current_chapter_num = 0
    current_chapter_title = "Unknown"
    current_section_title = "Introduction"
    current_topic = "General"
    buffer_lines: list[str] = []
    buffer_page_start = 1

    pdf = None

    try:
        pdf = pdfplumber.open(io.BytesIO(file_bytes))
        pages = pdf.pages[:max_pages]
    except Exception:
        reader = PdfReader(io.BytesIO(file_bytes))
        pages_raw = reader.pages[:max_pages]

        class _FakePage:
            def __init__(self, p, num):
                self._p = p
                self.page_number = num + 1
            def extract_text(self, **kw):
                return self._p.extract_text() or ""
            def extract_tables(self):
                return []

        pages = [_FakePage(p, i) for i, p in enumerate(pages_raw)]

    def _flush_buffer(page_end: int):
        nonlocal buffer_lines, buffer_page_start
        text = "\n".join(buffer_lines).strip()
        if len(text) < min_chunk_chars or _is_skip_block(text):
            buffer_lines = []
            return
        sub_chunks = _split_into_sub_chunks(text, max_chunk_chars)
        for sub in sub_chunks:
            if len(sub) < min_chunk_chars:
                continue
            chunks.append(TextChunk(
                chapter_num=current_chapter_num,
                chapter_title=current_chapter_title,
                section_title=current_section_title,
                topic_tag=current_topic,
                text=sub.strip(),
                page_start=buffer_page_start,
                page_end=page_end,
                has_formula=bool(_FORMULA_RE.search(sub)),
                has_example=bool(_EXAMPLE_RE.search(sub)),
                teaching_density=_teaching_density(sub),
                key_terms=_extract_key_terms(sub),
            ))
        buffer_lines = []

    for page in pages:
        page_num = getattr(page, "page_number", 0)

        try:
            raw_text = _extract_page_text(page)
        except Exception:
            raw_text = (page.extract_text() or "") if hasattr(page, "extract_text") else ""

        if not raw_text.strip():
            if hasattr(page, "flush_cache"):
                page.flush_cache()
            continue

        # Check for chapter heading on this page using flexible patterns.
        # TOC/front-matter pages often contain lines like "2 Preface", which
        # are page references rather than real chapter starts.
        ch_match = None if _is_toc_like_page(raw_text) else _find_chapter_match(raw_text)
        if ch_match:
            _flush_buffer(page_num - 1)
            buffer_page_start = page_num
            current_chapter_num, current_chapter_title = ch_match
            current_topic = _resolve_topic(current_chapter_title)
            current_section_title = "Introduction"

        # Check for section heading changes
        sec_matches = list(_SECTION_RE.finditer(raw_text))
        if sec_matches:
            for sec_m in sec_matches:
                _flush_buffer(page_num - 1)
                buffer_page_start = page_num
                current_section_title = sec_m.group(2).strip()

        # Accumulate lines
        lines = raw_text.splitlines()
        buffer_lines.extend(lines)

        # Flush if buffer is large enough
        total_chars = sum(len(l) for l in buffer_lines)
        if total_chars >= max_chunk_chars:
            _flush_buffer(page_num)
            buffer_page_start = page_num + 1

        if hasattr(page, "flush_cache"):
            page.flush_cache()

    # Final flush
    _flush_buffer(max_pages)

    if pdf is not None:
        pdf.close()

    return chunks


def _split_into_sub_chunks(text: str, max_chars: int) -> list[str]:
    """Split a large text block at paragraph boundaries."""
    if len(text) <= max_chars:
        return [text]

    paragraphs = re.split(r"\n{2,}", text)
    sub_chunks = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 <= max_chars:
            current = (current + "\n\n" + para).strip()
        else:
            if current:
                sub_chunks.append(current)
            current = para
    if current:
        sub_chunks.append(current)
    return sub_chunks if sub_chunks else [text[:max_chars]]


def extract_chapters_from_pdf(file_bytes: bytes, max_pages: int = 600) -> list[dict]:
    """
    Quickly scan a PDF and return the list of chapters found in it.
    Returns list of {"num": int, "title": str} dicts, ordered by chapter number.
    Falls back to a single entry {"num": 0, "title": "Entire Document"} if none found.

    Uses all flexible chapter patterns so it works across different textbooks.
    """
    seen: dict[int, str] = {}
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages[:max_pages]:
                try:
                    text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
                except Exception:
                    text = ""
                if _is_toc_like_page(text):
                    if hasattr(page, "flush_cache"):
                        page.flush_cache()
                    continue
                result = _find_chapter_match(text)
                if result:
                    num, title = result
                    if num not in seen:
                        seen[num] = title
                if hasattr(page, "flush_cache"):
                    page.flush_cache()
    except Exception:
        # pypdf fallback
        try:
            reader = PdfReader(io.BytesIO(file_bytes))
            for page in reader.pages[:max_pages]:
                text = page.extract_text() or ""
                if _is_toc_like_page(text):
                    continue
                result = _find_chapter_match(text)
                if result:
                    num, title = result
                    if num not in seen:
                        seen[num] = title
        except Exception:
            pass

    if not seen:
        return [{"num": 0, "title": "Entire Document"}]

    return [
        {"num": num, "title": title}
        for num, title in sorted(seen.items())
    ]


def get_pdf_info(file_bytes: bytes) -> dict:
    """Return basic metadata about a PDF."""
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        meta = reader.metadata or {}
        return {
            "pages": len(reader.pages),
            "title": meta.get("/Title", ""),
            "author": meta.get("/Author", ""),
        }
    except Exception as e:
        return {"pages": 0, "title": "", "author": "", "error": str(e)}


def extract_text_from_pdf(file_bytes: bytes, max_pages: int = 100) -> str:
    """
    Legacy flat extraction — kept for backward compatibility with .txt uploads.
    For textbooks, use parse_pdf_into_chunks() instead.
    """
    try:
        parts = []
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for i, page in enumerate(pdf.pages[:max_pages]):
                t = page.extract_text()
                if t:
                    parts.append(f"--- Page {i + 1} ---\n{t}")
        if parts:
            return "\n\n".join(parts)
    except Exception:
        pass
    reader = PdfReader(io.BytesIO(file_bytes))
    return "\n\n".join(
        p.extract_text() or "" for p in reader.pages[:max_pages]
    )

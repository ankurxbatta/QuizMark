"""
exercise_index.py — specialist RAG index for real textbook exercises
(grounding layer for question generation).

One document per mined exercise, built from the prose already stored in
pdf_chunks (no PDF re-read). The OpenStax exercise structures parsed here are:

  • EXAMPLE n.m worked examples (problem statement + following Solution text)
  • HOMEWORK / PRACTICE numbered items ("1.", "2." …), each possibly carrying
    a/b/c/d multiple-choice sub-options
  • group stems ("Use the following information to answer the next N exercises:
    <preamble>") whose shared preamble is attached to the next N numbered items

Generation retrieves these (retrieve_exercises) so questions are grounded in the
real exercises from the exact chapter rather than free-invented content. No LLM
spend in the builder — only the embedding of (stem + options + source_label).
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone

from app.core.config import settings
from app.services.mongo_vector_store import (
    CHUNKS_COLLECTION,
    EXERCISE_COLLECTION,
    EXERCISE_INDEX_NAME,
    INDEX_JOBS_COLLECTION,
    _get_collection,
    vector_search,
)

logger = logging.getLogger(__name__)


# ── Parsing ─────────────────────────────────────────────────────────────────────

# "Example 1.12" / "EXAMPLE 4.3" — heading for a worked example.
_EXAMPLE_RE = re.compile(r"^\s*(?:EXAMPLE|Example)\s+(\d+\.\d+)\s*$")
# "Solution 1.12" / "Solution" — start of a worked example's solution.
_SOLUTION_RE = re.compile(r"^\s*(?:SOLUTION|Solution)\s*(\d+\.\d+)?\s*$")
# A numbered exercise item: "1.", "23." at line start.
_NUM_ITEM_RE = re.compile(r"^\s*(\d{1,3})\.\s*(.*)$")
# An a/b/c/d option line (the option text usually follows on the next line(s)).
_OPT_RE = re.compile(r"^\s*([a-d])\.\s*(.*)$")
# "Use the following information to answer the next three exercises: <preamble>"
_GROUP_STEM_RE = re.compile(
    r"Use the following information to answer the next\s+([a-z0-9]+)\s+exercises?\s*:?\s*(.*)",
    re.IGNORECASE | re.DOTALL,
)
# Section heading inside HOMEWORK, e.g. "1.1 Definitions of Statistics ...".
_SECTION_HEAD_RE = re.compile(r"^\s*\d+\.\d+\s+[A-Z]")

_WORD_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}

_KIND_HOMEWORK = "homework"
_KIND_PRACTICE = "practice"
_KIND_EXAMPLE = "example"


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()[:300]


def _to_int(token: str) -> int:
    token = (token or "").strip().lower()
    if token.isdigit():
        return int(token)
    return _WORD_NUM.get(token, 0)


def _looks_like_junk(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 25:
        return True
    if re.fullmatch(r"[\d\s.,%$()/-]+", t):  # bare numbers / page-number noise
        return True
    return False


def split_exercises(chunk_text: str, table_texts: list, image_texts: list) -> list[dict]:
    """
    Parse one stored chunk's prose into structured exercise dicts.

    Returns a list of dicts with keys:
      stem, options (list[str]), solution (str), exercise_kind, inferred_qtype,
      table_markdown, figure_desc, source_label.
    """
    lines = (chunk_text or "").splitlines()
    table_md = (table_texts or [None])[0] or "" if table_texts else ""
    figure_desc = (image_texts or [None])[0] or "" if image_texts else ""

    out: list[dict] = []

    # Section context inside HOMEWORK/PRACTICE: "1.2 Data, Sampling ..." → "1.2".
    section_num = ""
    mode = ""  # "" | homework | practice (which section we're in)
    group_preamble = ""
    group_remaining = 0

    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        line = raw.strip()

        # Section mode toggles
        if re.match(r"^\s*HOMEWORK\b", raw):
            mode = _KIND_HOMEWORK
            group_preamble, group_remaining = "", 0
            i += 1
            continue
        if re.match(r"^\s*PRACTICE\b", raw):
            mode = _KIND_PRACTICE
            group_preamble, group_remaining = "", 0
            i += 1
            continue

        # ── Worked example ────────────────────────────────────────────────────
        m_ex = _EXAMPLE_RE.match(raw)
        if m_ex:
            label_num = m_ex.group(1)
            i += 1
            body: list[str] = []
            while i < n and not _EXAMPLE_RE.match(lines[i]) and not _SOLUTION_RE.match(lines[i]):
                body.append(lines[i].strip())
                i += 1
            solution_lines: list[str] = []
            if i < n and _SOLUTION_RE.match(lines[i]):
                i += 1
                while i < n and not _EXAMPLE_RE.match(lines[i]) and not _SOLUTION_RE.match(lines[i]):
                    if re.match(r"^\s*(HOMEWORK|PRACTICE)\b", lines[i]):
                        break
                    solution_lines.append(lines[i].strip())
                    i += 1
            stem = re.sub(r"\s+", " ", " ".join(b for b in body if b)).strip()
            solution = re.sub(r"\s+", " ", " ".join(s for s in solution_lines if s)).strip()
            options = _scan_options(body)
            if not _looks_like_junk(stem):
                out.append(_make_entry(
                    stem=stem, options=options, solution=solution,
                    kind=_KIND_EXAMPLE, label=f"Example {label_num}",
                    table_md=table_md, figure_desc=figure_desc,
                ))
            continue

        # Section heading inside a HOMEWORK/PRACTICE block resets group context
        if mode and _SECTION_HEAD_RE.match(raw):
            sm = re.match(r"^\s*(\d+\.\d+)\b", raw)
            section_num = sm.group(1) if sm else section_num
            group_preamble, group_remaining = "", 0
            i += 1
            continue

        # ── Group stem ("answer the next N exercises:") ───────────────────────
        m_grp = _GROUP_STEM_RE.search(line)
        if m_grp:
            group_remaining = _to_int(m_grp.group(1))
            group_preamble = re.sub(r"\s+", " ", m_grp.group(2) or "").strip()
            i += 1
            continue

        # ── Numbered exercise item ────────────────────────────────────────────
        m_num = _NUM_ITEM_RE.match(raw)
        if m_num and mode:
            num = m_num.group(1)
            first = m_num.group(2).strip()
            body = [first] if first else []
            i += 1
            # Gather continuation + option lines until the next numbered item or
            # a structural boundary.
            options: list[str] = []
            pending_opt: str | None = None
            while i < n:
                nxt = lines[i]
                if _NUM_ITEM_RE.match(nxt) or _EXAMPLE_RE.match(nxt) \
                        or re.match(r"^\s*(HOMEWORK|PRACTICE)\b", nxt) \
                        or _SECTION_HEAD_RE.match(nxt) or _GROUP_STEM_RE.search(nxt.strip()):
                    break
                m_opt = _OPT_RE.match(nxt)
                if m_opt:
                    if pending_opt is not None:
                        options.append(pending_opt.strip())
                    pending_opt = m_opt.group(2).strip()
                elif pending_opt is not None:
                    pending_opt += " " + nxt.strip()
                else:
                    body.append(nxt.strip())
                i += 1
            if pending_opt is not None:
                options.append(pending_opt.strip())

            stem = re.sub(r"\s+", " ", " ".join(b for b in body if b)).strip()
            if group_remaining > 0 and group_preamble:
                stem = f"{group_preamble} {stem}".strip()
                group_remaining -= 1
            options = [o for o in options if o]
            label_sec = f" {section_num}" if section_num else ""
            label = f"{mode.capitalize()}{label_sec} Q{num}"
            if not _looks_like_junk(stem):
                out.append(_make_entry(
                    stem=stem, options=options, solution="",
                    kind=mode, label=label,
                    table_md=table_md, figure_desc=figure_desc,
                ))
            continue

        i += 1

    return out


def _scan_options(body_lines: list[str]) -> list[str]:
    """Pull a/b/c/d option text out of an example body (options on own lines)."""
    options: list[str] = []
    pending: str | None = None
    for ln in body_lines:
        m = _OPT_RE.match(ln)
        if m:
            if pending is not None:
                options.append(pending.strip())
            pending = m.group(2).strip()
        elif pending is not None:
            pending += " " + ln.strip()
    if pending is not None:
        options.append(pending.strip())
    return [o for o in options if o]


def _make_entry(
    *, stem: str, options: list[str], solution: str, kind: str, label: str,
    table_md: str, figure_desc: str,
) -> dict:
    lower = stem.lower()
    references_table = bool(re.search(r"\btable\b", lower))
    references_figure = bool(re.search(r"\b(figure|graph|chart|histogram|plot)\b", lower))
    inferred = "mcq" if len(options) >= 2 else "short_answer"
    return {
        "stem": stem,
        "options": options,
        "solution": solution,
        "exercise_kind": kind,
        "inferred_qtype": inferred,
        "table_markdown": table_md if (references_table and table_md) else "",
        "figure_desc": figure_desc if (references_figure and figure_desc) else "",
        "source_label": label,
    }


# ── Doc identity + embedding text ──────────────────────────────────────────────

def exercise_doc_id(book_hash: str, parent_chunk_id: str, stem: str) -> str:
    key = f"{book_hash}:{parent_chunk_id}:{_normalise(stem)}"
    return hashlib.sha1(key.encode("utf-8", "ignore")).hexdigest()[:24]


def embedding_text(entry: dict) -> str:
    parts = [entry.get("source_label") or "", entry.get("stem") or ""]
    if entry.get("options"):
        parts.append("options: " + " | ".join(entry["options"][:6]))
    return " — ".join(p for p in parts if p)[:1500]


# ── Builder (runs on worker-clean / deepsearch queue) ──────────────────────────

async def build_exercise_index(book_id: str) -> dict:
    """Build (or rebuild) the exercise index for one book from stored chunks."""
    from app.services.llm_service import llm_service

    jobs_col = await _get_collection(INDEX_JOBS_COLLECTION)
    job_id = f"exercise:{book_id}"
    now = datetime.now(timezone.utc)
    await jobs_col.replace_one(
        {"_id": job_id},
        {"_id": job_id, "index": "exercise", "book_id": book_id,
         "status": "processing", "started_at": now, "finished_at": None, "error": None},
        upsert=True,
    )

    try:
        chunks_col = await _get_collection(CHUNKS_COLLECTION)
        cursor = chunks_col.find(
            {"book_id": book_id},
            {"text": 1, "table_texts": 1, "image_texts": 1, "book_hash": 1,
             "chapter_num": 1, "chapter_title": 1, "section_title": 1, "page_start": 1},
        )

        entries: list[dict] = []
        seen_ids: set[str] = set()
        async for chunk in cursor:
            chunk_id = str(chunk["_id"])
            exercises = split_exercises(
                chunk.get("text", ""),
                chunk.get("table_texts", []),
                chunk.get("image_texts", []),
            )
            for ex in exercises:
                doc_id = exercise_doc_id(chunk.get("book_hash") or book_id, chunk_id, ex["stem"])
                if doc_id in seen_ids:
                    continue
                seen_ids.add(doc_id)
                ex.update({
                    "_id": doc_id, "book_id": book_id,
                    "book_hash": chunk.get("book_hash"),
                    "parent_chunk_id": chunk_id,
                    "chapter_num": chunk.get("chapter_num", 0),
                    "chapter_title": chunk.get("chapter_title", ""),
                    "section_title": chunk.get("section_title", ""),
                    "page": chunk.get("page_start", 0),
                })
                entries.append(ex)

        if not entries:
            await jobs_col.update_one({"_id": job_id}, {"$set": {
                "status": "done", "exercises": 0, "finished_at": datetime.now(timezone.utc)}})
            return {"book_id": book_id, "exercises": 0}

        texts = [embedding_text(e) for e in entries]
        embeddings: list[list[float]] = []
        for i in range(0, len(texts), max(1, settings.EMBEDDING_BATCH_SIZE)):
            chunk_texts = texts[i:i + max(1, settings.EMBEDDING_BATCH_SIZE)]
            try:
                embeddings.extend(await llm_service.embed_batch(chunk_texts))
            except Exception:
                for t in chunk_texts:
                    try:
                        embeddings.append(await llm_service.embed(t))
                    except Exception:
                        embeddings.append([])

        col = await _get_collection(EXERCISE_COLLECTION)
        stored = 0
        for e, emb in zip(entries, embeddings):
            if not emb:
                continue
            doc = {
                "_id": e["_id"], "book_id": e["book_id"], "book_hash": e["book_hash"],
                "parent_chunk_id": e["parent_chunk_id"],
                "chapter_num": e["chapter_num"], "chapter_title": e["chapter_title"],
                "section_title": e["section_title"], "page": e["page"],
                "stem": e["stem"][:4000], "options": e["options"][:8],
                "solution": e["solution"][:2000], "exercise_kind": e["exercise_kind"],
                "inferred_qtype": e["inferred_qtype"],
                "table_markdown": e["table_markdown"][:6000],
                "figure_desc": e["figure_desc"][:2000],
                "source_label": e["source_label"],
                "embedding": emb, "created_at": datetime.now(timezone.utc),
            }
            try:
                await col.replace_one({"_id": doc["_id"]}, doc, upsert=True)
                stored += 1
            except Exception as exc:
                logger.warning(f"exercise_index upsert failed for {doc['_id']}: {exc}")

        try:
            await col.create_index("parent_chunk_id")
            await col.create_index("book_id")
        except Exception:
            pass

        await jobs_col.update_one({"_id": job_id}, {"$set": {
            "status": "done", "exercises": stored, "finished_at": datetime.now(timezone.utc)}})
        logger.info(f"exercise_index: built {stored} exercise docs for book '{book_id}'")
        return {"book_id": book_id, "exercises": stored}

    except Exception as exc:
        await jobs_col.update_one({"_id": job_id}, {"$set": {
            "status": "failed", "error": str(exc)[:500],
            "finished_at": datetime.now(timezone.utc)}})
        raise


# ── Retrieval + prompt rendering ───────────────────────────────────────────────

async def retrieve_exercises(
    query_embedding: list[float],
    book_id: str | None = None,
    chapter_num: int | None = None,
    k: int = 4,
) -> list[dict]:
    if not settings.EXERCISE_INDEX_ENABLED:
        return []
    filters = {"chapter_num": chapter_num} if chapter_num is not None else None
    return await vector_search(
        query_embedding, k=k, book_id=book_id, filters=filters,
        collection_name=EXERCISE_COLLECTION, index_name=EXERCISE_INDEX_NAME,
    )


def render_exercises_block(exercises: list[dict]) -> str:
    """Render retrieved exercise docs as a prompt section. Empty string if none."""
    if not exercises:
        return ""
    lines = [
        "SEED EXERCISES — real exercises from this exact chapter of the textbook. "
        "Use them as the backbone of your questions. For MCQ and True/False you MAY "
        "reuse a strong seed verbatim when it fits the requested type and level. For "
        "short-answer, keep the concept and structure but CHANGE the numbers/scenario "
        "so the textbook's published solution does not directly apply. Never introduce "
        "content outside the SOURCE CONTENT and these seeds.",
    ]
    for e in exercises:
        label = e.get("source_label") or e.get("exercise_kind") or "exercise"
        stem = (e.get("stem") or "").strip()[:600]
        lines.append(f"- [{label}] {stem}")
        for opt, letter in zip(e.get("options", [])[:6], "abcdef"):
            lines.append(f"    {letter}. {opt[:200]}")
        if e.get("solution"):
            lines.append(f"    Solution: {e['solution'][:300]}")
    return "\n".join(lines)


async def delete_exercise_index(book_id: str) -> int:
    try:
        col = await _get_collection(EXERCISE_COLLECTION)
        result = await col.delete_many({"book_id": book_id})
        jobs_col = await _get_collection(INDEX_JOBS_COLLECTION)
        await jobs_col.delete_one({"_id": f"exercise:{book_id}"})
        return result.deleted_count
    except Exception as exc:
        logger.warning(f"delete_exercise_index failed (non-fatal): {exc}")
        return 0


async def exercise_index_status() -> list[dict]:
    out: list[dict] = []
    try:
        jobs_col = await _get_collection(INDEX_JOBS_COLLECTION)
        col = await _get_collection(EXERCISE_COLLECTION)
        async for job in jobs_col.find({"index": "exercise"}):
            book_id = job.get("book_id")
            count = await col.count_documents({"book_id": book_id})
            out.append({
                "book_id": book_id, "status": job.get("status"),
                "exercises": count, "started_at": job.get("started_at"),
                "finished_at": job.get("finished_at"), "error": job.get("error"),
            })
    except Exception as exc:
        logger.warning(f"exercise_index_status failed: {exc}")
    return out

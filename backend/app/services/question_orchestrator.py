"""
question_orchestrator.py — Multi-round agentic question bank generation.

Implements the full iterative DeepSearch loop for question generation:

  Round 1 — Intent + broad retrieval
    • LLM generates 4 exam-focused sub-queries for the chapter topic
    • Parallel vector searches surface the most testable chunks
    • Generate ~70% of target count with Bloom's distribution

  Round 2 — Coverage audit + gap fill (iterative)
    • Inspect Bloom's level distribution of Round 1 output
    • For each under-represented level (< 50% of target share):
        – Generate a level-specific retrieval query
        – DeepSearch for chunks that best support that cognitive level
        – Run targeted generation locked to that Bloom's level
    • Each gap uses Round 1 questions as uniqueness context

  Round 3 — Validation + dedup
    • Remove near-duplicate questions (prefix similarity)
    • Enforce Bloom's distribution by trimming over-represented levels
    • If still below target count, run a small top-up pass
    • Return final validated set capped at requested count

This mirrors the Shiksha Copilot SectionsGraphOrchestrator pattern:
each round's output feeds directly into the next round as context.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from typing import Optional

from app.services.question_generator import (
    DbChunk,
    deep_retrieve_for_generation,
    extract_chapter_concepts,
    generate_questions_from_chunks,
    generate_targeted_bloom_questions,
    _validate_questions,
)


# ── Bloom's targets and retrieval focus ───────────────────────────────────────

# Target share for each Bloom's level across the final question set
_BLOOMS_TARGET_PCT: dict[str, float] = {
    "L1": 0.15,
    "L2": 0.20,
    "L3": 0.30,
    "L4": 0.20,
    "L5": 0.15,
}

# Retrieval query suffix for gap-fill retrieval — emphasises the right content
_BLOOMS_RETRIEVAL_SUFFIX: dict[str, str] = {
    "L1": "definitions key terms vocabulary formulas names",
    "L2": "explain meaning interpret summarise concept",
    "L3": "calculate apply formula worked example numerical scenario",
    "L4": "compare methods assumptions conditions analyze breakdown",
    "L5": "evaluate justify critique assess statistical decision",
}


# ── Coverage audit ─────────────────────────────────────────────────────────────

def _audit_bloom_gaps(
    questions: list[dict],
    target_count: int,
) -> list[dict]:
    """
    Identify Bloom's levels that are under-represented relative to targets.

    A level is flagged as a gap if it has fewer than 50% of its target share.
    Returns a list of gap dicts: {bloom_level, needed, retrieval_suffix}
    """
    n = len(questions)
    level_counts: Counter = Counter(q.get("bloom_level", "L3") for q in questions)
    gaps: list[dict] = []
    for level, target_pct in _BLOOMS_TARGET_PCT.items():
        target_n = max(1, round(target_count * target_pct))
        current_n = level_counts.get(level, 0)
        # Flag gap if current count is less than 50% of target
        if current_n < target_n * 0.5:
            needed = target_n - current_n
            gaps.append({
                "bloom_level": level,
                "needed": max(1, needed),
                "retrieval_suffix": _BLOOMS_RETRIEVAL_SUFFIX[level],
            })
    return gaps


# ── Round 3 helpers ────────────────────────────────────────────────────────────

def _dedup_by_prefix(questions: list[dict], prefix_len: int = 80) -> list[dict]:
    """Remove questions whose first `prefix_len` characters match an earlier question."""
    seen: set[str] = set()
    unique: list[dict] = []
    for q in questions:
        key = q.get("question_text", "")[:prefix_len].lower().strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(q)
    return unique


def _balance_bloom_distribution(
    questions: list[dict],
    target_count: int,
) -> list[dict]:
    """
    Trim over-represented Bloom's levels so the distribution is balanced.
    Prioritises keeping questions from under-represented levels.
    """
    level_target: dict[str, int] = {
        lvl: max(1, round(target_count * pct))
        for lvl, pct in _BLOOMS_TARGET_PCT.items()
    }
    buckets: dict[str, list[dict]] = {lvl: [] for lvl in _BLOOMS_TARGET_PCT}
    overflow: list[dict] = []

    for q in questions:
        lvl = q.get("bloom_level", "L3")
        if lvl in buckets and len(buckets[lvl]) < level_target.get(lvl, 999):
            buckets[lvl].append(q)
        else:
            overflow.append(q)

    balanced: list[dict] = []
    for lvl in ("L1", "L2", "L3", "L4", "L5"):
        balanced.extend(buckets[lvl])

    # Fill remaining slots with overflow (any level), maintaining order
    slots_left = target_count - len(balanced)
    balanced.extend(overflow[:max(0, slots_left)])
    return balanced


# ── Main orchestrator ──────────────────────────────────────────────────────────

async def orchestrate_question_bank(
    chapter_topic: str,
    book_id: Optional[str],
    question_type: str,
    count: int,
    difficulty: str = "all",
    existing_questions: Optional[list[str]] = None,
) -> list[dict]:
    """
    3-round agentic question bank generation for one chapter/topic.

    Args:
        chapter_topic:      Chapter title + topic tag (e.g. "Chapter 5: Confidence Intervals").
        book_id:            Restrict retrieval to one book, or None for all books.
        question_type:      "short_answer" | "mcq" | "true_false"
        count:              Target number of questions.
        difficulty:         "all" | "easy" | "medium" | "hard"
        existing_questions: Question texts already in the bank (uniqueness context).

    Returns:
        Validated list of question dicts, len <= count.
    """
    existing_questions = list(existing_questions or [])
    print(f"[ORCH] Starting 3-round orchestration for '{chapter_topic}' | target={count}")

    # ── Round 0: "Read the chapter carefully" → extract key concepts ──────────
    seed_raw = await deep_retrieve_for_generation(
        topic=chapter_topic, book_id=book_id, k=6,
    )
    seed_chunks = [DbChunk(doc) for doc in seed_raw]
    chapter_concepts: list[str] = await extract_chapter_concepts(
        chapter_topic, seed_chunks, n=8,
    )
    if chapter_concepts:
        print(f"[ORCH] Round 0 — concepts: {chapter_concepts}")

    # ── Round 1: Broad DeepSearch retrieval + initial generation ──────────────
    r1_target = max(int(count * 0.75), count)  # slight overgenerate to allow trimming
    print(f"[ORCH] Round 1 — broad retrieval, target={r1_target}")

    # Concept-augmented topic gives retrieval extra signal
    enriched_topic = chapter_topic
    if chapter_concepts:
        enriched_topic = f"{chapter_topic} | concepts: {', '.join(chapter_concepts)}"

    raw_chunks_r1 = await deep_retrieve_for_generation(
        topic=enriched_topic,
        book_id=book_id,
        k=max(r1_target * 2, 12),
    )
    chunks_r1 = [DbChunk(doc) for doc in raw_chunks_r1]

    if not chunks_r1:
        print("[ORCH] Round 1 — no chunks retrieved, returning empty")
        return []

    questions: list[dict] = await generate_questions_from_chunks(
        chunks_r1,
        question_type=question_type,
        count=r1_target,
        difficulty=difficulty,
        existing_questions=existing_questions,
    )
    print(f"[ORCH] Round 1 — generated {len(questions)} questions")

    # Accumulate uniqueness context
    seen_texts = list(existing_questions) + [q["question_text"] for q in questions]

    # ── Round 2: Coverage audit → gap-fill per missing Bloom's level ──────────
    gaps = _audit_bloom_gaps(questions, target_count=count)
    print(f"[ORCH] Round 2 — {len(gaps)} Bloom's gaps: {[g['bloom_level'] for g in gaps]}")

    # Process each gap sequentially so each gap's output feeds the next's context
    for gap in gaps:
        bloom_level = gap["bloom_level"]
        needed = gap["needed"]
        print(f"[ORCH] Round 2 — filling {bloom_level} gap, need {needed} questions")

        # Level-specific retrieval query
        focused_topic = f"{gap['retrieval_suffix']} {chapter_topic}"
        raw_chunks_r2 = await deep_retrieve_for_generation(
            topic=focused_topic,
            book_id=book_id,
            k=8,
        )
        chunks_r2 = [DbChunk(doc) for doc in raw_chunks_r2]

        if not chunks_r2:
            print(f"[ORCH] Round 2 — no chunks for {bloom_level} gap, skipping")
            continue

        gap_questions = await generate_targeted_bloom_questions(
            chunks=chunks_r2,
            question_type=question_type,
            count=needed,
            bloom_level=bloom_level,
            existing_questions=seen_texts,
        )
        print(f"[ORCH] Round 2 — {bloom_level} gap filled with {len(gap_questions)} questions")
        questions.extend(gap_questions)
        seen_texts.extend(q["question_text"] for q in gap_questions)

    print(f"[ORCH] After Round 2 — {len(questions)} total questions")

    # ── Round 2b: Concept coverage gap-fill ───────────────────────────────────
    if chapter_concepts:
        joined_qs = " || ".join(q.get("question_text", "").lower() for q in questions)
        uncovered = [c for c in chapter_concepts if c.lower() not in joined_qs]
        if uncovered:
            print(f"[ORCH] Round 2b — {len(uncovered)} uncovered concepts: {uncovered}")
            for concept in uncovered[:5]:  # cap so we don't explode the run
                focused = f"{concept} {chapter_topic}"
                raw_cc = await deep_retrieve_for_generation(
                    topic=focused, book_id=book_id, k=6,
                )
                cc_chunks = [DbChunk(doc) for doc in raw_cc]
                if not cc_chunks:
                    continue
                cc_qs = await generate_targeted_bloom_questions(
                    chunks=cc_chunks,
                    question_type=question_type,
                    count=1,
                    bloom_level="L3",  # default — apply/analyse most uncovered concepts
                    existing_questions=seen_texts,
                )
                if cc_qs:
                    questions.extend(cc_qs)
                    seen_texts.extend(q["question_text"] for q in cc_qs)

    # ── Round 3: Dedup + balance + top-up if still short ─────────────────────
    questions = _dedup_by_prefix(questions)
    questions = _balance_bloom_distribution(questions, target_count=count)
    print(f"[ORCH] After Round 3 dedup/balance — {len(questions)} questions")

    # Top-up: if we're more than 20% below target, do one more small pass
    shortfall = count - len(questions)
    if shortfall > max(1, count * 0.2) and chunks_r1:
        print(f"[ORCH] Round 3 top-up — generating {shortfall} more questions")
        topup = await generate_questions_from_chunks(
            chunks_r1,
            question_type=question_type,
            count=shortfall,
            difficulty=difficulty,
            existing_questions=seen_texts,
        )
        questions.extend(topup)
        questions = _dedup_by_prefix(questions)

    final = questions[:count]
    print(f"[ORCH] Final — {len(final)} questions for '{chapter_topic}'")
    return final

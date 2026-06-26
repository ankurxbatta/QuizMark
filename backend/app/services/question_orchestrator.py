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

import logging
from collections import Counter
from typing import Optional

from app.services.question_generator import (
    DbChunk,
    deep_retrieve_for_generation,
    extract_chapter_concepts,
    generate_questions_from_chunks,
    generate_targeted_bloom_questions,
)

logger = logging.getLogger(__name__)


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

# When an instructor pins an explicit difficulty, it becomes a HARD constraint:
# Bloom's rebalancing is suppressed and every question stays in this band.
_DIFFICULTY_BLOOMS: dict[str, list[str]] = {
    "easy": ["L1", "L2"],
    "medium": ["L3", "L4"],
    "hard": ["L5"],
}
# Single Bloom's level to lock targeted gap-fill to, per explicit difficulty.
_DIFFICULTY_PRIMARY_BLOOM: dict[str, str] = {
    "easy": "L2", "medium": "L3", "hard": "L5",
}
# Difficulty-aware grounding: which real-exercise kinds to surface first.
# Worked examples (and the short Try-It practice boxes) anchor easy/medium
# questions; homework/practice problems anchor the harder ones.
# Note: these only RE-RANK retrieved exercises (retrieve_exercises does a stable
# sort, not a filter), so no kind is ever excluded — they just bias which kinds
# surface first for a given band. "activity"/collaborative tasks anchor the
# applied middle band.
_DIFFICULTY_EXERCISE_KINDS: dict[str, list[str]] = {
    "easy": ["example", "try_it"],
    "medium": ["example", "try_it", "activity", "homework", "practice"],
    "hard": ["homework", "practice", "activity"],
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


# ── Seed-exercise retrieval (WS3 — ground generation in real exercises) ────────

async def _retrieve_seed_exercises(
    chapter_topic: str,
    book_id: Optional[str],
    chapter_num: Optional[int],
    difficulty: str = "all",
) -> list[dict]:
    """
    Retrieve real exercises from this exact chapter ONCE so generation rounds can
    reuse them as seeds. Degrades to [] when the index is disabled/empty/failing.

    When an explicit difficulty is set, bias the seeds toward the exercise kinds
    that best anchor that band (worked examples for easy, homework for hard).
    """
    from app.core.config import settings
    if not settings.EXERCISE_INDEX_ENABLED:
        return []
    try:
        from app.services.exercise_index import retrieve_exercises
        from app.services.llm_service import slm_service
        q_emb = await slm_service.embed(chapter_topic)
        preferred_kinds = _DIFFICULTY_EXERCISE_KINDS.get(difficulty)
        return await retrieve_exercises(
            q_emb, book_id=book_id, chapter_num=chapter_num, k=6,
            preferred_kinds=preferred_kinds,
        )
    except Exception as exc:
        logger.debug(f"[ORCH] seed exercise retrieval skipped: {exc}")
        return []


async def _build_asset_directive(
    require_table: bool,
    require_figure: bool,
    book_id: Optional[str],
    chapter_num: Optional[int],
    chapter_topic: str,
) -> str:
    """Build a prompt directive (with real chapter tables/figures) that BIASES
    generation toward table/figure questions. require_table/require_figure are
    PREFERENCES, not hard constraints: the model still decides per question
    whether an asset genuinely helps (and constructs it from the real numbers),
    so it never forces an unanswerable asset onto a question. '' if not requested
    or nothing retrievable."""
    if not (require_table or require_figure):
        return ""
    from app.core.config import settings
    from app.services.llm_service import slm_service
    blocks: list[str] = []
    try:
        emb = await slm_service.embed(chapter_topic)
        if require_table and settings.TABLE_INDEX_ENABLED:
            from app.services.table_index import retrieve_tables, render_tables_block
            b = render_tables_block(await retrieve_tables(emb, book_id=book_id, chapter_num=chapter_num, k=3))
            if b:
                blocks.append(b)
        if require_figure and settings.FIGURE_INDEX_ENABLED:
            from app.services.figure_index import retrieve_figures, render_figures_block
            b = render_figures_block(await retrieve_figures(emb, book_id=book_id, chapter_num=chapter_num, k=3))
            if b:
                blocks.append(b)
    except Exception as exc:
        logger.debug(f"[ORCH] asset directive retrieval skipped: {exc}")
    parts: list[str] = []
    if require_table:
        # Prefer (don't hard-force) table questions: when the model builds one it
        # MUST include the full table in 'assets' (the gate rejects a question that
        # references a table it didn't include). Allow a clean concept question when
        # a table genuinely doesn't fit, so generation never fails to 0.
        parts.append(
            "PREFER TABLE QUESTIONS: favour questions built around a data TABLE that YOU construct fully from the "
            "chapter's real numbers below. If you build a table question you MUST include the COMPLETE table (every "
            "cell filled with its correct value, except a cell the student is asked to compute) as a clean markdown "
            "table in the question's 'assets' array — never refer to a table you do not include. If a table genuinely "
            "does not fit a concept, ask a clean concept question instead. Require a real SKILL (compute/interpret/compare/conclude), not a single-cell lookup."
        )
    if require_figure:
        # Figures are AI-generated images, which are reliable ONLY for qualitative
        # SHAPES/PATTERNS — never for precise data values. So steer every figure
        # question to a CONCEPTUAL illustration and test what the shape means.
        # The image is generated only after the question passes the gate.
        parts.append(
            "FIGURE QUESTIONS (conceptual only): build EACH question around a CONCEPTUAL figure that illustrates a "
            "SHAPE or PATTERN — e.g. a right-skewed / left-skewed / symmetric distribution, a normal bell curve, a "
            "positive / negative / no-correlation scatter pattern, or a boxplot showing spread. The question MUST test "
            "what the shape or pattern MEANS (skewness, how mean vs median compare, correlation direction/strength, "
            "spread/outliers) — NEVER reading a precise numeric value off the figure (the image is a qualitative "
            "illustration, not a data source). Emit a concise figure_spec describing the shape/pattern as the "
            "question's 'assets' entry; do NOT assume an image already exists."
        )
    directive = "\n".join(parts)
    if blocks:
        directive += "\n\n" + "\n\n".join(blocks)
    return directive


# ── Main orchestrator ──────────────────────────────────────────────────────────

async def orchestrate_question_bank(
    chapter_topic: str,
    book_id: Optional[str],
    question_type: str,
    count: int,
    difficulty: str = "all",
    existing_questions: Optional[list[str]] = None,
    chapter_num: Optional[int] = None,
    require_table: bool = False,
    require_figure: bool = False,
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
    # An explicit easy/medium/hard request is a HARD constraint: we suppress
    # Bloom's-distribution rebalancing and keep every question in that band.
    explicit_difficulty = difficulty in ("easy", "medium", "hard")
    logger.info(
        f"[ORCH] Starting 3-round orchestration for '{chapter_topic}' | target={count} "
        f"| difficulty={difficulty}{' (locked)' if explicit_difficulty else ''}"
    )

    # ── Dedicated asset paths: tables are grounded in REAL cleaned chapter tables;
    # figures are conceptual-shape questions whose image is generated only after the
    # gate. When BOTH toggles are on, split the requested count between them. ──────
    if require_table or require_figure:
        from app.services.question_generator import (
            generate_figure_grounded_questions,
            generate_table_grounded_questions,
        )
        if require_table and require_figure:
            n_table = (count + 1) // 2
            n_figure = count - n_table
        elif require_table:
            n_table, n_figure = count, 0
        else:
            n_table, n_figure = 0, count
        asset_qs: list[dict] = []
        if n_table:
            asset_qs.extend(await generate_table_grounded_questions(
                book_id=book_id, chapter_num=chapter_num, question_type=question_type,
                count=n_table, difficulty=difficulty, existing_questions=existing_questions,
            ))
        if n_figure:
            asset_qs.extend(await generate_figure_grounded_questions(
                book_id=book_id, chapter_num=chapter_num, chapter_topic=chapter_topic,
                question_type=question_type, count=n_figure, difficulty=difficulty,
                existing_questions=existing_questions,
            ))
        if asset_qs:
            logger.info(f"[ORCH] asset paths produced {len(asset_qs)} question(s) (table={n_table}, figure={n_figure})")
            return asset_qs[:count]
        logger.info("[ORCH] asset paths empty — falling back to standard generation")

    # ── Round 0: "Read the chapter carefully" → extract key concepts ──────────
    seed_raw = await deep_retrieve_for_generation(
        topic=chapter_topic, book_id=book_id, chapter_num=chapter_num, k=6,
    )
    seed_chunks = [DbChunk(doc) for doc in seed_raw]
    chapter_concepts: list[str] = await extract_chapter_concepts(
        chapter_topic, seed_chunks, n=8,
    )
    if chapter_concepts:
        logger.info(f"[ORCH] Round 0 — concepts: {chapter_concepts}")

    # Optional table/figure requirement: build a directive (with real chapter
    # tables/figures) that forces every generated question to be built around them.
    asset_directive = await _build_asset_directive(
        require_table, require_figure, book_id, chapter_num, chapter_topic,
    )
    if asset_directive:
        logger.info(f"[ORCH] asset requirement active (table={require_table}, figure={require_figure})")

    # ── Round 1: Broad DeepSearch retrieval + initial generation ──────────────
    # Overgenerate so Round 3 dedup/balance/hard-guard trimming still lands at
    # the requested count instead of consistently undershooting it.
    r1_target = max(int(count * 1.2), count)
    logger.info(f"[ORCH] Round 1 — broad retrieval, target={r1_target}")

    # Concept-augmented topic gives retrieval extra signal
    enriched_topic = chapter_topic
    if chapter_concepts:
        enriched_topic = f"{chapter_topic} | concepts: {', '.join(chapter_concepts)}"

    raw_chunks_r1, fused_r1 = await deep_retrieve_for_generation(
        topic=enriched_topic,
        book_id=book_id,
        chapter_num=chapter_num,
        k=max(r1_target * 2, 12),
        return_context=True,
    )
    chunks_r1 = [DbChunk(doc) for doc in raw_chunks_r1]
    # Inject the dedicated math/figure/table indexes into mainline generation —
    # otherwise Round 1 (the bulk of the bank) only ever sees chunk prose and the
    # specialist indexes are retrieved but thrown away.
    specialist_block = fused_r1.specialist_block()
    if specialist_block:
        logger.info("[ORCH] Round 1 — specialist index context attached")

    if not chunks_r1:
        logger.warning("[ORCH] Round 1 — no chunks retrieved, returning empty")
        return []

    # Retrieve real chapter exercises ONCE (cheap, reused across all rounds).
    seed_exercises = await _retrieve_seed_exercises(
        chapter_topic, book_id, chapter_num, difficulty=difficulty,
    )
    if seed_exercises:
        logger.info(f"[ORCH] Round 1 — {len(seed_exercises)} seed exercises retrieved")

    questions: list[dict] = await generate_questions_from_chunks(
        chunks_r1,
        question_type=question_type,
        count=r1_target,
        difficulty=difficulty,
        existing_questions=existing_questions,
        seed_exercises=seed_exercises,
        asset_directive=asset_directive,
        extra_context=specialist_block,
    )
    logger.info(f"[ORCH] Round 1 — generated {len(questions)} questions")

    # Accumulate uniqueness context
    seen_texts = list(existing_questions) + [q["question_text"] for q in questions]

    # ── Round 2: Coverage audit → gap-fill per missing Bloom's level ──────────
    if explicit_difficulty:
        # Difficulty is locked → no Bloom's rebalancing. Only fill a raw count
        # shortfall, and keep it inside the requested band's primary level.
        primary = _DIFFICULTY_PRIMARY_BLOOM[difficulty]
        shortfall = count - len(questions)
        gaps = (
            [{"bloom_level": primary, "needed": shortfall,
              "retrieval_suffix": _BLOOMS_RETRIEVAL_SUFFIX[primary]}]
            if shortfall > 0 else []
        )
    else:
        gaps = _audit_bloom_gaps(questions, target_count=count)
    logger.info(f"[ORCH] Round 2 — {len(gaps)} gaps: {[g['bloom_level'] for g in gaps]}")

    # Process each gap sequentially so each gap's output feeds the next's context.
    # Gap-filling is enrichment — a failure (e.g. a transient embed error during
    # retrieval) must never discard the questions already generated.
    for gap in gaps:
        bloom_level = gap["bloom_level"]
        needed = gap["needed"]
        logger.info(f"[ORCH] Round 2 — filling {bloom_level} gap, need {needed} questions")

        try:
            # Level-specific retrieval query
            focused_topic = f"{gap['retrieval_suffix']} {chapter_topic}"
            raw_chunks_r2 = await deep_retrieve_for_generation(
                topic=focused_topic,
                book_id=book_id,
                chapter_num=chapter_num,
                k=8,
            )
            chunks_r2 = [DbChunk(doc) for doc in raw_chunks_r2]

            if not chunks_r2:
                logger.warning(f"[ORCH] Round 2 — no chunks for {bloom_level} gap, skipping")
                continue

            gap_questions = await generate_targeted_bloom_questions(
                chunks=chunks_r2,
                question_type=question_type,
                count=needed,
                bloom_level=bloom_level,
                existing_questions=seen_texts,
                book_id=book_id,
                chapter_num=chapter_num,
                seed_exercises=seed_exercises,
                asset_directive=asset_directive,
            )
        except Exception as exc:
            logger.warning(f"[ORCH] Round 2 — {bloom_level} gap fill failed (non-fatal): {exc}")
            continue
        logger.info(f"[ORCH] Round 2 — {bloom_level} gap filled with {len(gap_questions)} questions")
        questions.extend(gap_questions)
        seen_texts.extend(q["question_text"] for q in gap_questions)

    logger.info(f"[ORCH] After Round 2 — {len(questions)} total questions")

    # ── Round 2b: Concept coverage gap-fill ───────────────────────────────────
    if chapter_concepts:
        joined_qs = " || ".join(q.get("question_text", "").lower() for q in questions)
        uncovered = [c for c in chapter_concepts if c.lower() not in joined_qs]
        if uncovered:
            logger.info(f"[ORCH] Round 2b — {len(uncovered)} uncovered concepts: {uncovered}")
            for concept in uncovered[:5]:  # cap so we don't explode the run
                try:
                    focused = f"{concept} {chapter_topic}"
                    raw_cc = await deep_retrieve_for_generation(
                        topic=focused, book_id=book_id, chapter_num=chapter_num, k=6,
                    )
                    cc_chunks = [DbChunk(doc) for doc in raw_cc]
                    if not cc_chunks:
                        continue
                    cc_bloom = (
                        _DIFFICULTY_PRIMARY_BLOOM[difficulty]
                        if explicit_difficulty else "L3"
                    )
                    cc_qs = await generate_targeted_bloom_questions(
                        chunks=cc_chunks,
                        question_type=question_type,
                        count=1,
                        bloom_level=cc_bloom,  # locked to band when difficulty is explicit
                        existing_questions=seen_texts,
                        book_id=book_id,
                        chapter_num=chapter_num,
                        seed_exercises=seed_exercises,
                        asset_directive=asset_directive,
                    )
                except Exception as exc:
                    logger.warning(f"[ORCH] Round 2b — concept {concept!r} fill failed (non-fatal): {exc}")
                    continue
                if cc_qs:
                    questions.extend(cc_qs)
                    seen_texts.extend(q["question_text"] for q in cc_qs)

    # ── Round 3: Dedup + balance + top-up if still short ─────────────────────
    questions = _dedup_by_prefix(questions)
    if explicit_difficulty:
        # Difficulty locked → no Bloom's trimming; just cap to the requested count.
        questions = questions[:count]
    else:
        questions = _balance_bloom_distribution(questions, target_count=count)
    logger.info(f"[ORCH] After Round 3 dedup/balance — {len(questions)} questions")

    # Top-up: dedup/balance can leave us below the requested count. Regenerate
    # quality replacements until we hit the target, bounded by GEN_TOPUP_MAX_ROUNDS
    # so a thin chapter can't loop forever or run up unbounded API cost. Happy path
    # (no shortfall) makes zero extra calls.
    from app.core.config import settings
    for attempt in range(max(0, int(settings.GEN_TOPUP_MAX_ROUNDS))):
        shortfall = count - len(questions)
        if shortfall <= 0 or not chunks_r1:
            break
        logger.info(
            f"[ORCH] Round 3 top-up {attempt + 1}/{settings.GEN_TOPUP_MAX_ROUNDS} "
            f"— generating {shortfall} more questions"
        )
        before = len(questions)
        topup = await generate_questions_from_chunks(
            chunks_r1,
            question_type=question_type,
            count=shortfall,
            difficulty=difficulty,
            existing_questions=seen_texts,
            seed_exercises=seed_exercises,
            asset_directive=asset_directive,
            extra_context=specialist_block,
        )
        questions.extend(topup)
        questions = _dedup_by_prefix(questions)
        if explicit_difficulty:
            questions = questions[:count]
        else:
            questions = _balance_bloom_distribution(questions, target_count=count)
        seen_texts.extend(q["question_text"] for q in topup)
        # No net gain (LLM only returned dups/junk) → stop early rather than burn
        # the remaining rounds on the same content.
        if len(questions) <= before:
            break
    if len(questions) < count:
        logger.warning(
            f"[ORCH] '{chapter_topic}' — only {len(questions)}/{count} questions after "
            f"top-up (content-limited); storing what we have."
        )

    final = questions[:count]

    # Assets are produced DURING generation now: the model constructs tables from
    # the real numbers (stored as table HTML) and emits figure SPECS (text only).
    # Tables and figure specs are already attached and are CHEAP, so the quality
    # gate below runs on them directly — it can see each question's table/figure
    # content and correctly judge an asset-bearing question as self-contained.

    # Quality passes: recompute numeric model answers, de-ambiguate MCQ options,
    # then the quality gate (DROPS un-renderable / unanswerable / wrong questions
    # — the returned list may shrink; the top-up loops regenerate the shortfall).
    from app.services.answer_verifier import verify_generated_questions
    final = await verify_generated_questions(final)

    # ONLY NOW generate the expensive figure IMAGES — for the questions that
    # PASSED the gate — so we never pay gpt-image-1 for a question that is then
    # dropped. Bounded by ASSET_MAX_PER_CHAPTER (no new unbounded loop). A figure
    # question whose image can't be produced is dropped (no dangling reference).
    try:
        from app.services.question_assets import realize_figure_images
        final = await realize_figure_images(
            final, chapter_num=chapter_num, book_id=book_id,
        )
    except Exception as exc:
        logger.warning(f"[ORCH] figure image realization failed (non-fatal): {exc}")

    logger.info(f"[ORCH] Final — {len(final)} questions for '{chapter_topic}'")
    return final

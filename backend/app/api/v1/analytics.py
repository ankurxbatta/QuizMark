"""
analytics.py  —  Pipeline analytics API.

Provides the instructor with insight into how the hybrid
SLM + RAG + LLM pipeline is performing:

  GET /analytics/pipeline  → route distribution, avg confidence,
                              avg marks per route, flagged rate
  GET /analytics/questions → per-question accuracy metrics
  GET /analytics/confidence-distribution → histogram data (20 bins)
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case
from app.core.database import get_db
from app.models.models import Submission, Question

router = APIRouter()


@router.get("/pipeline")
async def pipeline_stats(db: AsyncSession = Depends(get_db)):
    """
    Summary statistics for the hybrid marking pipeline.
    Shows how answers are distributed across the three routing tiers
    and the average confidence / mark for each.
    """
    result = await db.execute(
        select(
            Submission.marking_route,
            func.count(Submission.id).label("count"),
            func.avg(Submission.auto_confidence).label("avg_confidence"),
            func.avg(Submission.auto_mark).label("avg_mark"),
            func.avg(Submission.slm_keyword_coverage).label("avg_keyword_coverage"),
            func.avg(Submission.slm_semantic_sim).label("avg_semantic_sim"),
            func.sum(case((Submission.is_flagged == True, 1), else_=0)).label("flagged"),
            func.sum(
                case((Submission.override_mark.is_not(None), 1), else_=0)
            ).label("overridden"),
        )
        .where(Submission.is_marked == True)
        .group_by(Submission.marking_route)
    )
    rows = result.all()

    total = sum(r.count for r in rows) or 1
    pipeline = []
    for r in rows:
        pipeline.append({
            "route": r.marking_route or "unknown",
            "count": r.count,
            "pct": round(r.count / total * 100, 1),
            "avg_confidence": round(r.avg_confidence or 0, 3),
            "avg_mark": round(r.avg_mark or 0, 2),
            "avg_keyword_coverage": round(r.avg_keyword_coverage or 0, 3),
            "avg_semantic_sim": round(r.avg_semantic_sim or 0, 3),
            "flagged": r.flagged,
            "overridden": r.overridden,
        })

    # Overall totals
    totals_result = await db.execute(
        select(
            func.count(Submission.id).label("total"),
            func.avg(Submission.auto_confidence).label("avg_conf"),
            func.sum(case((Submission.is_flagged == True, 1), else_=0)).label("flagged"),
            func.sum(
                case((Submission.override_mark.is_not(None), 1), else_=0)
            ).label("overridden"),
        )
        .where(Submission.is_marked == True)
    )
    t = totals_result.one()

    return {
        "total_marked": t.total or 0,
        "overall_avg_confidence": round(t.avg_conf or 0, 3),
        "flagged_rate": round((t.flagged or 0) / (t.total or 1) * 100, 1),
        "override_rate": round((t.overridden or 0) / (t.total or 1) * 100, 1),
        "by_route": pipeline,
    }


@router.get("/questions")
async def question_accuracy(db: AsyncSession = Depends(get_db)):
    """
    Per-question analytics: how many submissions, avg auto-mark,
    avg override delta (where instructor changed the mark),
    and flagged rate — useful for identifying poorly-calibrated questions.
    """
    result = await db.execute(
        select(
            Question.id,
            Question.question_text,
            Question.max_marks,
            Question.topic_tag,
            Question.difficulty,
            func.count(Submission.id).label("submissions"),
            func.avg(Submission.auto_mark).label("avg_auto_mark"),
            func.avg(Submission.auto_confidence).label("avg_confidence"),
            func.sum(
                case((Submission.is_flagged == True, 1), else_=0)
            ).label("flagged_count"),
            func.avg(
                case(
                    (
                        Submission.override_mark.is_not(None),
                        Submission.override_mark - Submission.auto_mark,
                    ),
                    else_=None,
                )
            ).label("avg_override_delta"),
        )
        .join(Submission, Submission.question_id == Question.id, isouter=True)
        .where(Submission.is_marked == True)
        .group_by(Question.id)
        .order_by(func.count(Submission.id).desc())
    )
    rows = result.all()
    return [
        {
            "question_id": str(r.id),
            "question_text": r.question_text[:80] + ("…" if len(r.question_text) > 80 else ""),
            "max_marks": r.max_marks,
            "topic_tag": r.topic_tag,
            "difficulty": r.difficulty,
            "submissions": r.submissions or 0,
            "avg_auto_mark": round(r.avg_auto_mark or 0, 2),
            "avg_confidence": round(r.avg_confidence or 0, 3),
            "flagged_rate": round(
                (r.flagged_count or 0) / (r.submissions or 1) * 100, 1
            ),
            "avg_override_delta": round(r.avg_override_delta or 0, 2),
        }
        for r in rows
    ]


@router.get("/confidence-distribution")
async def confidence_distribution(db: AsyncSession = Depends(get_db)):
    """
    Histogram of auto_confidence values across all marked submissions.
    Returns 20 bins (0.0–0.05, 0.05–0.10, …, 0.95–1.0).
    """
    result = await db.execute(
        select(Submission.auto_confidence)
        .where(
            Submission.is_marked == True,
            Submission.auto_confidence.is_not(None),
        )
    )
    values = [row[0] for row in result.all()]

    bins = [0.0] * 20
    for v in values:
        idx = min(int(v * 20), 19)
        bins[idx] += 1

    return {
        "bins": [
            {
                "range": f"{i * 0.05:.2f}–{(i + 1) * 0.05:.2f}",
                "low": round(i * 0.05, 2),
                "high": round((i + 1) * 0.05, 2),
                "count": int(bins[i]),
            }
            for i in range(20)
        ],
        "total": len(values),
        "thresholds": {
            "confidence_high": 0.85,
            "confidence_mid": 0.55,
        },
    }

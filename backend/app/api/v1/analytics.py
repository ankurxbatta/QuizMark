from fastapi import APIRouter, Depends, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.database import get_db
from app.core.security import require_instructor

router = APIRouter()


@router.get("/pipeline")
async def pipeline_stats(
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    pipeline = [
        {"$match": {"is_marked": True}},
        {"$group": {
            "_id": "$marking_route",
            "count": {"$sum": 1},
            "avg_confidence": {"$avg": "$auto_confidence"},
            "avg_mark": {"$avg": "$auto_mark"},
            "avg_keyword_coverage": {"$avg": "$slm_keyword_coverage"},
            "avg_semantic_sim": {"$avg": "$slm_semantic_sim"},
            "flagged": {"$sum": {"$cond": ["$is_flagged", 1, 0]}},
            "overridden": {"$sum": {"$cond": [{"$ne": ["$override_mark", None]}, 1, 0]}},
        }},
    ]
    rows = await db["submissions"].aggregate(pipeline).to_list(length=20)
    total = sum(r["count"] for r in rows) or 1

    by_route = [
        {
            "route": r["_id"] or "unknown",
            "count": r["count"],
            "pct": round(r["count"] / total * 100, 1),
            "avg_confidence": round(r.get("avg_confidence") or 0, 3),
            "avg_mark": round(r.get("avg_mark") or 0, 2),
            "avg_keyword_coverage": round(r.get("avg_keyword_coverage") or 0, 3),
            "avg_semantic_sim": round(r.get("avg_semantic_sim") or 0, 3),
            "flagged": r["flagged"],
            "overridden": r["overridden"],
        }
        for r in rows
    ]

    totals_pipeline = [
        {"$match": {"is_marked": True}},
        {"$group": {
            "_id": None,
            "total": {"$sum": 1},
            "avg_conf": {"$avg": "$auto_confidence"},
            "flagged": {"$sum": {"$cond": ["$is_flagged", 1, 0]}},
            "overridden": {"$sum": {"$cond": [{"$ne": ["$override_mark", None]}, 1, 0]}},
        }},
    ]
    t_rows = await db["submissions"].aggregate(totals_pipeline).to_list(length=1)
    t = t_rows[0] if t_rows else {"total": 0, "avg_conf": 0, "flagged": 0, "overridden": 0}

    return {
        "total_marked": t.get("total", 0),
        "overall_avg_confidence": round(t.get("avg_conf") or 0, 3),
        "flagged_rate": round((t.get("flagged", 0)) / (t.get("total", 1) or 1) * 100, 1),
        "override_rate": round((t.get("overridden", 0)) / (t.get("total", 1) or 1) * 100, 1),
        "by_route": by_route,
    }


@router.get("/questions")
async def question_accuracy(
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    # Aggregate submissions grouped by question
    sub_pipeline = [
        {"$match": {"is_marked": True}},
        {"$group": {
            "_id": "$question_id",
            "submissions": {"$sum": 1},
            "avg_auto_mark": {"$avg": "$auto_mark"},
            "avg_confidence": {"$avg": "$auto_confidence"},
            "flagged_count": {"$sum": {"$cond": ["$is_flagged", 1, 0]}},
            "override_deltas": {
                "$push": {
                    "$cond": [
                        {"$ne": ["$override_mark", None]},
                        {"$subtract": ["$override_mark", "$auto_mark"]},
                        "$$REMOVE",
                    ]
                }
            },
        }},
    ]
    sub_rows = await db["submissions"].aggregate(sub_pipeline).to_list(length=5000)
    sub_map = {r["_id"]: r for r in sub_rows}

    questions = await db["questions"].find({}, {"embedding": 0}).to_list(length=5000)
    result = []
    for q in questions:
        qid = q["_id"]
        s = sub_map.get(qid, {})
        deltas = s.get("override_deltas") or []
        avg_delta = sum(deltas) / len(deltas) if deltas else 0.0
        result.append({
            "question_id": qid,
            "question_text": q["question_text"][:80] + ("…" if len(q["question_text"]) > 80 else ""),
            "max_marks": q["max_marks"],
            "topic_tag": q.get("topic_tag"),
            "difficulty": q.get("difficulty"),
            "submissions": s.get("submissions", 0),
            "avg_auto_mark": round(s.get("avg_auto_mark") or 0, 2),
            "avg_confidence": round(s.get("avg_confidence") or 0, 3),
            "flagged_rate": round((s.get("flagged_count", 0)) / (s.get("submissions", 1) or 1) * 100, 1),
            "avg_override_delta": round(avg_delta, 2),
        })
    result.sort(key=lambda x: x["submissions"], reverse=True)
    return result


@router.get("/confidence-distribution")
async def confidence_distribution(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncIOMotorDatabase = Depends(get_db),
    _: dict = Depends(require_instructor),
):
    docs = await db["submissions"].find(
        {"is_marked": True, "auto_confidence": {"$ne": None}},
        {"auto_confidence": 1},
    ).skip(skip).limit(limit).to_list(length=limit)
    values = [d["auto_confidence"] for d in docs if d.get("auto_confidence") is not None]

    bins = [0] * 20
    for v in values:
        bins[min(int(v * 20), 19)] += 1

    return {
        "bins": [
            {"range": f"{i * 0.05:.2f}–{(i + 1) * 0.05:.2f}",
             "low": round(i * 0.05, 2),
             "high": round((i + 1) * 0.05, 2),
             "count": int(bins[i])}
            for i in range(20)
        ],
        "total": len(values),
        "thresholds": {"confidence_high": 0.85, "confidence_mid": 0.55},
    }

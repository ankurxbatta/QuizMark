#!/usr/bin/env python3
"""
export_ingestion_metrics.py — Dump metadata-only ingestion metrics from the
running MongoDB (db: marking_tools) into report/data/*.json for the
ingestion-accuracy notebook.

Run from project root:
    backend/.venv-test/bin/python scripts/export_ingestion_metrics.py [BOOK_ID]

Design notes
------------
* Metadata-ONLY: embedding arrays are never exported, only a boolean for
  presence and an integer length.  Keeps dumps small.
* Two transports are supported transparently:
    1. pymongo (preferred — if importable and a server is reachable).
    2. `docker compose exec -T mongodb mongosh` fallback (used automatically
       when pymongo is unavailable or cannot connect).
* The script is defensive about empty collections so it produces valid JSON
  even while ingestion is still in progress.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_OUT_DIR = _REPO_ROOT / "report" / "data"
_ENV_FILE = _REPO_ROOT / ".env"

DEFAULT_BOOK_ID = "Test_ch4-5"
DB_NAME = "marking_tools"

# Chapters the target book (OpenStax statistics ch4-5) is expected to contain.
EXPECTED_CHAPTERS = [4, 5]


# ── env helpers ───────────────────────────────────────────────────────────────
def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


_ENV = _load_env()


# ── transport: pymongo ────────────────────────────────────────────────────────
def _try_pymongo():
    try:
        from pymongo import MongoClient
    except ImportError:
        return None
    url = os.environ.get("MONGODB_URL", _ENV.get("MONGODB_URL", "mongodb://localhost:27017"))
    # Inside docker the host is "mongodb"; from the host machine use localhost.
    url = url.replace("mongodb://mongodb:", "mongodb://localhost:")
    try:
        client = MongoClient(url, serverSelectionTimeoutMS=4000, directConnection=True)
        client.admin.command("ping")
        return client[DB_NAME]
    except Exception as exc:  # noqa: BLE001
        print(f"[export] pymongo connect failed ({exc}); falling back to mongosh", file=sys.stderr)
        return None


# ── transport: mongosh via docker ─────────────────────────────────────────────
def _mongosh(js: str) -> object:
    """Run a mongosh snippet that prints a single JSON blob; return parsed obj."""
    cmd = [
        "docker", "compose", "exec", "-T", "mongodb",
        "mongosh", DB_NAME, "--quiet", "--eval",
        "print(JSON.stringify((function(){" + js + "})()))",
    ]
    out = subprocess.run(cmd, cwd=_REPO_ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"mongosh failed: {out.stderr.strip()}")
    txt = out.stdout.strip()
    # mongosh may emit warnings before the JSON; grab the last JSON line.
    for line in reversed(txt.splitlines()):
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            return json.loads(line)
    raise RuntimeError(f"no JSON in mongosh output: {txt[:300]}")


# ── metric extraction (works on python dicts regardless of transport) ──────────
def _chunk_metric(d: dict) -> dict:
    emb = d.get("embedding")
    return {
        "chapter_num": d.get("chapter_num"),
        "chapter_title": d.get("chapter_title"),
        "section_title": d.get("section_title"),
        "page_start": d.get("page_start"),
        "page_end": d.get("page_end"),
        "has_tables": bool(d.get("has_tables")),
        "has_images": bool(d.get("has_images")),
        "has_math": bool(d.get("has_math")),
        "has_formula": bool(d.get("has_formula")),
        "has_example": bool(d.get("has_example")),
        "text_len": len(d.get("text") or ""),
        "math_text_len": len(d.get("math_text") or ""),
        "table_texts_count": len(d.get("table_texts") or []),
        "image_texts_count": len(d.get("image_texts") or []),
        "key_terms_count": len(d.get("key_terms") or []),
        "teaching_density": d.get("teaching_density"),
        "embedding_present": bool(emb),
        "embedding_dim": len(emb) if emb else 0,
    }


def _exercise_metric(d: dict) -> dict:
    return {
        "exercise_kind": d.get("exercise_kind"),
        "inferred_qtype": d.get("inferred_qtype"),
        "chapter_num": d.get("chapter_num"),
        "source_label": d.get("source_label"),
        "has_math_text": bool((d.get("math_text") or "").strip()),
        "has_table_markdown": bool((d.get("table_markdown") or "").strip()),
        "has_figure_desc": bool((d.get("figure_desc") or "").strip()),
        "options_count": len(d.get("options") or []),
        "has_solution": bool((d.get("solution") or "").strip()),
    }


def _question_metric(d: dict) -> dict:
    return {
        "difficulty": d.get("difficulty"),
        "bloom_level": d.get("bloom_level"),
        "chapter_num": d.get("chapter_num"),
        "qtype": d.get("qtype") or d.get("question_type"),
        "book_id": d.get("book_id"),
    }


# ── pymongo path ──────────────────────────────────────────────────────────────
def _export_pymongo(db, book_id: str) -> dict:
    chunk_proj = {
        "chapter_num": 1, "chapter_title": 1, "section_title": 1,
        "page_start": 1, "page_end": 1, "has_tables": 1, "has_images": 1,
        "has_math": 1, "has_formula": 1, "has_example": 1, "text": 1,
        "math_text": 1, "table_texts": 1, "image_texts": 1, "key_terms": 1,
        "teaching_density": 1, "embedding": 1,
    }
    q = {"book_id": book_id}
    chunks = [_chunk_metric(d) for d in db["pdf_chunks"].find(q, chunk_proj)]
    if not chunks:  # maybe seeded without that exact book_id; take all
        chunks = [_chunk_metric(d) for d in db["pdf_chunks"].find({}, chunk_proj)]

    ex_proj = {
        "exercise_kind": 1, "inferred_qtype": 1, "chapter_num": 1,
        "source_label": 1, "math_text": 1, "table_markdown": 1,
        "figure_desc": 1, "options": 1, "solution": 1,
    }
    exercises = [_exercise_metric(d) for d in db["book_exercises"].find({}, ex_proj)]

    qn_proj = {
        "difficulty": 1, "bloom_level": 1, "chapter_num": 1,
        "qtype": 1, "question_type": 1, "book_id": 1,
    }
    questions = [_question_metric(d) for d in db["questions"].find({}, qn_proj)]

    counts = {
        "pdf_chunks": db["pdf_chunks"].count_documents({}),
        "book_exercises": db["book_exercises"].count_documents({}),
        "questions": db["questions"].count_documents({}),
        "math_index": db["math_index"].count_documents({}),
        "table_index": db["table_index"].count_documents({}),
        "figure_index": db["figure_index"].count_documents({}),
    }
    return {"chunks": chunks, "exercises": exercises, "questions": questions, "counts": counts}


# ── mongosh path ──────────────────────────────────────────────────────────────
_MONGOSH_JS = r"""
function chunkMetric(d){
  var emb = d.embedding;
  return {
    chapter_num: d.chapter_num, chapter_title: d.chapter_title,
    section_title: d.section_title, page_start: d.page_start,
    page_end: d.page_end, has_tables: !!d.has_tables,
    has_images: !!d.has_images, has_math: !!d.has_math,
    has_formula: !!d.has_formula, has_example: !!d.has_example,
    text_len: (d.text||"").length, math_text_len: (d.math_text||"").length,
    table_texts_count: (d.table_texts||[]).length,
    image_texts_count: (d.image_texts||[]).length,
    key_terms_count: (d.key_terms||[]).length,
    teaching_density: d.teaching_density,
    embedding_present: !!(emb && emb.length),
    embedding_dim: emb ? emb.length : 0
  };
}
function exMetric(d){
  return {
    exercise_kind: d.exercise_kind, inferred_qtype: d.inferred_qtype,
    chapter_num: d.chapter_num, source_label: d.source_label,
    has_math_text: !!((d.math_text||"").trim()),
    has_table_markdown: !!((d.table_markdown||"").trim()),
    has_figure_desc: !!((d.figure_desc||"").trim()),
    options_count: (d.options||[]).length,
    has_solution: !!((d.solution||"").trim())
  };
}
function qnMetric(d){
  return { difficulty: d.difficulty, bloom_level: d.bloom_level,
           chapter_num: d.chapter_num, qtype: d.qtype || d.question_type,
           book_id: d.book_id };
}
var bookId = "__BOOK_ID__";
var chunks = db.pdf_chunks.find({book_id: bookId}).toArray().map(chunkMetric);
if (chunks.length === 0) chunks = db.pdf_chunks.find({}).toArray().map(chunkMetric);
var exercises = db.book_exercises.find({}).toArray().map(exMetric);
var questions = db.questions.find({}).toArray().map(qnMetric);
var counts = {
  pdf_chunks: db.pdf_chunks.countDocuments({}),
  book_exercises: db.book_exercises.countDocuments({}),
  questions: db.questions.countDocuments({}),
  math_index: db.math_index.countDocuments({}),
  table_index: db.table_index.countDocuments({}),
  figure_index: db.figure_index.countDocuments({})
};
return {chunks: chunks, exercises: exercises, questions: questions, counts: counts};
"""


def _export_mongosh(book_id: str) -> dict:
    js = _MONGOSH_JS.replace("__BOOK_ID__", book_id)
    return _mongosh(js)


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    book_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BOOK_ID
    _OUT_DIR.mkdir(parents=True, exist_ok=True)

    db = _try_pymongo()
    if db is not None:
        print("[export] using pymongo transport")
        data = _export_pymongo(db, book_id)
    else:
        print("[export] using mongosh (docker) transport")
        data = _export_mongosh(book_id)

    meta = {
        "book_id": book_id,
        "db_name": DB_NAME,
        "expected_chapters": EXPECTED_CHAPTERS,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "transport": "pymongo" if db is not None else "mongosh",
        "counts": data["counts"],
        "n_chunks_exported": len(data["chunks"]),
        "n_exercises_exported": len(data["exercises"]),
        "n_questions_exported": len(data["questions"]),
    }

    writes = {
        "pdf_chunks.json": data["chunks"],
        "book_exercises.json": data["exercises"],
        "questions.json": data["questions"],
        "index_counts.json": data["counts"],
        "_meta.json": meta,
    }
    for name, payload in writes.items():
        path = _OUT_DIR / name
        path.write_text(json.dumps(payload, indent=2, default=str))
        print(f"[export] wrote {path.relative_to(_REPO_ROOT)}  "
              f"({len(payload) if isinstance(payload, list) else len(payload)} items)")

    print("\n[export] summary:")
    print(json.dumps(meta, indent=2, default=str))


if __name__ == "__main__":
    main()

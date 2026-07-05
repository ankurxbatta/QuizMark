"""DeepSearch refiner: merge safety, fail-open behavior, and gating."""
import json

import pytest

from app.core.config import settings
from app.services import deepsearch
from app.services.deepsearch import _merge_repair, _stem, refine_questions


def _q(**overrides) -> dict:
    base = {
        "question_text": "What is the mean of 2, 4 and 6?",
        "question_type": "short_answer",
        "difficulty": "easy",
        "model_answer": "The mean is 4.",
        "rubric": "1 mark: states 4.",
        "max_marks": 1.0,
        "book_id": "book-1",
        "chapter_num": 3,
        "embedding": [0.1, 0.2],
    }
    base.update(overrides)
    return base


# ── _merge_repair ─────────────────────────────────────────────────────────────

def test_merge_applies_only_editable_fields():
    original = _q()
    repaired = {
        "question_text": "What is the mean of 2, 4 and 9?",
        "model_answer": "The mean is 5.",
        "book_id": "EVIL",           # not editable
        "question_type": "mcq",      # not editable
        "embedding": [9.9],          # not editable
    }
    merged = _merge_repair(original, repaired)
    assert merged["question_text"] == "What is the mean of 2, 4 and 9?"
    assert merged["model_answer"] == "The mean is 5."
    assert merged["book_id"] == "book-1"
    assert merged["question_type"] == "short_answer"
    assert merged["embedding"] == [0.1, 0.2]


def test_merge_rejects_empty_question_text():
    original = _q()
    assert _merge_repair(original, {"question_text": "  "}) is original
    assert _merge_repair(original, {"model_answer": "x"}) is original  # no text at all


def test_merge_validates_field_types():
    original = _q()
    merged = _merge_repair(original, {
        "question_text": "ok?",
        "max_marks": "three",          # wrong type → keep original value
        "assets": "not-a-list",        # wrong type → keep original (absent)
        "rubric": ["not", "a", "str"],  # wrong type → keep original value
    })
    assert merged["max_marks"] == 1.0
    assert "assets" not in merged
    assert merged["rubric"] == "1 mark: states 4."


def test_merge_accepts_valid_assets_and_marks():
    original = _q()
    merged = _merge_repair(original, {
        "question_text": "ok?",
        "max_marks": 3,
        "assets": [{"type": "table", "html": "<table></table>"}],
    })
    assert merged["max_marks"] == 3.0
    assert merged["assets"] == [{"type": "table", "html": "<table></table>"}]


# ── _stem ─────────────────────────────────────────────────────────────────────

def test_stem_strips_mcq_options():
    q = _q(question_text="Which is the median?\nA. 1\nB. 2\nC. 3\nD. 4")
    assert _stem(q) == "Which is the median?"


# ── refine_questions ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refine_disabled_returns_input(monkeypatch):
    monkeypatch.setattr(settings, "DEEPSEARCH_REFINE_ENABLED", False)
    qs = [_q()]
    assert await refine_questions(qs) is qs


@pytest.mark.asyncio
async def test_refine_fail_open_keeps_originals(monkeypatch):
    monkeypatch.setattr(settings, "DEEPSEARCH_REFINE_ENABLED", True)

    async def boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(deepsearch, "_rag_evidence", boom)
    monkeypatch.setattr(deepsearch, "web_search", boom)
    monkeypatch.setattr(deepsearch.generation_service, "generate", boom)

    qs = [_q(), _q(question_text="Second?")]
    out = await refine_questions(qs, book_id="book-1", chapter_num=3)
    assert out == qs  # same length, originals untouched


@pytest.mark.asyncio
async def test_refine_applies_llm_repair(monkeypatch):
    monkeypatch.setattr(settings, "DEEPSEARCH_REFINE_ENABLED", True)

    async def fake_evidence(*a, **k):
        return "[TEXTBOOK 1: Ch3]\nThe mean of 2, 4, 6 is 4."

    async def no_web(*a, **k):
        return []

    async def fake_generate(prompt):
        assert "AUTO-REJECTION CHECK" in prompt  # validator rules are injected
        return json.dumps({
            "verdict": "repaired",
            "changes": "fixed the model answer",
            "question": {
                "question_text": "What is the mean of 2, 4 and 6?",
                "model_answer": "The mean is (2+4+6)/3 = 4.",
            },
        })

    monkeypatch.setattr(deepsearch, "_rag_evidence", fake_evidence)
    monkeypatch.setattr(deepsearch, "web_search", no_web)
    monkeypatch.setattr(deepsearch.generation_service, "generate", fake_generate)

    out = await refine_questions([_q()], book_id="book-1", chapter_num=3)
    assert len(out) == 1
    assert out[0]["model_answer"] == "The mean is (2+4+6)/3 = 4."
    assert out[0]["book_id"] == "book-1"


@pytest.mark.asyncio
async def test_refine_verdict_ok_keeps_original(monkeypatch):
    monkeypatch.setattr(settings, "DEEPSEARCH_REFINE_ENABLED", True)

    async def fake_evidence(*a, **k):
        return ""

    async def no_web(*a, **k):
        return []

    async def fake_generate(prompt):
        return '{"verdict": "ok"}'

    monkeypatch.setattr(deepsearch, "_rag_evidence", fake_evidence)
    monkeypatch.setattr(deepsearch, "web_search", no_web)
    monkeypatch.setattr(deepsearch.generation_service, "generate", fake_generate)

    q = _q()
    out = await refine_questions([q])
    assert out[0] is q


@pytest.mark.asyncio
async def test_web_search_inert_without_any_key(monkeypatch):
    monkeypatch.setattr(settings, "DEEPSEARCH_WEB_ENABLED", True)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    monkeypatch.setattr(settings, "TAVILY_API_KEY", None, raising=False)
    assert await deepsearch.web_search("mean of a sample") == []


@pytest.mark.asyncio
async def test_web_search_prefers_openai_when_key_present(monkeypatch):
    monkeypatch.setattr(settings, "DEEPSEARCH_WEB_ENABLED", True)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")

    async def fake_openai(query):
        return [{"title": "Web search (OpenAI)", "content": "the mean is the average", "url": "https://x"}]

    async def fail_tavily(*a, **k):
        raise AssertionError("tavily must not be called when OpenAI succeeds")

    monkeypatch.setattr(deepsearch, "_openai_web_search", fake_openai)
    monkeypatch.setattr(deepsearch, "_tavily_search", fail_tavily)
    out = await deepsearch.web_search("mean")
    assert out[0]["title"] == "Web search (OpenAI)"


@pytest.mark.asyncio
async def test_web_search_falls_back_to_tavily(monkeypatch):
    monkeypatch.setattr(settings, "DEEPSEARCH_WEB_ENABLED", True)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")

    async def empty_openai(query):
        return []

    async def fake_tavily(query, max_results=None):
        return [{"title": "T", "content": "c", "url": "u"}]

    monkeypatch.setattr(deepsearch, "_openai_web_search", empty_openai)
    monkeypatch.setattr(deepsearch, "_tavily_search", fake_tavily)
    out = await deepsearch.web_search("mean")
    assert out[0]["title"] == "T"

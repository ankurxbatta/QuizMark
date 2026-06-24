"""Regression tests for the marker's JSON parser (rag_pipeline._parse_llm_json).

A correct student answer must never collapse to 0 marks just because the marking
LLM wrapped its score in slightly-malformed JSON — LaTeX backslashes, trailing
prose, or stray control characters.
"""
from app.services.rag_pipeline import _parse_llm_json


def test_clean_json():
    r = _parse_llm_json('{"mark": 4, "feedback": "Perfect", "flagged": false, "confidence": 0.9}', 4.0)
    assert r["mark"] == 4.0 and r["flagged"] is False


def test_latex_backslashes_in_feedback():
    # The Q3 live-failure mode: feedback contains LaTeX with INVALID JSON escapes
    # (\( and \sigma), which made the old json.loads raise → 0 marks + flag.
    raw = r'{"mark": 2, "feedback": "Correct: use \(\sigma\) here; result is 1/3.", "flagged": false, "confidence": 0.8}'
    r = _parse_llm_json(raw, 2.0)
    assert r["mark"] == 2.0
    assert "sigma" in r["feedback"]
    assert r["flagged"] is False


def test_trailing_prose_after_object():
    raw = '{"mark": 1.5, "feedback": "Partly right", "flagged": false, "confidence": 0.6}\n\nHope this helps!'
    r = _parse_llm_json(raw, 3.0)
    assert r["mark"] == 1.5


def test_control_chars():
    raw = '{"mark": 3, "feedback": "Good\twork", "flagged": true, "confidence": 0.5}'
    r = _parse_llm_json(raw, 3.0)
    assert r["mark"] == 3.0 and r["flagged"] is True


def test_mark_capped_at_max():
    r = _parse_llm_json('{"mark": 9, "feedback": "x", "flagged": false, "confidence": 1}', 4.0)
    assert r["mark"] == 4.0


def test_regex_fallback_recovers_mark():
    # Badly broken JSON (unterminated), but the mark is recoverable → must not zero.
    raw = '{"mark": 2.5, "feedback": "ok \\ broken latex \\q here, "flagged": false'
    r = _parse_llm_json(raw, 3.0)
    assert r["mark"] == 2.5

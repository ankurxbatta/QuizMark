from app.services.math_format import _sanity_ok, _strip_fences, needs_latexify
from app.services.question_generator import unmangle_latex


# ── needs_latexify ──────────────────────────────────────────────────────────────

def test_needs_latexify_detects_bare_math():
    assert needs_latexify("Compute P(x) = mu^x e^{-mu} / x!")
    assert needs_latexify("The variance is sigma^2 for the sample.")


def test_needs_latexify_skips_already_delimited():
    assert not needs_latexify(r"Compute $P(x) = \mu^x$ for the sample.")


def test_needs_latexify_skips_plain_prose():
    assert not needs_latexify("Define a confidence interval in your own words.")
    assert not needs_latexify("")
    assert not needs_latexify("   ")


# ── unmangle_latex (JSON-escape corruption repair) ──────────────────────────────

def test_unmangle_restores_backspace_to_backslash_b():
    # json.loads turns a single-escaped \b into a backspace char, eating the "b".
    assert unmangle_latex("\x08inom{n}{k}") == r"\binom{n}{k}"


def test_unmangle_restores_formfeed_to_backslash_f():
    assert unmangle_latex("\x0crac{a}{b}") == r"\frac{a}{b}"


def test_unmangle_recurses_into_nested_structures():
    data = {"opts": ["\x08inom{n}{k}", "ok"], "n": 3}
    assert unmangle_latex(data) == {"opts": [r"\binom{n}{k}", "ok"], "n": 3}


# ── sanity check on rewrites ────────────────────────────────────────────────────

def test_sanity_rejects_rewrite_without_delimiters():
    assert not _sanity_ok("P(x) = mu^x", "P(x) = mu^x")


def test_sanity_rejects_truncated_rewrite():
    assert not _sanity_ok("a" * 100, "$x$")


def test_sanity_accepts_reasonable_rewrite():
    assert _sanity_ok("P(x) = mu^x", r"$P(x) = \mu^x$")


def test_strip_fences_removes_code_fence():
    assert _strip_fences("```latex\n$x^2$\n```") == "$x^2$"


# ── admin backfill endpoint RBAC ────────────────────────────────────────────────

def test_latexify_backfill_requires_auth(client):
    resp = client.post("/api/v1/admin/questions/latexify")
    assert resp.status_code == 401


def test_latexify_backfill_forbidden_for_students(client, token_factory):
    resp = client.post(
        "/api/v1/admin/questions/latexify",
        headers={"Authorization": f"Bearer {token_factory('student')}"},
    )
    assert resp.status_code == 403


def test_latexify_backfill_allowed_for_instructors(client, token_factory, monkeypatch):
    import app.services.math_format as mf

    async def fake_backfill(book_id=None):
        return {"scanned": 5, "needed": 2, "updated": 2}

    monkeypatch.setattr(mf, "backfill_stored_questions", fake_backfill)
    resp = client.post(
        "/api/v1/admin/questions/latexify",
        headers={"Authorization": f"Bearer {token_factory('instructor')}"},
    )
    assert resp.status_code == 200
    assert resp.json()["updated"] == 2

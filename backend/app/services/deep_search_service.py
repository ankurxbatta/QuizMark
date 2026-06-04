"""
deep_search_service.py — Multi-query RAG with LLM synthesis (DeepSearch).

Inspired by Shiksha Copilot's EduChat architecture:
  1. Decompose user query into N diverse sub-queries (query decomposition)
  2. Embed each sub-query and run parallel vector searches
  3. Deduplicate and merge retrieved textbook chunks
  4. Optionally augment with Tavily web search (if TAVILY_API_KEY is set)
  5. Synthesize a grounded answer with source citations

This gives accurate, curriculum-aligned answers because the LLM is forced
to reason over retrieved textbook content rather than its parametric knowledge.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Optional

from app.services.llm_service import generation_service, slm_service
from app.services.mongo_vector_store import vector_search


# ── Prompts ────────────────────────────────────────────────────────────────────

_DECOMPOSE_PROMPT = """\
You are a search query optimizer for an educational statistics textbook retrieval system.

User question: {query}

Generate {n} diverse, specific search queries that together cover ALL aspects of this question.
Target different facets: definitions, formulas, conditions, worked examples, interpretations.
Each query should be short (5–15 words) and semantically distinct from the others.

Output ONLY a JSON array of strings. No preamble, no explanation.
Example: ["query about definition", "query about formula", "query about application"]
"""

_SYNTHESIS_PROMPT = """\
You are an expert statistics tutor answering a student or instructor question.
Base your answer STRICTLY on the textbook excerpts provided below.

Question: {query}

Textbook context:
{context}

Instructions:
- Answer concisely and accurately, drawing only from the context above.
- If the context contains relevant formulas, data, or worked examples, include them.
- Cite each source inline as [Source N] where N matches the source number.
- If the context is genuinely insufficient to answer, say so — do NOT fabricate.
- Write in clear, educational prose suitable for a university statistics course.
"""


# ── Query decomposition ────────────────────────────────────────────────────────

async def _decompose_query(query: str, n: int = 3) -> list[str]:
    """Use LLM to generate n diverse retrieval sub-queries from the user's question."""
    prompt = _DECOMPOSE_PROMPT.format(query=query, n=n)
    try:
        raw = await generation_service.generate(prompt)
        raw = re.sub(r"```(?:json)?", "", raw).strip()
        match = re.search(r"\[.*?\]", raw, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            queries = [str(q).strip() for q in parsed if q and str(q).strip()]
            if queries:
                return queries[:n]
    except Exception:
        pass
    # Fallback: return original query only
    return [query]


# ── Optional web search (Tavily) ───────────────────────────────────────────────

async def _web_search(query: str) -> list[dict]:
    """Tavily web search — only runs when TAVILY_API_KEY is configured."""
    try:
        from app.core.config import settings
        key = getattr(settings, "TAVILY_API_KEY", None)
        if not key:
            return []
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": key,
                    "query": query,
                    "max_results": 3,
                    "search_depth": "basic",
                    "include_answer": True,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        results: list[dict] = []
        if data.get("answer"):
            results.append({
                "text": data["answer"],
                "title": "Web Search Summary",
                "url": "",
                "source_type": "web",
            })
        for r in data.get("results", [])[:2]:
            results.append({
                "text": r.get("content", ""),
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "source_type": "web",
            })
        return results
    except Exception:
        return []


# ── Public API ─────────────────────────────────────────────────────────────────

async def deep_search(
    query: str,
    book_id: Optional[str] = None,
    k_per_query: int = 3,
    include_web: bool = False,
) -> dict:
    """
    Deep multi-query search over the ingested textbook library.

    Args:
        query:         The user's natural-language question.
        book_id:       Optional — restrict search to a specific ingested book.
        k_per_query:   Textbook chunks retrieved per sub-query (default 3).
        include_web:   Augment with Tavily web results (requires TAVILY_API_KEY).

    Returns:
        {
          "answer":      synthesized answer string,
          "sources":     list of source dicts (chapter, section, pages, text excerpt),
          "sub_queries": list of sub-queries used for retrieval,
        }
    """
    # Step 1 — decompose the question into sub-queries
    sub_queries = await _decompose_query(query, n=3)

    # Step 2 — embed all sub-queries in parallel
    embeddings = await asyncio.gather(*[slm_service.embed(q) for q in sub_queries])

    # Step 3 — parallel vector searches over the textbook library
    search_tasks = [
        vector_search(emb, k=k_per_query, book_id=book_id)
        for emb in embeddings
    ]
    raw_batches = await asyncio.gather(*search_tasks)

    # Step 4 — deduplicate chunks by _id (preserve first-encountered order)
    seen_ids: set[str] = set()
    chunks: list[dict] = []
    for batch in raw_batches:
        for chunk in batch:
            cid = chunk.get("_id", "")
            if cid not in seen_ids:
                seen_ids.add(cid)
                chunks.append(chunk)

    # Step 5 — optional web search on the original query
    web_results: list[dict] = []
    if include_web:
        web_results = await _web_search(query)

    if not chunks and not web_results:
        return {
            "answer": (
                "No relevant content found in the textbook library. "
                "Please ensure books are ingested via the Library tab first."
            ),
            "sources": [],
            "sub_queries": sub_queries,
        }

    # Step 6 — assemble context string and source list
    context_parts: list[str] = []
    sources: list[dict] = []

    for i, chunk in enumerate(chunks, 1):
        chapter = chunk.get("chapter_title", "")
        section = chunk.get("section_title", "")
        pages = f"pp.{chunk.get('page_start', '')}–{chunk.get('page_end', '')}"
        text = chunk.get("text", "")
        if chunk.get("table_texts"):
            text += "\n" + "\n".join(chunk["table_texts"])
        if chunk.get("math_text"):
            text += f"\n[Formulas] {chunk['math_text']}"
        if chunk.get("image_texts"):
            text += "\n[Visual] " + " ".join(chunk["image_texts"])

        context_parts.append(
            f"[Source {i}: {chapter} — {section} | {pages}]\n{text[:600]}"
        )
        sources.append({
            "text": text[:400],
            "chapter": chapter,
            "section": section,
            "pages": pages,
            "book_id": chunk.get("book_id", ""),
            "source_type": "textbook",
        })

    for j, wr in enumerate(web_results, len(chunks) + 1):
        context_parts.append(
            f"[Source {j}: {wr.get('title', 'Web')}]\n{wr['text'][:400]}"
        )
        sources.append({
            "text": wr["text"][:400],
            "chapter": "",
            "section": wr.get("title", ""),
            "pages": "",
            "url": wr.get("url", ""),
            "source_type": "web",
        })

    context = "\n\n".join(context_parts)

    # Step 7 — synthesize a grounded answer
    synthesis_prompt = _SYNTHESIS_PROMPT.format(query=query, context=context)
    try:
        answer = await generation_service.generate(synthesis_prompt)
        answer = answer.strip()
    except Exception as exc:
        answer = f"Unable to synthesize answer: {exc}"

    return {
        "answer": answer,
        "sources": sources,
        "sub_queries": sub_queries,
    }

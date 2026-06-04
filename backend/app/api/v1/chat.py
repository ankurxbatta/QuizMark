"""
chat.py — DeepSearch endpoint.

POST /api/v1/chat/search
  Runs multi-query RAG over ingested textbooks and returns a synthesized,
  citation-backed answer. Optionally augments with Tavily web search.
"""
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.security import get_current_user
from app.services.deep_search_service import deep_search

router = APIRouter()


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=3, max_length=1000, description="Natural-language question to search")
    book_id: Optional[str] = Field(None, description="Restrict search to a specific ingested book (omit for all books)")
    include_web: bool = Field(False, description="Also run a Tavily web search (requires TAVILY_API_KEY in .env)")
    k_per_query: int = Field(3, ge=1, le=8, description="Textbook chunks retrieved per sub-query")


class SourceOut(BaseModel):
    text: str
    chapter: str
    section: str
    pages: str
    book_id: str = ""
    url: str = ""
    source_type: str  # "textbook" | "web"


class SearchResponse(BaseModel):
    answer: str
    sources: list[SourceOut]
    sub_queries: list[str]


@router.post("/search", response_model=SearchResponse)
async def deep_search_endpoint(
    payload: SearchRequest,
    _: dict = Depends(get_current_user),
):
    """
    Deep multi-query search over ingested textbook content with LLM synthesis.

    The query is automatically decomposed into sub-queries that cover different
    facets of the question (definitions, formulas, examples, conditions).
    All sub-queries are run in parallel against the textbook vector store,
    results are deduplicated, and an LLM synthesizes a grounded answer with
    inline source citations.
    """
    result = await deep_search(
        query=payload.query,
        book_id=payload.book_id,
        k_per_query=payload.k_per_query,
        include_web=payload.include_web,
    )
    return result

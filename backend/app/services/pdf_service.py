"""
PDF text extraction service.
Handles text-based PDFs using pdfplumber, with pypdf fallback.
"""
import io
import pdfplumber
from pypdf import PdfReader


def extract_text_from_pdf(file_bytes: bytes, max_pages: int = 100) -> str:
    """
    Extract text from a PDF given raw bytes.
    Uses pdfplumber for accurate layout-aware extraction.
    Falls back to pypdf if pdfplumber fails.
    Returns the extracted text as a single string.
    """
    try:
        text_parts = []
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            pages_to_read = min(len(pdf.pages), max_pages)
            for i, page in enumerate(pdf.pages[:pages_to_read]):
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(f"--- Page {i + 1} ---\n{page_text}")
        if text_parts:
            return "\n\n".join(text_parts)
    except Exception:
        pass  # fall through to pypdf

    # Fallback: pypdf
    reader = PdfReader(io.BytesIO(file_bytes))
    pages_to_read = min(len(reader.pages), max_pages)
    return "\n\n".join(
        page.extract_text() or ""
        for page in reader.pages[:pages_to_read]
    )


def get_pdf_info(file_bytes: bytes) -> dict:
    """Return basic metadata about a PDF."""
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        meta = reader.metadata or {}
        return {
            "pages": len(reader.pages),
            "title": meta.get("/Title", ""),
            "author": meta.get("/Author", ""),
        }
    except Exception as e:
        return {"pages": 0, "title": "", "author": "", "error": str(e)}

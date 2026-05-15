"""kaos-pdf: PDF extraction and document processing for KAOS.

Uses pypdfium2 (Apache 2.0) for PDF parsing, producing kaos-content
ContentDocument AST with provenance (page numbers, bounding boxes).

Canonical entry points (per ``docs/guides/python-api-naming.md``):

- :func:`parse_pdf` — file/path → :class:`~kaos_content.ContentDocument`
- :func:`parse_pdf_bytes` — in-memory bytes → :class:`~kaos_content.ContentDocument`
- :func:`extract_pdf_with_tables` — multi-output pipeline (AST + ``TabularDocument``)

The legacy ``extract_pdf`` / ``extract_pdf_bytes`` names continue to import and
forward to the canonical functions but emit ``DeprecationWarning``.
"""

import warnings as _warnings
from functools import wraps as _wraps
from typing import Any as _Any

# Re-export canonical search from kaos-content
from kaos_content.search import SearchResult, SearchResults, search_document

from kaos_pdf._version import __version__
from kaos_pdf.errors import KaosPdfError, PdfExtractionError, PdfNotFoundError, PdfRenderError
from kaos_pdf.extract import (
    OCRMode,
    TableMode,
    classify_document,
    classify_page,
    extract_page_text,
    extract_pdf_with_tables,
    get_page_count,
    get_pdf_metadata,
    get_pdf_outline,
    parse_pdf,
    parse_pdf_bytes,
    render_page,
)
from kaos_pdf.model import PdfMetadata, PdfOutlineEntry
from kaos_pdf.ocr import (
    OCREngine,
    OCRLine,
    OCRResult,
    TesseractEngine,
    TesseractNotInstalledError,
)
from kaos_pdf.ocr import get_default_engine as get_default_ocr_engine
from kaos_pdf.tables import (
    ExtractedTable,
    TableEngine,
    TableResult,
)
from kaos_pdf.tables import get_default_engine as get_default_table_engine
from kaos_pdf.tools import (
    ClassifyPageTool,
    GetOutlineTool,
    GetPageTextTool,
    ParsePDFTool,
    PDFMetadataTool,
    RenderPageTool,
    SearchDocumentTool,
    register_pdf_authoring_tools,
    register_pdf_documents_tools,
    register_pdf_tools,
)


def _deprecated_alias(new_func: _Any, *, old_name: str, new_name: str, since: str) -> _Any:
    """Wrap ``new_func`` so calls via the legacy name emit ``DeprecationWarning``.

    Used to keep historical ``extract_pdf`` / ``extract_pdf_bytes`` import
    sites working after the PA3 rename to ``parse_pdf`` / ``parse_pdf_bytes``.
    See ``docs/guides/python-api-naming.md`` for the cross-module rule.
    """

    @_wraps(new_func)
    def _wrapper(*args: _Any, **kwargs: _Any) -> _Any:
        _warnings.warn(
            f"kaos_pdf.{old_name}() is deprecated since {since}; "
            f"use kaos_pdf.{new_name}() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return new_func(*args, **kwargs)

    _wrapper.__name__ = old_name
    _wrapper.__qualname__ = old_name
    return _wrapper


# Deprecated aliases: forward to the canonical ``parse_*`` names and warn.
# Kept in ``__all__`` for the deprecation window; remove in a future minor.
extract_pdf = _deprecated_alias(
    parse_pdf, old_name="extract_pdf", new_name="parse_pdf", since="0.1.0a7"
)
extract_pdf_bytes = _deprecated_alias(
    parse_pdf_bytes, old_name="extract_pdf_bytes", new_name="parse_pdf_bytes", since="0.1.0a7"
)


__all__ = [
    "ClassifyPageTool",
    "ExtractedTable",
    "GetOutlineTool",
    "GetPageTextTool",
    "KaosPdfError",
    "OCREngine",
    "OCRLine",
    "OCRMode",
    "OCRResult",
    "PDFMetadataTool",
    "ParsePDFTool",
    "PdfExtractionError",
    "PdfMetadata",
    "PdfNotFoundError",
    "PdfOutlineEntry",
    "PdfRenderError",
    "RenderPageTool",
    "SearchDocumentTool",
    "SearchResult",
    "SearchResults",
    "TableEngine",
    "TableMode",
    "TableResult",
    "TesseractEngine",
    "TesseractNotInstalledError",
    "__version__",
    "classify_document",
    "classify_page",
    "extract_page_text",
    "extract_pdf",
    "extract_pdf_bytes",
    "extract_pdf_with_tables",
    "get_default_ocr_engine",
    "get_default_table_engine",
    "get_page_count",
    "get_pdf_metadata",
    "get_pdf_outline",
    "parse_pdf",
    "parse_pdf_bytes",
    "register_pdf_authoring_tools",
    "register_pdf_documents_tools",
    "register_pdf_tools",
    "render_page",
    "search_document",
]

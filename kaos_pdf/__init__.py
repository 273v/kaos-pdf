"""kaos-pdf: PDF extraction and document processing for KAOS.

Uses pypdfium2 (Apache 2.0) for PDF parsing, producing kaos-content
ContentDocument AST with provenance (page numbers, bounding boxes).
"""

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
    extract_pdf,
    extract_pdf_bytes,
    extract_pdf_with_tables,
    get_page_count,
    get_pdf_metadata,
    get_pdf_outline,
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
    register_pdf_tools,
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
    "register_pdf_tools",
    "render_page",
    "search_document",
]

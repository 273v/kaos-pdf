"""MCP tool definitions for PDF extraction.

KaosTool implementations that can be registered with a KaosRuntime
and exposed via kaos-mcp. Each tool performs extraction, stores results
as artifacts, and returns summary + resource links.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from kaos_content.artifacts import document_outline, document_to_summary, store_document
from kaos_core import KaosContext, KaosRuntime, KaosTool, ToolMetadata, ToolResult
from kaos_core.types.annotations import ToolAnnotations
from kaos_core.types.enums import ToolCapability, ToolCategory
from kaos_core.types.parameters import ParameterSchema

from kaos_pdf.extract import (
    classify_document,
    classify_page,
    extract_page_text,
    get_page_count,
    get_pdf_metadata,
    get_pdf_outline,
    parse_pdf,
    render_page,
)

_MODULE = "kaos-pdf"
_VERSION = "0.1.0"

# Single-threaded executor for PDFium calls — the global _PDFIUM_LOCK serialises
# them anyway, and this keeps CPU-bound work off the async event loop.
_PDF_EXECUTOR = ThreadPoolExecutor(max_workers=1)

# All PDF tools are read-only, idempotent, and local-only (no external services).
_PDF_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

# ---------------------------------------------------------------------------
# Error-message hints (three-part: what / how / alternative) — audit-01 PDF-005
# ---------------------------------------------------------------------------
# Every error returned via ToolResult.create_error() must carry an
# alternative tool or approach so an LLM agent can self-correct without
# the user. These constants are the alternative-tool half of the
# three-part error contract — the call sites supply the "what" and "how"
# and append one of these.

_HINT_FILE_NOT_FOUND = (
    "If the file lives elsewhere or under a different name, use "
    "'kaos-source-fs-discover' to list candidate paths. For non-PDF "
    "formats, try 'kaos-office-extract-*' (DOCX/PPTX/XLSX) or "
    "'kaos-web-extract-readability' (HTML)."
)
_HINT_CORRUPTED_PDF = (
    "Alternatives: try 'kaos-pdf-extract-page-text' on individual pages "
    "to isolate the failure, or 'kaos-pdf-render-page' + an OCR engine "
    "if the document is a scan with no embedded text layer."
)
_HINT_PAGE_OUT_OF_RANGE = (
    "Run 'kaos-pdf-extract-metadata' first to confirm the document's "
    "page_count, or 'kaos-pdf-extract-parse' to retrieve the full body."
)
_HINT_NO_RUNTIME = (
    "Run this tool through 'kaos-pdf-serve' (or any host that injects a "
    "KaosRuntime context). For text-only extraction without artifact "
    "storage, use 'kaos-pdf-extract-page-text' instead."
)
_HINT_EMPTY_QUERY = (
    "Pass one or more whitespace-separated search terms. To retrieve the "
    "full document body without filtering, use 'kaos-pdf-extract-parse'."
)
_HINT_SEARCH_FAILED = (
    "Verify the artifact_id matches the response from a prior "
    "'kaos-pdf-extract-parse' call in the same session."
)


class ParsePDFTool(KaosTool):
    """Parse a PDF file into a structured ContentDocument."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-pdf-extract-parse",
            display_name="Parse PDF",
            description=(
                "Parse a PDF file into a structured document with paragraphs "
                "and provenance (page numbers, bounding boxes). "
                "Returns a summary and resource link to the full document."
            ),
            category=ToolCategory.DOCUMENT,
            capability=ToolCapability.EXTRACT,
            module_name=_MODULE,
            version=_VERSION,
            annotations=_PDF_ANNOTATIONS,
            input_schema=[
                ParameterSchema(name="path", type="string", description="Path to the PDF file."),
                ParameterSchema(
                    name="pages",
                    type="array",
                    description="Optional 0-based page indices to extract.",
                    required=False,
                    constraints={"items": {"type": "integer", "minimum": 0}},
                ),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        path = inputs["path"]
        if not Path(path).exists():
            return ToolResult.create_error(
                f"File not found: {path}. "
                "Verify the file path is correct and the file exists. " + _HINT_FILE_NOT_FOUND
            )

        loop = asyncio.get_running_loop()
        try:
            doc = await loop.run_in_executor(
                _PDF_EXECUTOR, lambda: parse_pdf(path, pages=inputs.get("pages"))
            )
        except Exception as exc:
            return ToolResult.create_error(
                f"PDF extraction failed for '{Path(path).name}': {exc}. "
                "The file may be corrupted, password-protected, or not a valid PDF. "
                + _HINT_CORRUPTED_PDF
            )

        if context is None or context.runtime is None:
            return ToolResult.create_error(
                "No runtime context available. "
                "ParsePDF requires a KaosRuntime with artifact storage. "
                "Use 'kaos-pdf-extract-page-text' for context-free text extraction."
            )

        page_count = await loop.run_in_executor(_PDF_EXECUTOR, lambda: get_page_count(path))
        manifest = await store_document(
            doc,
            context.runtime,
            context,
            name=Path(path).stem,
            description=f"Extracted from {Path(path).name}",
            metadata={"source_path": str(path), "page_count": page_count},
        )

        summary = document_to_summary(doc, max_length=500)
        outline = document_outline(doc)

        from kaos_content.views import DocumentView

        view = DocumentView(doc)

        return manifest.to_tool_result(
            summary=summary,
            structured_content={
                "artifact_id": manifest.artifact_id,
                "title": doc.metadata.title,
                "page_count": doc.metadata.extra.get("page_count", 0),
                "block_count": len(doc.body),
                "has_pages": view.has_pages,
                "has_sections": view.has_sections,
                "outline": outline[:10],
                "section_count": len(view.flat_sections),
                "body_uri": manifest.body_uri,
                "pages_uri": f"kaos://content/{manifest.artifact_id}/pages",
                "sections_uri": f"kaos://content/{manifest.artifact_id}/sections",
            },
        )


class GetPageTextTool(KaosTool):
    """Extract plain text from a single PDF page."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-pdf-extract-page-text",
            display_name="Get PDF Page Text",
            description="Extract plain text from a specific page of a PDF file.",
            category=ToolCategory.DOCUMENT,
            capability=ToolCapability.EXTRACT,
            module_name=_MODULE,
            version=_VERSION,
            annotations=_PDF_ANNOTATIONS,
            input_schema=[
                ParameterSchema(name="path", type="string", description="Path to the PDF file."),
                ParameterSchema(name="page", type="integer", description="0-based page index."),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        path = inputs["path"]
        if not Path(path).exists():
            return ToolResult.create_error(
                f"File not found: {path}. "
                "Verify the file path is correct and the file exists. " + _HINT_FILE_NOT_FOUND
            )
        loop = asyncio.get_running_loop()
        try:
            page_idx = inputs["page"]
            text = await loop.run_in_executor(
                _PDF_EXECUTOR, lambda: extract_page_text(path, page_idx)
            )
            return ToolResult.create_text(text)
        except IndexError:
            total = await loop.run_in_executor(_PDF_EXECUTOR, lambda: get_page_count(path))
            return ToolResult.create_error(
                f"Page {inputs['page']} is out of range. "
                f"This document has {total} pages (valid indices: 0 to {total - 1}). "
                + _HINT_PAGE_OUT_OF_RANGE
            )
        except Exception as exc:
            return ToolResult.create_error(
                f"Text extraction failed for '{Path(path).name}': {exc}. "
                "The file may be corrupted or not a valid PDF. " + _HINT_CORRUPTED_PDF
            )


class RenderPageTool(KaosTool):
    """Render a PDF page as an image."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-pdf-render-page",
            display_name="Render PDF Page",
            description="Render a PDF page as an image (PNG). Returns a resource link.",
            category=ToolCategory.DOCUMENT,
            capability=ToolCapability.EXTRACT,
            module_name=_MODULE,
            version=_VERSION,
            annotations=_PDF_ANNOTATIONS,
            input_schema=[
                ParameterSchema(name="path", type="string", description="Path to the PDF file."),
                ParameterSchema(name="page", type="integer", description="0-based page index."),
                ParameterSchema(
                    name="dpi",
                    type="integer",
                    description="Rendering DPI (default 300).",
                    required=False,
                    default=300,
                ),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        path = inputs["path"]
        if not Path(path).exists():
            return ToolResult.create_error(
                f"File not found: {path}. "
                "Verify the file path is correct and the file exists. " + _HINT_FILE_NOT_FOUND
            )

        if context is None or context.runtime is None:
            return ToolResult.create_error(
                "No runtime context available. "
                "RenderPage requires artifact storage to return the rendered image. "
                + _HINT_NO_RUNTIME
            )

        loop = asyncio.get_running_loop()
        try:
            dpi = inputs.get("dpi", 300)
            page_idx = inputs["page"]
            img = await loop.run_in_executor(
                _PDF_EXECUTOR, lambda: render_page(path, page_idx, dpi=dpi)
            )
        except IndexError:
            total = await loop.run_in_executor(_PDF_EXECUTOR, lambda: get_page_count(path))
            return ToolResult.create_error(
                f"Page {inputs['page']} is out of range. "
                f"This document has {total} pages (valid indices: 0 to {total - 1}). "
                + _HINT_PAGE_OUT_OF_RANGE
            )
        except Exception as exc:
            return ToolResult.create_error(
                f"Page render failed for '{Path(path).name}': {exc}. "
                "The file may be corrupted or not a valid PDF. " + _HINT_CORRUPTED_PDF
            )

        from kaos_content.images.artifacts import store_image

        manifest = await store_image(
            img,
            context.runtime,
            context,
            name=f"{Path(path).stem}_page_{inputs['page']}",
            description=f"Page {inputs['page']} of {Path(path).name} at {dpi} DPI",
        )

        return manifest.to_tool_result(
            summary=f"Rendered page {inputs['page']} at {dpi} DPI ({img.width}x{img.height})",
            structured_content={
                "artifact_id": manifest.artifact_id,
                "width": img.width,
                "height": img.height,
                "dpi": dpi,
                "body_uri": manifest.body_uri,
            },
        )


class PDFMetadataTool(KaosTool):
    """Get PDF file metadata and document classification."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-pdf-metadata",
            display_name="PDF Metadata",
            description="Get metadata and document type classification from a PDF file.",
            category=ToolCategory.DOCUMENT,
            capability=ToolCapability.EXTRACT,
            module_name=_MODULE,
            version=_VERSION,
            annotations=_PDF_ANNOTATIONS,
            input_schema=[
                ParameterSchema(name="path", type="string", description="Path to the PDF file."),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        path = inputs["path"]
        if not Path(path).exists():
            return ToolResult.create_error(
                f"File not found: {path}. "
                "Verify the file path is correct and the file exists. " + _HINT_FILE_NOT_FOUND
            )
        loop = asyncio.get_running_loop()
        try:
            meta = await loop.run_in_executor(_PDF_EXECUTOR, lambda: get_pdf_metadata(path))
            doc_type = await loop.run_in_executor(_PDF_EXECUTOR, lambda: classify_document(path))
            output: dict[str, Any] = meta.to_dict()
            output["document_type"] = doc_type
            title = meta.title or Path(path).name
            summary = f"{title} — {meta.page_count} pages, type: {doc_type}"
            return ToolResult.create_success(output=output, summary=summary)
        except Exception as exc:
            return ToolResult.create_error(
                f"Failed to read metadata from '{Path(path).name}': {exc}. "
                "The file may be corrupted or not a valid PDF. " + _HINT_CORRUPTED_PDF
            )


class SearchDocumentTool(KaosTool):
    """Search within an extracted document by text query."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-pdf-search-document",
            display_name="Search Document",
            description=(
                "Search within a previously extracted document by text query. "
                "Returns matching blocks with page numbers, section context, and relevance scores."
            ),
            category=ToolCategory.DOCUMENT,
            capability=ToolCapability.QUERY,
            module_name=_MODULE,
            version=_VERSION,
            annotations=_PDF_ANNOTATIONS,
            input_schema=[
                ParameterSchema(
                    name="artifact_id",
                    type="string",
                    description="Artifact ID of the extracted document.",
                ),
                ParameterSchema(name="query", type="string", description="Search query text."),
                ParameterSchema(
                    name="top_k",
                    type="integer",
                    description="Maximum number of results (default 10).",
                    required=False,
                    default=10,
                ),
                ParameterSchema(
                    name="level",
                    type="string",
                    description=(
                        "Search granularity: 'paragraph' (default) or 'sentence'. "
                        "Sentence-level search requires kaos-nlp-core."
                    ),
                    required=False,
                    default="paragraph",
                    constraints={"enum": ["paragraph", "sentence"]},
                ),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        artifact_id = inputs["artifact_id"]
        query = inputs["query"]
        top_k = inputs.get("top_k", 10)
        level = inputs.get("level", "paragraph")

        if not query.strip():
            return ToolResult.create_error("Query must not be empty. " + _HINT_EMPTY_QUERY)

        if context is None or context.runtime is None:
            return ToolResult.create_error(
                "No runtime context available. "
                "SearchDocument requires a previously parsed document. "
                "First use 'kaos-pdf-extract-parse' to extract the document."
            )

        loop = asyncio.get_running_loop()
        try:
            from kaos_content.artifacts import load_document
            from kaos_content.search import search_document

            doc = await load_document(artifact_id, context.runtime)
            search_results = await loop.run_in_executor(
                _PDF_EXECUTOR,
                lambda: search_document(doc, query, top_k=top_k, preview_length=200, level=level),
            )

            result_data = {
                "results": [
                    {
                        "text": r.text,
                        "score": r.score,
                        "block_ref": r.block_ref,
                        "page": r.page,
                        "section_ref": r.section_ref,
                        "section_text": r.section_title,
                    }
                    for r in search_results.results
                ],
                "total_matches": search_results.total_matches,
                "has_more": search_results.has_more,
                "query": query,
            }
            more = " (has more)" if search_results.has_more else ""
            summary = f"Found {search_results.total_matches} matches for '{query}'{more}"
            return ToolResult.create_success(output=result_data, summary=summary)
        except Exception as exc:
            return ToolResult.create_error(
                f"Search failed for artifact '{artifact_id}': {exc}. " + _HINT_SEARCH_FAILED
            )


class GetOutlineTool(KaosTool):
    """Extract the PDF bookmark/outline tree (table of contents)."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-pdf-get-outline",
            display_name="Get PDF Outline",
            description=(
                "Extract the bookmark/outline tree (table of contents) from a PDF file. "
                "Returns a list of entries with title, nesting level, and target page. "
                "Use 'kaos-pdf-extract-parse' first if you need full document content."
            ),
            category=ToolCategory.DOCUMENT,
            capability=ToolCapability.EXTRACT,
            module_name=_MODULE,
            version=_VERSION,
            annotations=_PDF_ANNOTATIONS,
            input_schema=[
                ParameterSchema(name="path", type="string", description="Path to the PDF file."),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        path = inputs["path"]
        if not Path(path).exists():
            return ToolResult.create_error(
                f"File not found: {path}. "
                "Verify the file path is correct and the file exists. " + _HINT_FILE_NOT_FOUND
            )

        loop = asyncio.get_running_loop()
        try:
            outline = await loop.run_in_executor(_PDF_EXECUTOR, lambda: get_pdf_outline(path))
            page_count = await loop.run_in_executor(_PDF_EXECUTOR, lambda: get_page_count(path))
            summary = f"{len(outline)} outline entries in {Path(path).name} ({page_count} pages)"
            return ToolResult.create_success(
                output={
                    "outline": [entry.to_dict() for entry in outline],
                    "entry_count": len(outline),
                },
                summary=summary,
            )
        except Exception as exc:
            return ToolResult.create_error(
                f"Failed to extract outline from '{Path(path).name}': {exc}. "
                "The file may be corrupted or not a valid PDF. " + _HINT_CORRUPTED_PDF
            )


class ClassifyPageTool(KaosTool):
    """Classify a PDF page or entire document by content type."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="kaos-pdf-classify-page",
            display_name="Classify PDF Page",
            description=(
                "Classify a PDF page as 'text', 'image', 'mixed', or 'blank'. "
                "If page is omitted, classifies the entire document "
                "('text', 'image', 'mixed', 'scanned', or 'blank'). "
                "Use 'kaos-pdf-metadata' for full metadata including classification."
            ),
            category=ToolCategory.DOCUMENT,
            capability=ToolCapability.ANALYZE,
            module_name=_MODULE,
            version=_VERSION,
            annotations=_PDF_ANNOTATIONS,
            input_schema=[
                ParameterSchema(name="path", type="string", description="Path to the PDF file."),
                ParameterSchema(
                    name="page",
                    type="integer",
                    description="0-based page index. If omitted, classifies the whole document.",
                    required=False,
                ),
            ],
        )

    async def execute(
        self, inputs: dict[str, Any], context: KaosContext | None = None
    ) -> ToolResult:
        path = inputs["path"]
        if not Path(path).exists():
            return ToolResult.create_error(
                f"File not found: {path}. "
                "Verify the file path is correct and the file exists. " + _HINT_FILE_NOT_FOUND
            )

        loop = asyncio.get_running_loop()
        page = inputs.get("page")
        try:
            if page is not None:
                classification = await loop.run_in_executor(
                    _PDF_EXECUTOR, lambda: classify_page(path, page)
                )
                summary = f"Page {page} of {Path(path).name}: {classification}"
                return ToolResult.create_success(
                    output={"classification": classification, "page": page},
                    summary=summary,
                )
            else:
                classification = await loop.run_in_executor(
                    _PDF_EXECUTOR, lambda: classify_document(path)
                )
                summary = f"{Path(path).name}: {classification}"
                return ToolResult.create_success(
                    output={"classification": classification, "page": None},
                    summary=summary,
                )
        except IndexError:
            total = await loop.run_in_executor(_PDF_EXECUTOR, lambda: get_page_count(path))
            return ToolResult.create_error(
                f"Page {page} is out of range. "
                f"This document has {total} pages (valid indices: 0 to {total - 1}). "
                + _HINT_PAGE_OUT_OF_RANGE
            )
        except Exception as exc:
            return ToolResult.create_error(
                f"Classification failed for '{Path(path).name}': {exc}. "
                "The file may be corrupted or not a valid PDF. " + _HINT_CORRUPTED_PDF
            )


def register_pdf_documents_tools(runtime: KaosRuntime) -> int:
    """Register the read-only PDF document tools.

    These are the parsers, extractors, renderers, and metadata
    inspectors — every tool that takes a PDF and returns text,
    structure, or a derived view *without* mutating or producing a
    new PDF artifact. Callers that want only the "documents" group
    of a SessionToolSet ceiling should register this and skip
    :func:`register_pdf_authoring_tools`.
    """
    tools: list[KaosTool] = [
        ParsePDFTool(),
        GetPageTextTool(),
        RenderPageTool(),
        PDFMetadataTool(),
        SearchDocumentTool(),
        GetOutlineTool(),
        ClassifyPageTool(),
    ]
    for tool in tools:
        runtime.tools.register_tool(tool)
    return len(tools)


def register_pdf_authoring_tools(runtime: KaosRuntime) -> int:
    """Register the PDF authoring tools (writers, mutators, redactors).

    Reserved for future ``kaos-pdf-write-*`` style tools that *create*
    or *mutate* PDFs (merge, split, watermark, redact, sign). Currently
    empty — this function is part of the public surface so that the
    SessionToolSet ``authoring`` group has a stable registration entry
    point even before any writers ship. Returns ``0`` until then.
    """
    del runtime
    return 0


def register_pdf_tools(runtime: KaosRuntime) -> int:
    """Register all PDF tools with the runtime. Returns count.

    Backward-compatible union of :func:`register_pdf_documents_tools`
    and :func:`register_pdf_authoring_tools` — the entry point
    ``kaos-mcp serve --module pdf`` calls. Existing callers continue
    to see the same tool inventory.
    """
    count = register_pdf_documents_tools(runtime)
    count += register_pdf_authoring_tools(runtime)
    return count

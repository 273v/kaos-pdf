"""Tests for PDF MCP tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from kaos_core import KaosContext, KaosRuntime, ToolResult

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


class TestParsePDFTool:
    async def test_parse_text_pdf(self, federal_register_pdf: Path, runtime: KaosRuntime) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        tool = ParsePDFTool()
        result = await tool.execute({"path": str(federal_register_pdf)}, context=context)

        assert not result.isError
        structured = result.require_structured()
        assert structured["artifact_id"] is not None
        assert structured["page_count"] == 1
        assert structured["block_count"] > 0
        assert "outline" in structured
        assert "body_uri" in structured

    async def test_parse_multipage(self, cftc_regulations_pdf: Path, runtime: KaosRuntime) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        tool = ParsePDFTool()
        result = await tool.execute({"path": str(cftc_regulations_pdf)}, context=context)

        assert not result.isError
        assert result.get_structured("page_count") == 34

    async def test_parse_with_page_selection(
        self, cftc_regulations_pdf: Path, runtime: KaosRuntime
    ) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        tool = ParsePDFTool()
        result = await tool.execute(
            {"path": str(cftc_regulations_pdf), "pages": [0, 1]}, context=context
        )

        assert not result.isError

    async def test_parse_missing_file(self, runtime: KaosRuntime) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        tool = ParsePDFTool()
        result = await tool.execute({"path": "/nonexistent.pdf"}, context=context)
        assert result.isError

    async def test_parse_no_context(self, federal_register_pdf: Path) -> None:
        tool = ParsePDFTool()
        result = await tool.execute({"path": str(federal_register_pdf)}, context=None)
        assert result.isError


class TestGetPageTextTool:
    async def test_get_text(self, federal_register_pdf: Path) -> None:
        tool = GetPageTextTool()
        result = await tool.execute({"path": str(federal_register_pdf), "page": 0})
        assert not result.isError
        assert len(result.content) > 0

    async def test_get_text_returns_plain_text_boundary(self, federal_register_pdf: Path) -> None:
        """PDF-004: plain-text tool uses ToolResult.create_text(...).

        The page text must be reachable via the documented typed accessor
        (`result.text` / `require_text`) without being wrapped in a JSON
        envelope or stringified dict.
        """
        tool = GetPageTextTool()
        result = await tool.execute({"path": str(federal_register_pdf), "page": 0})
        assert not result.isError
        # No structured payload — this is a plain-text tool.
        assert result.structuredContent is None
        text = result.require_text()
        assert isinstance(text, str)
        assert len(text) > 0
        # Sanity: text is not a stringified Python dict / JSON envelope.
        assert not text.lstrip().startswith(("{", "["))

    async def test_get_text_invalid_page(self, federal_register_pdf: Path) -> None:
        tool = GetPageTextTool()
        result = await tool.execute({"path": str(federal_register_pdf), "page": 999})
        assert result.isError

    async def test_get_text_missing_file(self) -> None:
        tool = GetPageTextTool()
        result = await tool.execute({"path": "/nonexistent.pdf", "page": 0})
        assert result.isError


class TestRenderPageTool:
    async def test_render(self, federal_register_pdf: Path, runtime: KaosRuntime) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        tool = RenderPageTool()
        result = await tool.execute(
            {"path": str(federal_register_pdf), "page": 0, "dpi": 150}, context=context
        )

        assert not result.isError
        structured = result.require_structured()
        assert structured["width"] > 0
        assert structured["height"] > 0
        assert structured["dpi"] == 150

    async def test_render_missing_file(self, runtime: KaosRuntime) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        tool = RenderPageTool()
        result = await tool.execute({"path": "/nonexistent.pdf", "page": 0}, context=context)
        assert result.isError

    async def test_render_no_context(self, federal_register_pdf: Path) -> None:
        tool = RenderPageTool()
        result = await tool.execute({"path": str(federal_register_pdf), "page": 0}, context=None)
        assert result.isError


class TestPDFMetadataTool:
    async def test_metadata(self, federal_register_pdf: Path) -> None:
        tool = PDFMetadataTool()
        result = await tool.execute({"path": str(federal_register_pdf)})
        assert not result.isError
        structured = result.require_structured()
        assert "page_count" in structured
        assert "document_type" in structured

    async def test_metadata_missing(self) -> None:
        tool = PDFMetadataTool()
        result = await tool.execute({"path": "/nonexistent.pdf"})
        assert result.isError


class TestGetOutlineTool:
    async def test_outline_text_pdf(self, cftc_regulations_pdf: Path) -> None:
        tool = GetOutlineTool()
        result = await tool.execute({"path": str(cftc_regulations_pdf)})
        assert not result.isError
        structured = result.require_structured()
        assert "outline" in structured
        assert "entry_count" in structured
        assert isinstance(structured["outline"], list)
        assert structured["entry_count"] == len(structured["outline"])

    async def test_outline_no_bookmarks(self, federal_register_pdf: Path) -> None:
        tool = GetOutlineTool()
        result = await tool.execute({"path": str(federal_register_pdf)})
        assert not result.isError
        structured = result.require_structured()
        assert isinstance(structured["outline"], list)
        assert structured["entry_count"] >= 0

    async def test_outline_missing_file(self) -> None:
        tool = GetOutlineTool()
        result = await tool.execute({"path": "/nonexistent.pdf"})
        assert result.isError
        msg = result.require_text()
        assert "Verify" in msg

    async def test_outline_metadata(self) -> None:
        tool = GetOutlineTool()
        meta = tool.metadata
        assert meta.name == "kaos-pdf-get-outline"
        assert meta.category == "document"
        assert meta.capability == "extract"


class TestClassifyPageTool:
    async def test_classify_single_page(self, federal_register_pdf: Path) -> None:
        tool = ClassifyPageTool()
        result = await tool.execute({"path": str(federal_register_pdf), "page": 0})
        assert not result.isError
        structured = result.require_structured()
        assert structured["classification"] in ("text", "image", "mixed", "blank")
        assert structured["page"] == 0

    async def test_classify_whole_document(self, federal_register_pdf: Path) -> None:
        tool = ClassifyPageTool()
        result = await tool.execute({"path": str(federal_register_pdf)})
        assert not result.isError
        structured = result.require_structured()
        assert structured["classification"] in ("text", "image", "mixed", "scanned", "blank")
        assert structured["page"] is None

    async def test_classify_scanned_pdf(self, scanned_pdf: Path) -> None:
        tool = ClassifyPageTool()
        result = await tool.execute({"path": str(scanned_pdf)})
        assert not result.isError
        structured = result.require_structured()
        assert structured["classification"] in ("scanned", "image", "mixed")

    async def test_classify_page_out_of_range(self, federal_register_pdf: Path) -> None:
        tool = ClassifyPageTool()
        result = await tool.execute({"path": str(federal_register_pdf), "page": 999})
        assert result.isError
        msg = result.require_text()
        assert "out of range" in msg
        assert "pages" in msg

    async def test_classify_missing_file(self) -> None:
        tool = ClassifyPageTool()
        result = await tool.execute({"path": "/nonexistent.pdf"})
        assert result.isError
        msg = result.require_text()
        assert "Verify" in msg

    async def test_classify_metadata(self) -> None:
        tool = ClassifyPageTool()
        meta = tool.metadata
        assert meta.name == "kaos-pdf-classify-page"
        assert meta.category == "document"
        assert meta.capability == "analyze"


class TestToolAnnotations:
    """All PDF tools must declare correct MCP annotations."""

    @pytest.mark.parametrize(
        "tool_cls",
        [
            ParsePDFTool,
            GetPageTextTool,
            RenderPageTool,
            PDFMetadataTool,
            SearchDocumentTool,
            GetOutlineTool,
            ClassifyPageTool,
        ],
    )
    def test_annotations_set(self, tool_cls: type[ParsePDFTool]) -> None:
        tool = tool_cls()
        ann = tool.metadata.annotations
        assert ann is not None, f"{tool_cls.__name__} missing annotations"
        assert ann.readOnlyHint is True
        assert ann.destructiveHint is False
        assert ann.idempotentHint is True
        assert ann.openWorldHint is False


class TestErrorMessages:
    """Error messages should be actionable for LLM self-correction."""

    @staticmethod
    def _error_text(result: ToolResult) -> str:
        """Extract error message text from a ToolResult."""
        return result.require_text()

    async def test_file_not_found_has_guidance(self) -> None:
        tool = ParsePDFTool()
        result = await tool.execute({"path": "/nonexistent.pdf"}, context=None)
        assert result.isError
        msg = self._error_text(result)
        assert "Verify" in msg

    async def test_page_out_of_range_has_count(self, federal_register_pdf: Path) -> None:
        tool = GetPageTextTool()
        result = await tool.execute({"path": str(federal_register_pdf), "page": 999})
        assert result.isError
        msg = self._error_text(result)
        assert "out of range" in msg
        assert "pages" in msg

    async def test_no_context_suggests_alternative(self, federal_register_pdf: Path) -> None:
        tool = ParsePDFTool()
        result = await tool.execute({"path": str(federal_register_pdf)}, context=None)
        assert result.isError
        msg = self._error_text(result)
        assert "kaos-pdf-extract-page-text" in msg

    async def test_search_no_context_suggests_parse(self) -> None:
        tool = SearchDocumentTool()
        result = await tool.execute({"artifact_id": "fake", "query": "test"}, context=None)
        assert result.isError
        msg = self._error_text(result)
        assert "kaos-pdf-extract-parse" in msg

    async def test_empty_query_has_guidance(self, runtime: KaosRuntime) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        tool = SearchDocumentTool()
        result = await tool.execute({"artifact_id": "fake", "query": "   "}, context=context)
        assert result.isError
        msg = self._error_text(result)
        assert "search terms" in msg

    @pytest.mark.parametrize(
        ("tool_cls", "inputs"),
        [
            (ParsePDFTool, {"path": "/nonexistent.pdf"}),
            (GetPageTextTool, {"path": "/nonexistent.pdf", "page": 0}),
            (RenderPageTool, {"path": "/nonexistent.pdf", "page": 0}),
            (PDFMetadataTool, {"path": "/nonexistent.pdf"}),
            (GetOutlineTool, {"path": "/nonexistent.pdf"}),
            (ClassifyPageTool, {"path": "/nonexistent.pdf"}),
        ],
    )
    async def test_file_not_found_three_part_error(
        self,
        tool_cls: type[ParsePDFTool],
        inputs: dict[str, Any],
    ) -> None:
        """PDF-005: every File-Not-Found error must include all three parts.

        (1) what went wrong, (2) how to fix it, (3) an alternative tool
        or approach. The third part is the part the audit found missing.
        """
        tool = tool_cls()
        result = await tool.execute(inputs)
        assert result.isError
        msg = self._error_text(result)
        # (1) what
        assert "File not found" in msg
        # (2) how
        assert "Verify" in msg
        # (3) alternative — must name at least one concrete tool
        assert any(
            name in msg
            for name in (
                "kaos-source-fs-discover",
                "kaos-office-extract",
                "kaos-web-extract",
            )
        ), f"no alternative tool named in: {msg!r}"


def test_pdf_005_error_hints_all_name_alternatives() -> None:
    """PDF-005 contract: every shared error hint must name at least one
    concrete alternative tool so an LLM agent can self-correct."""
    from kaos_pdf import tools as tools_mod

    hints = {name: getattr(tools_mod, name) for name in dir(tools_mod) if name.startswith("_HINT_")}
    assert hints, "no _HINT_* constants found in kaos_pdf.tools"
    for name, value in hints.items():
        assert isinstance(value, str) and value, f"{name} is empty"
        assert "kaos-" in value, f"{name} does not name an alternative kaos-* tool: {value!r}"


class TestRegisterTools:
    def test_register(self, runtime: KaosRuntime) -> None:
        count = register_pdf_tools(runtime)
        assert count == 7

        tool_names = {t.metadata.name for t in runtime.tools.list_tool_objects()}
        assert "kaos-pdf-extract-parse" in tool_names
        assert "kaos-pdf-extract-page-text" in tool_names
        assert "kaos-pdf-render-page" in tool_names
        assert "kaos-pdf-metadata" in tool_names
        assert "kaos-pdf-get-outline" in tool_names
        assert "kaos-pdf-classify-page" in tool_names

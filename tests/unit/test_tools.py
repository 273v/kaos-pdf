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
    OcrPageTool,
    ParsePDFTool,
    PDFMetadataTool,
    RenderPageTool,
    SearchDocumentTool,
    register_pdf_authoring_tools,
    register_pdf_documents_tools,
    register_pdf_tools,
)


class TestParsePDFTool:
    async def test_parse_text_pdf(self, federal_register_pdf: Path, runtime: KaosRuntime) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        tool = ParsePDFTool()
        result = await tool.execute({"path": federal_register_pdf.as_uri()}, context=context)

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
        result = await tool.execute({"path": cftc_regulations_pdf.as_uri()}, context=context)

        assert not result.isError
        assert result.get_structured("page_count") == 34

    async def test_parse_with_page_selection(
        self, cftc_regulations_pdf: Path, runtime: KaosRuntime
    ) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        tool = ParsePDFTool()
        result = await tool.execute(
            {"path": cftc_regulations_pdf.as_uri(), "pages": [0, 1]}, context=context
        )

        assert not result.isError

    async def test_parse_missing_file(self, runtime: KaosRuntime) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        tool = ParsePDFTool()
        result = await tool.execute({"path": "file:///nonexistent.pdf"}, context=context)
        assert result.isError

    async def test_parse_no_context(self, federal_register_pdf: Path) -> None:
        tool = ParsePDFTool()
        result = await tool.execute({"path": federal_register_pdf.as_uri()}, context=None)
        assert result.isError


class TestGetPageTextTool:
    async def test_get_text(self, federal_register_pdf: Path) -> None:
        tool = GetPageTextTool()
        result = await tool.execute({"path": federal_register_pdf.as_uri(), "page": 0})
        assert not result.isError
        assert len(result.content) > 0

    async def test_get_text_returns_plain_text_boundary(self, federal_register_pdf: Path) -> None:
        """PDF-004: plain-text tool uses ToolResult.create_text(...).

        The page text must be reachable via the documented typed accessor
        (`result.text` / `require_text`) without being wrapped in a JSON
        envelope or stringified dict.
        """
        tool = GetPageTextTool()
        result = await tool.execute({"path": federal_register_pdf.as_uri(), "page": 0})
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
        result = await tool.execute({"path": federal_register_pdf.as_uri(), "page": 999})
        assert result.isError

    async def test_get_text_missing_file(self) -> None:
        tool = GetPageTextTool()
        result = await tool.execute({"path": "file:///nonexistent.pdf", "page": 0})
        assert result.isError


class TestRenderPageTool:
    async def test_render(self, federal_register_pdf: Path, runtime: KaosRuntime) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        tool = RenderPageTool()
        result = await tool.execute(
            {"path": federal_register_pdf.as_uri(), "page": 0, "dpi": 150}, context=context
        )

        assert not result.isError
        structured = result.require_structured()
        assert structured["width"] > 0
        assert structured["height"] > 0
        assert structured["dpi"] == 150

    async def test_render_missing_file(self, runtime: KaosRuntime) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        tool = RenderPageTool()
        result = await tool.execute({"path": "file:///nonexistent.pdf", "page": 0}, context=context)
        assert result.isError

    async def test_render_no_context(self, federal_register_pdf: Path) -> None:
        tool = RenderPageTool()
        result = await tool.execute(
            {"path": federal_register_pdf.as_uri(), "page": 0}, context=None
        )
        assert result.isError


class TestOcrPageTool:
    """OCR tool for scanned-PDF pages.

    Closes corpus-stress S03 (Failure 3 in
    ``2026-05-26-corpus-stress-5-failure-resolution.md``): kaos-pdf had
    a complete ``TesseractEngine`` at ``kaos_pdf/ocr/tesseract.py`` but
    exposed no MCP wrapper, so the agent could render a scanned page
    but had no tool to OCR it. This adds the missing entry point.
    """

    async def test_ocr_scanned_synthetic_pdf(self, tmp_path: Path) -> None:
        """End-to-end: render → OCR through the tool surface.

        Synthesizes a bitmap-font scanned PDF carrying a distinctive
        token, runs ``kaos-pdf-ocr-page``, and asserts the token
        survives tesseract OCR. The token deliberately uses a
        high-confidence vocabulary ("FALCON" is unambiguous to
        tesseract; mixed-case or numerals would be flakier).

        Requires tesseract + pytesseract on the host. Skips otherwise
        — the contract this test pins is "the tool wraps the engine,"
        not "tesseract OCR is perfect on bitmap fonts".
        """
        pytest.importorskip("pytesseract")
        pytest.importorskip("PIL")
        pytest.importorskip("reportlab")

        try:
            import pytesseract

            pytesseract.get_tesseract_version()
        except Exception as exc:
            pytest.skip(f"tesseract binary not available: {exc}")

        from PIL import Image, ImageDraw, ImageFont
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas

        # Build an image-only PDF with a distinctive token.
        token = "FALCON"
        img = Image.new("RGB", (1700, 2200), color="white")
        draw = ImageDraw.Draw(img)
        font = ImageFont.load_default()
        draw.text((60, 60), "INTERNAL MEMO", fill="black", font=font)
        draw.text((60, 120), f"Codename: {token}", fill="black", font=font)
        import io

        png_buf = io.BytesIO()
        img.save(png_buf, format="PNG")
        png_buf.seek(0)
        pdf_path = tmp_path / "scanned.pdf"
        c = canvas.Canvas(str(pdf_path), pagesize=LETTER)
        pw, ph = LETTER
        c.drawImage(
            ImageReader(png_buf),
            x=0,
            y=0,
            width=pw,
            height=ph,
            preserveAspectRatio=True,
            mask="auto",
        )
        c.showPage()
        c.save()

        tool = OcrPageTool()
        result = await tool.execute({"path": pdf_path.as_uri(), "page": 0, "dpi": 300})

        assert not result.isError, f"OCR errored: {result.content}"
        structured = result.require_structured()
        assert "text" in structured
        # The distinctive token must survive OCR. Tesseract on bitmap
        # fonts can mangle small words ("Oryx" → "Onyx") but reliably
        # recovers all-caps tokens of 5+ characters.
        assert token in structured["text"], (
            f"planted token {token!r} missing from OCR output {structured['text']!r}"
        )
        assert structured["mean_confidence"] > 0.0
        assert structured["line_count"] >= 1
        assert structured["engine"] == "tesseract"

    async def test_ocr_missing_file(self) -> None:
        """OCR returns a clear path-resolution error for missing files."""
        tool = OcrPageTool()
        result = await tool.execute({"path": "file:///nonexistent.pdf", "page": 0})
        assert result.isError

    async def test_ocr_page_out_of_range(self, federal_register_pdf: Path) -> None:
        """Out-of-range page emits the standard agent-facing hint."""
        tool = OcrPageTool()
        # Real Federal Register fixture has 1 page; page=99 is out of range.
        result = await tool.execute({"path": federal_register_pdf.as_uri(), "page": 99})
        assert result.isError

    async def test_ocr_text_pdf_round_trips(self, federal_register_pdf: Path) -> None:
        """OCR on a text PDF still produces SOMETHING (it rasterizes first).

        Edge case: passing a text-layer PDF to OCR is not an error
        path — the renderer produces an image of the rasterized page
        and tesseract OCRs that. The result is lower-quality than the
        text-layer extraction, but the tool MUST NOT error here
        because the agent might call OCR as a fallback without first
        knowing the page is scanned.
        """
        pytest.importorskip("pytesseract")
        try:
            import pytesseract

            pytesseract.get_tesseract_version()
        except Exception as exc:
            pytest.skip(f"tesseract binary not available: {exc}")

        tool = OcrPageTool()
        result = await tool.execute({"path": federal_register_pdf.as_uri(), "page": 0, "dpi": 200})
        assert not result.isError


class TestPDFMetadataTool:
    async def test_metadata(self, federal_register_pdf: Path) -> None:
        tool = PDFMetadataTool()
        result = await tool.execute({"path": federal_register_pdf.as_uri()})
        assert not result.isError
        structured = result.require_structured()
        assert "page_count" in structured
        assert "document_type" in structured

    async def test_metadata_missing(self) -> None:
        tool = PDFMetadataTool()
        result = await tool.execute({"path": "file:///nonexistent.pdf"})
        assert result.isError


class TestGetOutlineTool:
    async def test_outline_text_pdf(self, cftc_regulations_pdf: Path) -> None:
        tool = GetOutlineTool()
        result = await tool.execute({"path": cftc_regulations_pdf.as_uri()})
        assert not result.isError
        structured = result.require_structured()
        assert "outline" in structured
        assert "entry_count" in structured
        assert isinstance(structured["outline"], list)
        assert structured["entry_count"] == len(structured["outline"])

    async def test_outline_no_bookmarks(self, federal_register_pdf: Path) -> None:
        tool = GetOutlineTool()
        result = await tool.execute({"path": federal_register_pdf.as_uri()})
        assert not result.isError
        structured = result.require_structured()
        assert isinstance(structured["outline"], list)
        assert structured["entry_count"] >= 0

    async def test_outline_missing_file(self) -> None:
        tool = GetOutlineTool()
        result = await tool.execute({"path": "file:///nonexistent.pdf"})
        assert result.isError
        msg = result.require_text()
        # path-resolver three-part error: "<what>. How to fix: <fix>. Alternative: <tool>."
        assert "not found" in msg.lower()
        assert "How to fix" in msg

    async def test_outline_metadata(self) -> None:
        tool = GetOutlineTool()
        meta = tool.metadata
        assert meta.name == "kaos-pdf-get-outline"
        assert meta.category == "document"
        assert meta.capability == "extract"


class TestClassifyPageTool:
    async def test_classify_single_page(self, federal_register_pdf: Path) -> None:
        tool = ClassifyPageTool()
        result = await tool.execute({"path": federal_register_pdf.as_uri(), "page": 0})
        assert not result.isError
        structured = result.require_structured()
        assert structured["classification"] in ("text", "image", "mixed", "blank")
        assert structured["page"] == 0

    async def test_classify_whole_document(self, federal_register_pdf: Path) -> None:
        tool = ClassifyPageTool()
        result = await tool.execute({"path": federal_register_pdf.as_uri()})
        assert not result.isError
        structured = result.require_structured()
        assert structured["classification"] in ("text", "image", "mixed", "scanned", "blank")
        assert structured["page"] is None

    async def test_classify_scanned_pdf(self, scanned_pdf: Path) -> None:
        tool = ClassifyPageTool()
        result = await tool.execute({"path": scanned_pdf.as_uri()})
        assert not result.isError
        structured = result.require_structured()
        assert structured["classification"] in ("scanned", "image", "mixed")

    async def test_classify_page_out_of_range(self, federal_register_pdf: Path) -> None:
        tool = ClassifyPageTool()
        result = await tool.execute({"path": federal_register_pdf.as_uri(), "page": 999})
        assert result.isError
        msg = result.require_text()
        assert "out of range" in msg
        assert "pages" in msg

    async def test_classify_missing_file(self) -> None:
        tool = ClassifyPageTool()
        result = await tool.execute({"path": "file:///nonexistent.pdf"})
        assert result.isError
        msg = result.require_text()
        # path-resolver three-part error: "<what>. How to fix: <fix>. Alternative: <tool>."
        assert "not found" in msg.lower()
        assert "How to fix" in msg

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
        result = await tool.execute({"path": "file:///nonexistent.pdf"}, context=None)
        assert result.isError
        msg = self._error_text(result)
        # path-resolver wraps file-not-found in a three-part contract:
        # "<what>. How to fix: <fix>. Alternative: <tool>."
        assert "not found" in msg.lower()
        assert "How to fix" in msg

    async def test_page_out_of_range_has_count(self, federal_register_pdf: Path) -> None:
        tool = GetPageTextTool()
        result = await tool.execute({"path": federal_register_pdf.as_uri(), "page": 999})
        assert result.isError
        msg = self._error_text(result)
        assert "out of range" in msg
        assert "pages" in msg

    async def test_no_context_suggests_alternative(self, federal_register_pdf: Path) -> None:
        tool = ParsePDFTool()
        result = await tool.execute({"path": federal_register_pdf.as_uri()}, context=None)
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
            (ParsePDFTool, {"path": "file:///nonexistent.pdf"}),
            (GetPageTextTool, {"path": "file:///nonexistent.pdf", "page": 0}),
            (RenderPageTool, {"path": "file:///nonexistent.pdf", "page": 0}),
            (PDFMetadataTool, {"path": "file:///nonexistent.pdf"}),
            (GetOutlineTool, {"path": "file:///nonexistent.pdf"}),
            (ClassifyPageTool, {"path": "file:///nonexistent.pdf"}),
        ],
    )
    async def test_file_not_found_three_part_error(
        self,
        tool_cls: type[ParsePDFTool],
        inputs: dict[str, Any],
    ) -> None:
        """PDF-005: every File-Not-Found error must include all three parts.

        (1) what went wrong, (2) how to fix it, (3) an alternative tool
        or approach. After Stage 2 of the VFS-blind-tools plan the
        canonical three-part error is composed by
        ``kaos_core.path_resolver.InputPathResolutionError.to_agent_message()``
        — vocabulary changes (``How to fix`` / ``Alternative``) but the
        contract stays the same.
        """
        tool = tool_cls()
        result = await tool.execute(inputs)
        assert result.isError
        msg = self._error_text(result)
        # (1) what
        assert "not found" in msg.lower()
        # (2) how
        assert "How to fix" in msg
        # (3) alternative — must name at least one concrete tool
        assert "Alternative" in msg
        assert any(
            name in msg
            for name in (
                "kaos-source-fs-discover",
                "kaos-office-extract",
                "kaos-web-extract",
                "kaos-core-vfs-list",
                "kaos-core-list-artifacts",
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
        # 8 tools as of 0.1.4: ParsePDF, GetPageText, RenderPage, OcrPage,
        # PDFMetadata, SearchDocument, GetOutline, ClassifyPage. OcrPage
        # was added to close corpus-stress S03 (scanned-PDF OCR was
        # unrecoverable because the engine existed but had no MCP entry).
        assert count == 8

        tool_names = {t.metadata.name for t in runtime.tools.list_tool_objects()}
        assert "kaos-pdf-extract-parse" in tool_names
        assert "kaos-pdf-extract-page-text" in tool_names
        assert "kaos-pdf-render-page" in tool_names
        assert "kaos-pdf-ocr-page" in tool_names
        assert "kaos-pdf-metadata" in tool_names
        assert "kaos-pdf-search-document" in tool_names
        assert "kaos-pdf-get-outline" in tool_names
        assert "kaos-pdf-classify-page" in tool_names

    def test_register_documents_subset(self, runtime: KaosRuntime) -> None:
        """The documents group registers every read-only PDF tool.

        Pins the SessionToolSet ``documents`` group entry point: a
        caller that wants only document reads (no future writers) gets
        the same 8 tools today. When ``register_pdf_authoring_tools``
        starts shipping writers, those land in the ``authoring`` group
        and stay out of this list.
        """
        count = register_pdf_documents_tools(runtime)
        assert count == 8
        # Every documents tool is read-only by ToolAnnotations.
        for tool in runtime.tools.list_tool_objects():
            annotations = tool.metadata.annotations
            assert annotations is not None, f"{tool.metadata.name} missing annotations"
            assert annotations.readOnlyHint is True, (
                f"{tool.metadata.name} is registered as a documents tool but is not read-only"
            )

    def test_register_authoring_empty_today(self, runtime: KaosRuntime) -> None:
        """The authoring group has no writers yet; entry point must still exist.

        Registering ``register_pdf_authoring_tools`` must be a no-op
        that returns 0 — the SessionToolSet ``authoring`` group has a
        stable surface from PR 1 onward even before writers ship.
        """
        count = register_pdf_authoring_tools(runtime)
        assert count == 0
        assert runtime.tools.list_tool_objects() == []

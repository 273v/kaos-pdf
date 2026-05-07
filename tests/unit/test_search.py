"""Tests for SearchDocumentTool."""

from __future__ import annotations

from pathlib import Path

from kaos_content.artifacts import store_document
from kaos_core import KaosContext, KaosRuntime

from kaos_pdf.extract import extract_pdf
from kaos_pdf.tools import SearchDocumentTool, register_pdf_tools


class TestSearchDocumentTool:
    async def test_search_finds_matching_text(
        self, federal_register_pdf: Path, runtime: KaosRuntime
    ) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        doc = extract_pdf(federal_register_pdf)
        manifest = await store_document(doc, runtime, context, name="search-test")

        tool = SearchDocumentTool()
        result = await tool.execute(
            {"artifact_id": manifest.artifact_id, "query": "Federal Register"},
            context=context,
        )

        assert not result.isError
        assert result.get_structured("total_matches", 0) > 0

    async def test_search_returns_page_context(
        self, cftc_regulations_pdf: Path, runtime: KaosRuntime
    ) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        doc = extract_pdf(cftc_regulations_pdf)
        manifest = await store_document(doc, runtime, context, name="search-ctx")

        tool = SearchDocumentTool()
        result = await tool.execute(
            {"artifact_id": manifest.artifact_id, "query": "Commission"},
            context=context,
        )

        assert not result.isError
        structured = result.require_structured()
        results = structured["results"]
        assert len(results) > 0
        # Results should have page context
        for r in results:
            assert "page" in r
            assert "block_ref" in r

    async def test_search_no_matches(
        self, federal_register_pdf: Path, runtime: KaosRuntime
    ) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        doc = extract_pdf(federal_register_pdf)
        manifest = await store_document(doc, runtime, context, name="search-empty")

        tool = SearchDocumentTool()
        result = await tool.execute(
            {"artifact_id": manifest.artifact_id, "query": "xyznonexistent123"},
            context=context,
        )

        assert not result.isError
        structured = result.require_structured()
        assert structured["total_matches"] == 0
        assert structured["has_more"] is False

    async def test_search_top_k(self, cftc_regulations_pdf: Path, runtime: KaosRuntime) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        doc = extract_pdf(cftc_regulations_pdf)
        manifest = await store_document(doc, runtime, context, name="search-topk")

        tool = SearchDocumentTool()
        result = await tool.execute(
            {"artifact_id": manifest.artifact_id, "query": "the", "top_k": 3},
            context=context,
        )

        assert not result.isError
        structured = result.require_structured()
        assert len(structured["results"]) <= 3
        # "the" should match many paragraphs — has_more should be True
        assert structured["has_more"] is True
        assert structured["total_matches"] > 3

    async def test_search_empty_query_error(self, runtime: KaosRuntime) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        tool = SearchDocumentTool()
        result = await tool.execute(
            {"artifact_id": "fake", "query": ""},
            context=context,
        )
        assert result.isError

    async def test_search_no_context_error(self) -> None:
        tool = SearchDocumentTool()
        result = await tool.execute(
            {"artifact_id": "fake", "query": "test"},
            context=None,
        )
        assert result.isError

    async def test_search_results_sorted_by_score(
        self, cftc_regulations_pdf: Path, runtime: KaosRuntime
    ) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        doc = extract_pdf(cftc_regulations_pdf)
        manifest = await store_document(doc, runtime, context, name="search-sort")

        tool = SearchDocumentTool()
        result = await tool.execute(
            {"artifact_id": manifest.artifact_id, "query": "regulation"},
            context=context,
        )

        structured = result.require_structured()
        if structured["total_matches"] > 1:
            results = structured["results"]
            scores = [r["score"] for r in results]
            assert scores == sorted(scores, reverse=True)


class TestRegisterPdfToolsIncludesSearch:
    def test_register_includes_search(self, runtime: KaosRuntime) -> None:
        count = register_pdf_tools(runtime)
        assert count == 7

        tool_names = {t.metadata.name for t in runtime.tools.list_tool_objects()}
        assert "kaos-pdf-search-document" in tool_names


class TestEnhancedParseResults:
    async def test_parse_includes_navigation_uris(
        self, federal_register_pdf: Path, runtime: KaosRuntime
    ) -> None:
        from kaos_pdf.tools import ParsePDFTool

        context = KaosContext.create(session_id="test", runtime=runtime)
        tool = ParsePDFTool()
        result = await tool.execute({"path": str(federal_register_pdf)}, context=context)

        assert not result.isError
        sc = result.require_structured()
        assert "pages_uri" in sc
        assert "sections_uri" in sc
        assert "has_pages" in sc
        assert "has_sections" in sc
        assert "section_count" in sc
        assert sc["artifact_id"] in sc["pages_uri"]

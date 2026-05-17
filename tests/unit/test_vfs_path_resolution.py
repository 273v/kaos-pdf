"""VFS-aware path resolution for kaos-pdf MCP tools.

Regression tests for Stage 2 of
``kaos-modules/docs/plans/vfs-blind-tools-audit-and-fix-plan.md``.

Each kaos-pdf file-input tool must resolve its ``path`` argument
through :func:`kaos_core.path_resolver.resolve_input_path` so that a
PDF uploaded into the session VFS (e.g. by a host UI's chat panel) is
discoverable by a bare filename — the failure mode that produced a
production incident in ``single-user-chat`` where five uploaded
``.docx`` files all returned ``File not found`` and the agent then
fabricated a legal-analysis table from zero successful reads.

The tests below assert the contract through the real public tool
entry points: write PDF bytes into the runtime VFS, invoke each tool
with the bare VFS path (no ``kaos://`` prefix, no absolute path), and
check that it succeeds.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kaos_core import KaosContext, KaosRuntime

from kaos_pdf.tools import (
    ClassifyPageTool,
    GetOutlineTool,
    GetPageTextTool,
    ParsePDFTool,
    PDFMetadataTool,
    RenderPageTool,
)


@pytest.fixture
async def vfs_pdf_path(runtime: KaosRuntime, federal_register_pdf: Path) -> str:
    """Upload a real PDF fixture into the session VFS and yield its VFS path.

    Mirrors what a host UI's upload endpoint does: bytes go into
    ``runtime.vfs`` scoped to a session id, the tool later sees only the
    relative path the user pasted into chat.
    """
    pdf_bytes = federal_register_pdf.read_bytes()
    vfs_path = f"files/{federal_register_pdf.name}"
    handle = runtime.vfs.get_path(vfs_path, context_id="test")
    await handle.write_bytes(pdf_bytes)
    return vfs_path


class TestParsePDFViaVFS:
    """``kaos-pdf-extract-parse`` resolves bare VFS paths to PDF bytes."""

    async def test_parse_accepts_vfs_path(self, runtime: KaosRuntime, vfs_pdf_path: str) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        tool = ParsePDFTool()
        result = await tool.execute({"path": vfs_pdf_path}, context=context)

        assert not result.isError, result.require_text()
        structured = result.require_structured()
        assert structured["artifact_id"], "parser must emit an artifact for the VFS upload"
        assert structured["page_count"] == 1
        assert structured["block_count"] > 0


class TestGetPageTextViaVFS:
    async def test_get_page_text_accepts_vfs_path(
        self, runtime: KaosRuntime, vfs_pdf_path: str
    ) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        tool = GetPageTextTool()
        result = await tool.execute({"path": vfs_pdf_path, "page": 0}, context=context)

        assert not result.isError, result.require_text()
        text = result.require_text()
        assert isinstance(text, str)
        assert text.strip(), "page text must be non-empty for a real federal-register fixture"


class TestRenderPageViaVFS:
    async def test_render_accepts_vfs_path(self, runtime: KaosRuntime, vfs_pdf_path: str) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        tool = RenderPageTool()
        result = await tool.execute(
            {"path": vfs_pdf_path, "page": 0, "dpi": 100},
            context=context,
        )

        assert not result.isError, result.require_text()
        structured = result.require_structured()
        assert structured["width"] > 0
        assert structured["height"] > 0
        assert structured["dpi"] == 100


class TestPDFMetadataViaVFS:
    async def test_metadata_accepts_vfs_path(self, runtime: KaosRuntime, vfs_pdf_path: str) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        tool = PDFMetadataTool()
        result = await tool.execute({"path": vfs_pdf_path}, context=context)

        assert not result.isError, result.require_text()
        structured = result.require_structured()
        assert structured["page_count"] == 1
        assert "document_type" in structured


class TestGetOutlineViaVFS:
    async def test_outline_accepts_vfs_path(self, runtime: KaosRuntime, vfs_pdf_path: str) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        tool = GetOutlineTool()
        result = await tool.execute({"path": vfs_pdf_path}, context=context)

        assert not result.isError, result.require_text()
        structured = result.require_structured()
        assert "outline" in structured
        assert "entry_count" in structured


class TestClassifyPageViaVFS:
    async def test_classify_accepts_vfs_path(self, runtime: KaosRuntime, vfs_pdf_path: str) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        tool = ClassifyPageTool()
        result = await tool.execute({"path": vfs_pdf_path}, context=context)

        assert not result.isError, result.require_text()
        structured = result.require_structured()
        assert structured["classification"] in ("text", "image", "mixed", "scanned", "blank")


class TestParsePDFViaArtifactURI:
    """``kaos-pdf-extract-parse`` accepts ``kaos://artifacts/<id>`` URIs.

    Stages the PDF as a session artifact, hands the artifact URI back
    to the tool, and asserts the parser succeeds and threads the source
    artifact id through into the response's structured content so a
    downstream tool can trace the parsed document back to its source.
    """

    async def test_artifact_uri_is_resolved_and_traced(
        self, runtime: KaosRuntime, federal_register_pdf: Path
    ) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        # Stage the PDF in the artifact store, mimicking what an upload
        # path would do when it materialises bytes into the session.
        manifest = await runtime.artifacts.create_from_bytes(
            federal_register_pdf.read_bytes(),
            context_id="test",
            session_id="test",
            mime_type="application/pdf",
            name=federal_register_pdf.stem,
        )
        artifact_uri = f"kaos://artifacts/{manifest.artifact_id}"

        tool = ParsePDFTool()
        result = await tool.execute({"path": artifact_uri}, context=context)

        assert not result.isError, result.require_text()
        structured = result.require_structured()
        # The parser emits a NEW artifact (the parsed ContentDocument is
        # different bytes from the source PDF), but the source artifact
        # id is threaded through so the chain stays traceable.
        assert structured["artifact_id"], "parsed-document artifact id missing"
        assert structured["artifact_id"] != manifest.artifact_id
        assert structured.get("source_artifact_id") == manifest.artifact_id


class TestPathResolverErrorContract:
    """Resolver errors must keep the three-part what/how/alternative contract."""

    async def test_missing_vfs_path_yields_three_part_error(self, runtime: KaosRuntime) -> None:
        context = KaosContext.create(session_id="test", runtime=runtime)
        tool = ParsePDFTool()
        result = await tool.execute({"path": "files/does-not-exist.pdf"}, context=context)
        assert result.isError
        msg = result.require_text()
        # (1) what went wrong
        assert "not found" in msg.lower()
        # (2) how to fix
        assert "How to fix" in msg
        # (3) alternative tool — must name at least one concrete kaos-* tool
        assert "Alternative" in msg

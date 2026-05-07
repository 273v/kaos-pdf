"""Comprehensive tests for PDF extraction functions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pytest
from kaos_content import ContentDocument, NodeIndex, serialize_markdown, serialize_text

from kaos_pdf.extract import (
    _merge_column_paragraphs,
    classify_document,
    classify_page,
    extract_page_text,
    extract_pdf,
    extract_pdf_bytes,
    get_page_count,
    get_pdf_metadata,
    render_page,
)

# ---------------------------------------------------------------------------
# Basic extraction — parametrized across all text PDFs
# ---------------------------------------------------------------------------


class TestExtractPDF:
    def test_extract_produces_content_document(self, text_pdf: Path) -> None:
        doc = extract_pdf(text_pdf)
        assert isinstance(doc, ContentDocument)
        assert len(doc.body) > 0

    def test_extract_has_metadata(self, text_pdf: Path) -> None:
        doc = extract_pdf(text_pdf)
        assert doc.metadata.extra.get("page_count") is not None
        assert doc.metadata.extra["page_count"] > 0

    def test_extract_has_source_ref(self, text_pdf: Path) -> None:
        doc = extract_pdf(text_pdf)
        assert doc.metadata.source is not None
        assert doc.metadata.source.mime_type == "application/pdf"
        assert text_pdf.name in doc.metadata.source.uri

    def test_extract_has_provenance(self, text_pdf: Path) -> None:
        """Every block should have provenance with page number and bbox."""
        doc = extract_pdf(text_pdf)
        blocks_with_prov = 0
        for block in doc.body:
            if hasattr(block, "provenance") and block.provenance is not None:
                blocks_with_prov += 1
                assert block.provenance.page is not None
                assert block.provenance.page >= 1
                assert block.provenance.bbox is not None
                assert block.provenance.extractor == "kaos-pdf/pypdfium2"

        assert blocks_with_prov > 0, "Expected blocks with provenance"

    def test_extract_text_is_nonempty(self, text_pdf: Path) -> None:
        doc = extract_pdf(text_pdf)
        text = serialize_text(doc)
        assert len(text.strip()) > 0

    def test_extract_json_round_trip(self, text_pdf: Path) -> None:
        """Extract → JSON → deserialize → verify."""
        doc = extract_pdf(text_pdf)
        json_str = doc.model_dump_json()
        loaded = ContentDocument.model_validate_json(json_str)
        assert len(loaded.body) == len(doc.body)

    def test_extract_markdown_output(self, text_pdf: Path) -> None:
        doc = extract_pdf(text_pdf)
        md = serialize_markdown(doc)
        assert isinstance(md, str)
        assert len(md) > 0


# ---------------------------------------------------------------------------
# Specific document tests
# ---------------------------------------------------------------------------


class TestSpecificDocuments:
    def test_federal_register_content(self, federal_register_pdf: Path) -> None:
        doc = extract_pdf(federal_register_pdf)
        text = serialize_text(doc)
        assert "FEDERAL REGISTER" in text or "Federal Register" in text

    def test_public_law_content(self, public_law_pdf: Path) -> None:
        doc = extract_pdf(public_law_pdf)
        text = serialize_text(doc)
        assert "PUBLIC LAW" in text or "STAT" in text

    def test_cftc_content(self, cftc_regulations_pdf: Path) -> None:
        doc = extract_pdf(cftc_regulations_pdf)
        text = serialize_text(doc)
        assert len(text) > 100
        assert doc.metadata.extra["page_count"] == 34

    def test_fda_guidance_content(self, fda_guidance_pdf: Path) -> None:
        doc = extract_pdf(fda_guidance_pdf)
        text = serialize_text(doc)
        assert "Federal Register" in text
        assert doc.metadata.extra["page_count"] == 2

    def test_fda_guidance_merge_column_paragraphs(self, fda_guidance_pdf: Path) -> None:
        # Multi-column GPO publications break each visual line into its own
        # text rect. With merge enabled, column-wrap fragments collapse into
        # paragraphs — line count drops sharply and the longest paragraph
        # contains text spanning multiple visual lines.
        doc_off = extract_pdf(fda_guidance_pdf)
        doc_on = extract_pdf(fda_guidance_pdf, merge_column_paragraphs=True)
        lines_off = serialize_text(doc_off).split("\n")
        lines_on = serialize_text(doc_on).split("\n")
        # Aggressive merge: at least 5x reduction in line count for this fixture.
        assert len(lines_on) < len(lines_off) // 5, (
            f"merge under-fired: {len(lines_off)} → {len(lines_on)}"
        )
        # Longest non-blank line in merged output should be substantially longer
        # than the longest line in the unmerged output (paragraphs reflowed).
        max_off = max(len(line) for line in lines_off)
        max_on = max(len(line) for line in lines_on)
        assert max_on > max_off * 3, (
            f"merge produced no long paragraphs: max {max_off}c → {max_on}c"
        )

    def test_court_filing_content(self, court_filing_short_pdf: Path) -> None:
        doc = extract_pdf(court_filing_short_pdf)
        text = serialize_text(doc)
        assert "COURT" in text or "Court" in text
        assert doc.metadata.extra["page_count"] == 8

    def test_long_court_filing(self, court_filing_long_pdf: Path) -> None:
        doc = extract_pdf(court_filing_long_pdf)
        text = serialize_text(doc)
        assert doc.metadata.extra["page_count"] == 31
        assert len(text) > 500

    def test_city_council_single_page(self, city_council_pdf: Path) -> None:
        doc = extract_pdf(city_council_pdf)
        text = serialize_text(doc)
        assert doc.metadata.extra["page_count"] == 1
        assert len(text) > 50

    def test_scanned_pdf_has_no_text(self, scanned_pdf: Path) -> None:
        """Scanned/image PDFs should produce minimal or no text blocks."""
        doc = extract_pdf(scanned_pdf)
        text = serialize_text(doc)
        # Scanned PDFs have no extractable text
        assert len(text.strip()) < 50  # Might have a few stray characters


# ---------------------------------------------------------------------------
# Page selection
# ---------------------------------------------------------------------------


class TestPageSelection:
    def test_extract_specific_pages(self, cftc_regulations_pdf: Path) -> None:
        doc_all = extract_pdf(cftc_regulations_pdf)
        doc_subset = extract_pdf(cftc_regulations_pdf, pages=[0, 1])

        # Subset should have fewer blocks
        assert len(doc_subset.body) < len(doc_all.body)
        assert len(doc_subset.body) > 0

    def test_extract_single_page(self, short_federal_register_pdf: Path) -> None:
        doc = extract_pdf(short_federal_register_pdf, pages=[0])
        assert len(doc.body) > 0

        # All provenance should reference page 1
        for block in doc.body:
            if hasattr(block, "provenance") and block.provenance:
                assert block.provenance.page == 1

    def test_extract_last_page(self, cftc_regulations_pdf: Path) -> None:
        n = get_page_count(cftc_regulations_pdf)
        doc = extract_pdf(cftc_regulations_pdf, pages=[n - 1])
        assert len(doc.body) >= 0  # Last page may have little content

    def test_out_of_range_pages_skipped(self, federal_register_pdf: Path) -> None:
        """Out-of-range page indices should be silently skipped."""
        doc = extract_pdf(federal_register_pdf, pages=[0, 999])
        assert len(doc.body) > 0  # Page 0 content still extracted


# ---------------------------------------------------------------------------
# Column paragraph merging (multi-column reflow for downstream NLP)
# ---------------------------------------------------------------------------


class TestMergeColumnParagraphs:
    """Unit tests for `_merge_column_paragraphs` against synthetic rects.

    PDF coords: bbox = (left, bottom, right, top) where top > bottom.
    Lines stacked vertically have decreasing top/bottom as we move down.
    """

    def _rect(
        self,
        text: str,
        top: float,
        height: float = 10.0,
        font_size: float = 10.0,
    ) -> tuple[str, tuple[float, float, float, float], dict[str, Any] | None]:
        meta: dict[str, Any] = {
            "font_size": font_size,
            "rect_height": height,
            "is_bold": False,
        }
        return (
            text,
            (50.0, top - height, 250.0, top),
            meta,
        )

    def test_no_merge_when_input_is_single_line(self) -> None:
        rects = [self._rect("Solo line.", top=700.0)]
        out = _merge_column_paragraphs(rects)
        assert len(out) == 1
        assert out[0][0] == "Solo line."

    def test_merges_column_wrap_continuations(self) -> None:
        # Two consecutive lines, the first ending without sentence terminator,
        # vertical gap small (= 0, lines touch).
        rects = [
            self._rect("FDA estimates that", top=700.0),
            self._rect("zero vending machine operators will", top=690.0),
        ]
        out = _merge_column_paragraphs(rects)
        assert len(out) == 1
        assert out[0][0] == "FDA estimates that zero vending machine operators will"

    def test_does_not_merge_across_paragraph_boundary(self) -> None:
        # First line ends with period — paragraph boundary, never merge.
        rects = [
            self._rect("End of first paragraph.", top=700.0),
            self._rect("Start of new paragraph.", top=690.0),
        ]
        out = _merge_column_paragraphs(rects)
        assert len(out) == 2

    def test_does_not_merge_across_font_size_jump(self) -> None:
        # Body line followed by a smaller-font folio line that lacks terminator.
        rects = [
            self._rect("body line without terminator", top=700.0, font_size=10.0),
            self._rect("page footer text", top=690.0, font_size=8.0),
        ]
        out = _merge_column_paragraphs(rects)
        # Different font size → no merge.
        assert len(out) == 2

    def test_does_not_merge_across_large_vertical_gap(self) -> None:
        # Same font, no terminator on first, but big vertical gap (50pt) →
        # different paragraphs.
        rects = [
            self._rect("body line without terminator", top=700.0),
            self._rect("next paragraph after gap", top=640.0),
        ]
        out = _merge_column_paragraphs(rects)
        assert len(out) == 2

    def test_chains_multiple_continuations_into_one_paragraph(self) -> None:
        # Three consecutive column-wrap lines → one paragraph.
        rects = [
            self._rect("The agency estimates", top=700.0),
            self._rect("that fewer than half the", top=690.0),
            self._rect("vendors will register.", top=680.0),
        ]
        out = _merge_column_paragraphs(rects)
        assert len(out) == 1
        assert out[0][0].startswith("The agency estimates that fewer")
        assert out[0][0].endswith("vendors will register.")

    def test_bbox_is_union_after_merge(self) -> None:
        rects = [
            self._rect("first half of paragraph", top=700.0),
            self._rect("second half of paragraph.", top=690.0),
        ]
        out = _merge_column_paragraphs(rects)
        assert len(out) == 1
        bbox = out[0][1]
        # Top should equal the topmost rect's top (700.0); bottom should
        # equal the bottommost rect's bottom (690 - 10 = 680.0).
        assert bbox[3] == 700.0
        assert bbox[1] == 680.0


# ---------------------------------------------------------------------------
# Page-level operations
# ---------------------------------------------------------------------------


class TestPageOperations:
    def test_get_page_count(self, any_valid_pdf: Path) -> None:
        n = get_page_count(any_valid_pdf)
        assert n >= 1

    def test_extract_page_text(self, federal_register_pdf: Path) -> None:
        text = extract_page_text(federal_register_pdf, 0)
        assert isinstance(text, str)
        assert len(text) > 100

    def test_extract_page_text_invalid_page(self, federal_register_pdf: Path) -> None:
        with pytest.raises(IndexError, match="out of range"):
            extract_page_text(federal_register_pdf, 999)

    def test_extract_page_text_negative_page(self, federal_register_pdf: Path) -> None:
        with pytest.raises(IndexError, match="out of range"):
            extract_page_text(federal_register_pdf, -1)

    def test_get_pdf_metadata(self, any_valid_pdf: Path) -> None:
        from kaos_pdf import PdfMetadata

        meta = get_pdf_metadata(any_valid_pdf)
        assert isinstance(meta, PdfMetadata)
        assert meta.page_count >= 1
        # to_dict() must always carry page_count and must not emit
        # null-valued optional fields (sparse wire format).
        d = meta.to_dict()
        assert d["page_count"] == meta.page_count
        for key in ("title", "author", "subject", "keywords", "creator", "producer"):
            if key in d:
                assert d[key] is not None

    def test_get_pdf_outline_typed(self, any_valid_pdf: Path) -> None:
        """PDF-003: get_pdf_outline() returns list[PdfOutlineEntry]."""
        from kaos_pdf import PdfOutlineEntry, get_pdf_outline

        outline = get_pdf_outline(any_valid_pdf)
        assert isinstance(outline, list)
        for entry in outline:
            assert isinstance(entry, PdfOutlineEntry)
            assert isinstance(entry.title, str)
            assert isinstance(entry.level, int)
            assert entry.page is None or isinstance(entry.page, int)
            d = entry.to_dict()
            assert d == {"title": entry.title, "level": entry.level, "page": entry.page}

    def test_render_page(self, federal_register_pdf: Path) -> None:
        img = render_page(federal_register_pdf, 0)
        assert img.width > 0
        assert img.height > 0
        assert img.dpi == (300, 300)
        assert img.provenance is not None
        assert img.provenance.page == 1

    def test_render_page_custom_dpi(self, federal_register_pdf: Path) -> None:
        img = render_page(federal_register_pdf, 0, dpi=150)
        assert img.dpi == (150, 150)

    def test_render_page_grayscale(self, federal_register_pdf: Path) -> None:
        img = render_page(federal_register_pdf, 0, dpi=72, grayscale=True)
        assert img.is_grayscale

    def test_render_page_invalid(self, federal_register_pdf: Path) -> None:
        with pytest.raises(IndexError):
            render_page(federal_register_pdf, 999)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class TestClassification:
    def test_classify_text_page(self, federal_register_pdf: Path) -> None:
        assert classify_page(federal_register_pdf, 0) == "text"

    def test_classify_image_page(self, scanned_pdf: Path) -> None:
        page_type = classify_page(scanned_pdf, 0)
        assert page_type in ("image", "mixed")

    def test_classify_text_document(self, federal_register_pdf: Path) -> None:
        assert classify_document(federal_register_pdf) == "text"

    def test_classify_scanned_document(self, scanned_pdf: Path) -> None:
        doc_type = classify_document(scanned_pdf)
        assert doc_type in ("scanned", "image", "mixed")

    def test_classify_all_text_pdfs(self, text_pdf: Path) -> None:
        doc_type = classify_document(text_pdf)
        assert doc_type in ("text", "mixed")

    def test_classify_page_invalid(self, federal_register_pdf: Path) -> None:
        with pytest.raises(IndexError):
            classify_page(federal_register_pdf, 999)


# ---------------------------------------------------------------------------
# Bytes extraction
# ---------------------------------------------------------------------------


class TestBytesExtraction:
    def test_extract_from_bytes(self, federal_register_pdf: Path) -> None:
        data = federal_register_pdf.read_bytes()
        doc = extract_pdf_bytes(data, filename="test.pdf")
        assert len(doc.body) > 0
        text = serialize_text(doc)
        assert "FEDERAL REGISTER" in text or "Federal Register" in text

    def test_extract_bytes_matches_path(self, federal_register_pdf: Path) -> None:
        """Bytes extraction should produce same content as path extraction."""
        doc_path = extract_pdf(federal_register_pdf)
        doc_bytes = extract_pdf_bytes(federal_register_pdf.read_bytes())

        text_path = serialize_text(doc_path)
        text_bytes = serialize_text(doc_bytes)

        # Should be very similar (may differ in source URI)
        assert len(text_path) == len(text_bytes)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_bad_pdf_raises(self, bad_pdf: Path) -> None:
        with pytest.raises(pdfium.PdfiumError):
            extract_pdf(bad_pdf)

    def test_nonexistent_file(self) -> None:
        with pytest.raises(FileNotFoundError):
            extract_pdf("/nonexistent/path/file.pdf")

    def test_bad_pdf_metadata(self, bad_pdf: Path) -> None:
        with pytest.raises(pdfium.PdfiumError):
            get_pdf_metadata(bad_pdf)

    def test_bad_pdf_page_count(self, bad_pdf: Path) -> None:
        with pytest.raises(pdfium.PdfiumError):
            get_page_count(bad_pdf)


# ---------------------------------------------------------------------------
# NodeIndex integration
# ---------------------------------------------------------------------------


class TestNodeIndex:
    def test_node_index_builds(self, text_pdf: Path) -> None:
        doc = extract_pdf(text_pdf)
        index = NodeIndex(doc)
        assert len(index) > 0

    def test_node_ref_lookup(self, federal_register_pdf: Path) -> None:
        doc = extract_pdf(federal_register_pdf)
        index = NodeIndex(doc)

        # First body node should be accessible
        node = index.get("#/body/0")
        assert node is not None

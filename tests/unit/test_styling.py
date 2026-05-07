"""Tests for bold/italic/link extraction — text styling + hyperlink annotations."""

from __future__ import annotations

import pytest

from kaos_pdf.extract import (
    _rects_overlap,
    _style_text,
    _text_rect_covered_by,
)


class TestRectCovered:
    def test_fully_inside(self) -> None:
        text = (10.0, 10.0, 20.0, 20.0)
        link = (0.0, 0.0, 100.0, 100.0)
        assert _text_rect_covered_by(text, link)

    def test_fully_outside(self) -> None:
        text = (200.0, 200.0, 210.0, 210.0)
        link = (0.0, 0.0, 100.0, 100.0)
        assert not _text_rect_covered_by(text, link)

    def test_edge_touch_is_not_covered(self) -> None:
        # Text rect touches the link rect only at the right edge — zero area intersection.
        text = (100.0, 10.0, 110.0, 20.0)
        link = (0.0, 0.0, 100.0, 100.0)
        assert not _text_rect_covered_by(text, link)

    def test_below_coverage_threshold(self) -> None:
        # 10% of the text rect is inside the link — below default 50%.
        text = (90.0, 10.0, 100.0, 20.0)  # area = 100
        link = (0.0, 0.0, 91.0, 100.0)  # intersects 1x10 = 10 area → 10%
        assert not _text_rect_covered_by(text, link)

    def test_custom_threshold(self) -> None:
        text = (0.0, 0.0, 10.0, 10.0)
        link = (0.0, 0.0, 5.0, 10.0)  # 50% coverage
        assert _text_rect_covered_by(text, link, coverage=0.4)
        assert not _text_rect_covered_by(text, link, coverage=0.6)

    def test_zero_area_text(self) -> None:
        text = (10.0, 10.0, 10.0, 10.0)  # degenerate
        link = (0.0, 0.0, 100.0, 100.0)
        assert not _text_rect_covered_by(text, link)


class TestRectsOverlap:
    def test_overlap(self) -> None:
        assert _rects_overlap((0.0, 0.0, 10.0, 10.0), (5.0, 5.0, 15.0, 15.0))

    def test_no_overlap(self) -> None:
        assert not _rects_overlap((0.0, 0.0, 10.0, 10.0), (20.0, 20.0, 30.0, 30.0))


class TestStyleText:
    def test_plain_text_unchanged(self) -> None:
        node = _style_text("hello", None, url=None)
        assert node.node_type == "text"
        assert node.value == "hello"

    def test_bold_wraps_in_strong(self) -> None:
        node = _style_text("hi", {"is_bold": True}, url=None)
        assert node.node_type == "strong"

    def test_italic_wraps_in_emphasis(self) -> None:
        node = _style_text("hi", {"is_italic": True}, url=None)
        assert node.node_type == "emphasis"

    def test_bold_italic_nests(self) -> None:
        node = _style_text("hi", {"is_bold": True, "is_italic": True}, url=None)
        # italic applied first, then bold wraps it
        assert node.node_type == "strong"
        inner = node.children[0]
        assert inner.node_type == "emphasis"

    def test_link_wraps_styled(self) -> None:
        node = _style_text("hi", {"is_bold": True}, url="https://example.com")
        assert node.node_type == "link"
        assert node.url == "https://example.com"
        assert node.children[0].node_type == "strong"

    def test_link_without_style(self) -> None:
        node = _style_text("click", None, url="https://example.com")
        assert node.node_type == "link"
        assert node.children[0].value == "click"


@pytest.fixture
def libreoffice_available() -> bool:
    import shutil

    return shutil.which("libreoffice") is not None


class TestExtractionWithStyling:
    """End-to-end: build DOCX, render to PDF, extract, verify styling survives."""

    def test_link_and_bold_roundtrip(self, libreoffice_available: bool, tmp_path) -> None:
        if not libreoffice_available:
            pytest.skip("libreoffice not installed")
        try:
            from kaos_content.model.blocks import Paragraph
            from kaos_content.model.document import ContentDocument
            from kaos_content.model.inlines import Link, Strong, Text
            from kaos_content.traversal import find_by_type, find_links
            from kaos_office.docx.writer import write_docx  # ty: ignore[unresolved-import]
        except ImportError:
            pytest.skip("kaos-office / kaos-content not available")

        import subprocess

        doc = ContentDocument(
            body=(
                Paragraph(
                    children=(
                        Text(value="Normal "),
                        Strong(children=(Text(value="bold text"),)),
                        Text(value=" and "),
                        Link(
                            url="https://example.com/probe",
                            children=(Text(value="LINK_MARKER"),),
                        ),
                        Text(value=" tail."),
                    )
                ),
            )
        )
        docx = tmp_path / "probe.docx"
        write_docx(doc, docx)
        subprocess.run(
            [
                "libreoffice",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(tmp_path),
                str(docx),
            ],
            check=True,
            capture_output=True,
        )
        pdf = tmp_path / "probe.pdf"
        assert pdf.exists()

        from kaos_pdf import extract_pdf

        extracted = extract_pdf(pdf, combine_fragments=False)
        links = list(find_links(extracted))
        link_texts = ["".join(getattr(c, "value", "") for c in lnk.children) for lnk in links]
        assert any("LINK_MARKER" in t for t in link_texts), (
            f"expected LINK_MARKER inside a Link node; got {link_texts}"
        )
        # The bbox-coverage check must prevent adjacent text from being tagged.
        assert not any("Normal" in t for t in link_texts), (
            f"'Normal' must not be tagged as part of a link; got {link_texts}"
        )

        bolds = list(find_by_type(extracted, Strong))
        bold_texts = ["".join(getattr(c, "value", "") for c in b.children) for b in bolds]
        assert any("bold" in t.lower() for t in bold_texts), (
            f"expected 'bold' text detected as Strong; got {bold_texts}"
        )

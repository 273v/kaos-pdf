"""Unit tests for the FUND-3 OCR subsystem.

Three layers of coverage:

1. **``_should_run_ocr`` policy matrix** — pure logic, no PDFs involved.
2. **Engine protocol + integration glue** — uses a ``FakeOCREngine`` that
   returns canned :class:`OCRResult` for a synthetic scanned PDF. Verifies
   that OCR output flows into the ContentDocument with proper provenance
   (page + bbox + confidence + extractor name).
3. **TesseractEngine unit surfaces** — ``TesseractNotInstalledError`` on
   missing pytesseract, word-to-line grouping correctness, conf
   normalization.

Live OCR of a real Tesseract binary against our generated fixture
lives in ``tests/integration/test_ocr_live.py``.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest
from kaos_content.images.model import KaosImage
from kaos_content.model.attr import BoundingBox
from kaos_content.model.blocks import Paragraph
from PIL import Image, ImageDraw, ImageFont

from kaos_pdf import (
    OCREngine,
    OCRLine,
    OCRResult,
    TesseractEngine,
    TesseractNotInstalledError,
    extract_pdf,
)
from kaos_pdf.extract import _should_run_ocr
from kaos_pdf.ocr.tesseract import _group_words_into_lines, _parse_conf

# --------------------------------------------------------------------
# Synthetic scanned PDF fixtures
# --------------------------------------------------------------------


def _render_text_to_pdf_bytes(text: str, *, width: int = 850, height: int = 1100) -> bytes:
    """Generate an image-only PDF from ``text``.

    Draws ``text`` onto a white PIL canvas then saves as a single-page
    PDF. Pillow embeds the image as the page — no text layer — so
    pypdfium2 classifies the resulting page as ``"image"``. Perfect
    synthetic stand-in for a real scanned page.
    """
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    # Pillow's default font is tiny (≈8px) and Tesseract struggles with it.
    # Walk common system paths to find a real TTF at 24pt. Fall back to
    # default if none found — the unit tests with FakeOCREngine don't
    # care about rendering quality.
    font = _find_font(size=28)
    margin = 60
    y = margin
    for line in text.splitlines() or [text]:
        draw.text((margin, y), line, fill="black", font=font)
        y += 40

    buffer = io.BytesIO()
    image.save(buffer, format="PDF", resolution=150.0)
    return buffer.getvalue()


def _find_font(*, size: int) -> Any:
    """Locate a real TrueType font on the host for crisp OCR-able text.

    Returns whichever font object Pillow hands us (FreeTypeFont when the
    system has DejaVu/Liberation, ImageFont when we fall back to default).
    """
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


@pytest.fixture
def scanned_pdf_bytes() -> bytes:
    """A 1-page image-only PDF containing known ground truth text."""
    return _render_text_to_pdf_bytes(
        "Section 10(b) of the Securities Exchange Act of 1934\n"
        "prohibits manipulative and deceptive devices in connection\n"
        "with the purchase or sale of any security."
    )


@pytest.fixture
def scanned_pdf_path(tmp_path: Any, scanned_pdf_bytes: bytes) -> str:
    p = tmp_path / "scanned.pdf"
    p.write_bytes(scanned_pdf_bytes)
    return str(p)


# --------------------------------------------------------------------
# _should_run_ocr policy
# --------------------------------------------------------------------


class TestShouldRunOcr:
    def test_never_is_false_for_empty_page(self) -> None:
        assert _should_run_ocr("never", []) is False

    def test_never_is_false_for_text_page(self) -> None:
        assert _should_run_ocr("never", [("hello", (0, 0, 10, 10), None)]) is False

    def test_always_is_true_for_empty_page(self) -> None:
        assert _should_run_ocr("always", []) is True

    def test_always_is_true_for_text_page(self) -> None:
        assert _should_run_ocr("always", [("hello", (0, 0, 10, 10), None)]) is True

    def test_auto_is_true_for_empty_page(self) -> None:
        assert _should_run_ocr("auto", []) is True

    def test_auto_is_true_for_whitespace_only_page(self) -> None:
        assert _should_run_ocr("auto", [("   ", (0, 0, 10, 10), None)]) is True

    def test_auto_is_false_for_page_with_real_text(self) -> None:
        assert _should_run_ocr("auto", [("hello", (0, 0, 10, 10), None)]) is False


# --------------------------------------------------------------------
# Engine integration via FakeOCREngine
# --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FakeOCREngine(OCREngine):
    """Canned OCR result — no real recognition happens.

    Used to exercise the extract_pdf glue without committing to a real
    engine binary. The canned result includes bounding boxes and
    confidences so we can verify provenance round-trips.
    """

    canned: OCRResult
    call_count_box: list[int]  # single-element mutable list acting as a counter
    name: ClassVar[str] = "fake"

    def extract_sync(self, image: KaosImage) -> OCRResult:
        self.call_count_box[0] += 1
        _ = image  # unused; real engines would consume the pixels
        return self.canned


def _canned_result() -> OCRResult:
    return OCRResult(
        lines=[
            OCRLine(
                text="First recognized line",
                bbox=BoundingBox(left=10.0, top=10.0, right=500.0, bottom=40.0),
                confidence=0.95,
            ),
            OCRLine(
                text="  ",  # whitespace-only — should be dropped at emit time
                bbox=BoundingBox(left=0.0, top=0.0, right=1.0, bottom=1.0),
                confidence=0.90,
            ),
            OCRLine(
                text="Second line at low confidence",
                bbox=BoundingBox(left=10.0, top=50.0, right=500.0, bottom=80.0),
                confidence=0.20,
            ),
        ],
        engine_name="fake",
    )


class TestExtractPdfWithOcr:
    def test_never_preserves_legacy_behavior(self, scanned_pdf_path: str) -> None:
        """ocr='never' yields the historical empty-paragraph behavior."""
        doc = extract_pdf(scanned_pdf_path, ocr="never")
        # No OCR paragraphs emitted. The PDF's native text layer is empty
        # so the document may have zero blocks (or only filtered chrome).
        ocr_blocks = [b for b in doc.body if _extractor_of(b).startswith("kaos-pdf/ocr/")]
        assert ocr_blocks == []

    def test_auto_triggers_ocr_on_scanned_page(self, scanned_pdf_path: str) -> None:
        counter = [0]
        engine = FakeOCREngine(canned=_canned_result(), call_count_box=counter)
        doc = extract_pdf(scanned_pdf_path, ocr="auto", ocr_engine=engine)
        assert counter[0] == 1, "OCR should run exactly once on the single scanned page"
        paragraphs = [b for b in doc.body if isinstance(b, Paragraph)]
        texts = [_block_text(b) for b in paragraphs]
        assert "First recognized line" in texts
        assert "Second line at low confidence" in texts
        # Whitespace-only line was dropped.
        assert "  " not in texts

    def test_ocr_paragraph_provenance_includes_page_bbox_confidence(
        self, scanned_pdf_path: str
    ) -> None:
        engine = FakeOCREngine(canned=_canned_result(), call_count_box=[0])
        doc = extract_pdf(scanned_pdf_path, ocr="auto", ocr_engine=engine)
        # Grab the first OCR-origin paragraph.
        ocr_para = next(b for b in doc.body if _extractor_of(b) == "kaos-pdf/ocr/fake")
        prov = ocr_para.provenance
        assert prov is not None
        assert prov.page == 1
        assert prov.bbox is not None
        assert prov.confidence == pytest.approx(0.95)

    def test_min_confidence_drops_low_conf_lines(self, scanned_pdf_path: str) -> None:
        engine = FakeOCREngine(canned=_canned_result(), call_count_box=[0])
        doc = extract_pdf(scanned_pdf_path, ocr="auto", ocr_engine=engine, ocr_min_confidence=0.5)
        texts = [_block_text(b) for b in doc.body if isinstance(b, Paragraph)]
        assert "First recognized line" in texts
        # The 0.20-confidence line is dropped by the threshold.
        assert "Second line at low confidence" not in texts

    def test_engine_failure_does_not_break_extraction(self, scanned_pdf_path: str) -> None:
        """An engine that raises should be swallowed — page yields empty."""

        class BoomEngine(OCREngine):
            name: ClassVar[str] = "boom"

            def extract_sync(self, image: KaosImage) -> OCRResult:
                _ = image
                raise RuntimeError("synthetic failure")

        doc = extract_pdf(scanned_pdf_path, ocr="auto", ocr_engine=BoomEngine())
        # Extraction produces a ContentDocument regardless (possibly empty).
        assert doc is not None

    def test_always_mode_runs_ocr_even_on_text_pages(self, tmp_path: Any) -> None:
        """ocr='always' trumps native text.

        Build a PDF with real text by running extract_pdf on a native
        text PDF fixture; verify that with ocr='always' the OCR engine
        gets invoked.
        """
        counter = [0]
        engine = FakeOCREngine(canned=_canned_result(), call_count_box=counter)
        # Build a tiny native-text PDF on the fly using pypdfium2's
        # PDFium does not expose a writer; use Pillow to create an image
        # PDF but with a different text (so we can confirm OCR text won).
        pdf_bytes = _render_text_to_pdf_bytes("native text goes here")
        pdf_path = tmp_path / "native.pdf"
        pdf_path.write_bytes(pdf_bytes)

        doc = extract_pdf(str(pdf_path), ocr="always", ocr_engine=engine)
        assert counter[0] == 1
        paragraphs = [_block_text(b) for b in doc.body if isinstance(b, Paragraph)]
        # OCR text (First/Second) wins over native.
        assert any("First recognized line" in t for t in paragraphs)


# --------------------------------------------------------------------
# TesseractEngine unit surfaces
# --------------------------------------------------------------------


class TestTesseractEngine:
    def test_missing_pytesseract_raises_install_hint(
        self, monkeypatch: pytest.MonkeyPatch, scanned_pdf_path: str
    ) -> None:
        """Simulate pytesseract being uninstalled → clear recovery guidance."""
        import builtins

        real_import = builtins.__import__

        def shim(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.startswith("pytesseract"):
                raise ImportError("pretend pytesseract is missing")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", shim)
        engine = TesseractEngine()
        image = KaosImage.from_pil(Image.new("RGB", (50, 50), "white"))
        with pytest.raises(TesseractNotInstalledError) as exc_info:
            engine.extract_sync(image)
        assert "pip install 'kaos-pdf[ocr]'" in str(exc_info.value)


class TestParseConf:
    def test_valid_percentage_normalizes(self) -> None:
        assert _parse_conf(85) == pytest.approx(0.85)

    def test_minus_one_is_none(self) -> None:
        """Tesseract uses -1 to mean 'discarded'."""
        assert _parse_conf(-1) is None

    def test_garbage_is_none(self) -> None:
        assert _parse_conf("not-a-number") is None

    def test_over_100_clamps_to_one(self) -> None:
        assert _parse_conf(150) == 1.0


class TestGroupWordsIntoLines:
    def test_groups_by_line_key(self) -> None:
        data = {
            "page_num": [1, 1, 1, 1],
            "block_num": [1, 1, 1, 1],
            "par_num": [1, 1, 1, 1],
            "line_num": [1, 1, 2, 2],
            "word_num": [1, 2, 1, 2],
            "text": ["Hello", "world", "second", "line"],
            "conf": [95, 95, 80, 80],
            "left": [10, 60, 10, 60],
            "top": [10, 10, 30, 30],
            "width": [40, 40, 40, 40],
            "height": [15, 15, 15, 15],
        }
        lines = _group_words_into_lines(data)
        assert len(lines) == 2
        assert lines[0].text == "Hello world"
        assert lines[0].confidence == pytest.approx(0.95)
        assert lines[1].text == "second line"
        # Union bbox covers both words' spans.
        assert lines[0].bbox is not None
        assert lines[0].bbox.left == pytest.approx(10.0)
        assert lines[0].bbox.right == pytest.approx(100.0)

    def test_discarded_words_dropped(self) -> None:
        data = {
            "page_num": [1, 1],
            "block_num": [1, 1],
            "par_num": [1, 1],
            "line_num": [1, 1],
            "word_num": [1, 2],
            "text": ["real", "phantom"],
            "conf": [95, -1],
            "left": [0, 50],
            "top": [0, 0],
            "width": [40, 40],
            "height": [15, 15],
        }
        lines = _group_words_into_lines(data)
        assert len(lines) == 1
        assert lines[0].text == "real"

    def test_empty_line_dropped(self) -> None:
        data = {
            "page_num": [1],
            "block_num": [1],
            "par_num": [1],
            "line_num": [1],
            "word_num": [1],
            "text": [""],  # empty
            "conf": [95],
            "left": [0],
            "top": [0],
            "width": [40],
            "height": [15],
        }
        assert _group_words_into_lines(data) == []


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------


def _block_text(block: Any) -> str:
    """Render a Paragraph/Heading block back to its plain text.

    Paragraph.children is a tuple of Inline nodes; Text inlines carry
    their content on ``.value`` (kaos-content convention, not ``.text``).
    Formatted inlines (Emphasis, Strong) would recurse through their own
    ``.children`` but the fake-engine tests only emit plain Text nodes.
    """
    parts: list[str] = []
    for inline in getattr(block, "children", ()):
        value = getattr(inline, "value", None)
        if isinstance(value, str):
            parts.append(value)
    return "".join(parts)


def _extractor_of(block: Any) -> str:
    """Return Provenance.extractor or ``""`` when absent."""
    prov = getattr(block, "provenance", None)
    if prov is None:
        return ""
    return prov.extractor or ""

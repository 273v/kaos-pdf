"""Tests for garbled-native-layer detection and ``ocr="auto"`` re-OCR.

Three layers:

1. **Quality module** (``kaos_pdf.quality``) — pure string scoring, no PDFs.
2. **Decision gate** (``_is_garbled_scan_layer``) — structural + legibility
   gates, with the structural classifier monkeypatched so the logic is tested
   in isolation.
3. **Pipeline** — ``parse_pdf(..., ocr="auto")`` over real scanned and
   born-digital fixtures, using a ``FakeOCREngine`` so the decision path is
   exercised end-to-end without requiring the Tesseract binary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, cast

import pytest
from kaos_content.images.model import KaosImage
from kaos_content.model.attr import BoundingBox

from kaos_pdf import parse_pdf
from kaos_pdf.extract import _is_garbled_scan_layer
from kaos_pdf.ocr.base import OCREngine, OCRLine, OCRResult
from kaos_pdf.quality import (
    DEFAULT_OCR_QUALITY_THRESHOLD,
    assess_text_quality,
    is_low_quality_layer,
    line_legibility,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pypdfium2 as pdfium

# Representative real text. The garbled string is the actual native text layer
# of the first page of ``staten_v_united_states.pdf`` (a Canon scan); the clean
# string is what that page actually says.
_GARBLED = "0RlGlt IAt lJn tbe @nitp! btutts ourt of trs lsims"
_CLEAN = "In the United States Court of Federal Claims"


# ---------------------------------------------------------------------------
# Quality module
# ---------------------------------------------------------------------------


class TestLineLegibility:
    def test_clean_text_scores_high(self) -> None:
        assert line_legibility(_CLEAN) >= 0.9

    def test_garbled_text_scores_low(self) -> None:
        assert line_legibility(_GARBLED) < 0.2

    def test_empty_or_tokenless_returns_one(self) -> None:
        assert line_legibility("") == 1.0
        assert line_legibility("   ") == 1.0
        assert line_legibility("123 45.6 §") == 1.0  # no alphabetic tokens

    def test_markup_is_stripped_before_scoring(self) -> None:
        assert line_legibility("# **United** States _Court_") >= 0.9


class TestAssessTextQuality:
    def test_worst_line_drives_page_score(self) -> None:
        # A clean body plus one garbled title line: the page must score as the
        # garbled line, not the average.
        text = f"{_GARBLED}\n{_CLEAN}\nNot for publication today here now"
        q = assess_text_quality(text)
        assert q.score < 0.2
        assert q.substantial_lines == 3
        assert "btutts" in q.worst_line

    def test_no_substantial_lines_scores_one(self) -> None:
        q = assess_text_quality("Page 1\n§ 31.4\n12345")
        assert q.score == 1.0
        assert q.substantial_lines == 0
        assert q.worst_line == ""


class TestIsLowQualityLayer:
    def test_garbled_layer_is_low(self) -> None:
        # A clean body does not rescue a page with one fully-garbled line.
        assert is_low_quality_layer(f"{_CLEAN} filed today\n{_GARBLED}") is True

    def test_clean_layer_is_not_low(self) -> None:
        assert is_low_quality_layer(f"{_CLEAN} filed today here now") is False

    def test_no_substantial_lines_is_not_low(self) -> None:
        assert is_low_quality_layer("Page 1\n12345") is False

    def test_threshold_zero_never_low(self) -> None:
        assert is_low_quality_layer(_GARBLED, threshold=0.0) is False


# ---------------------------------------------------------------------------
# Decision gate: _is_garbled_scan_layer
# ---------------------------------------------------------------------------


def _rects(
    *texts: str,
) -> list[tuple[str, tuple[float, float, float, float], dict[str, object] | None]]:
    return [(t, (0.0, 0.0, 100.0, 10.0 * i), None) for i, t in enumerate(texts, start=1)]


class TestIsGarbledScanLayer:
    @pytest.fixture
    def page(self) -> pdfium.PdfPage:
        # The structural classifier is monkeypatched in every test, so the page
        # object is never inspected — a sentinel is enough.
        return cast("pdfium.PdfPage", object())

    def _patch_class(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setattr("kaos_pdf.extract._classify_page", lambda _page: value)

    def test_garbled_scan_page_triggers(
        self, monkeypatch: pytest.MonkeyPatch, page: pdfium.PdfPage
    ) -> None:
        self._patch_class(monkeypatch, "mixed")
        rects = _rects(_GARBLED, _CLEAN)
        assert _is_garbled_scan_layer(page, rects, DEFAULT_OCR_QUALITY_THRESHOLD) is True

    def test_image_class_also_triggers(
        self, monkeypatch: pytest.MonkeyPatch, page: pdfium.PdfPage
    ) -> None:
        self._patch_class(monkeypatch, "image")
        assert _is_garbled_scan_layer(page, _rects(_GARBLED), DEFAULT_OCR_QUALITY_THRESHOLD) is True

    def test_born_digital_text_page_never_triggers(
        self, monkeypatch: pytest.MonkeyPatch, page: pdfium.PdfPage
    ) -> None:
        # Even with garbled text, a structurally born-digital page is excluded.
        self._patch_class(monkeypatch, "text")
        assert (
            _is_garbled_scan_layer(page, _rects(_GARBLED), DEFAULT_OCR_QUALITY_THRESHOLD) is False
        )

    def test_clean_scan_layer_not_re_ocrd(
        self, monkeypatch: pytest.MonkeyPatch, page: pdfium.PdfPage
    ) -> None:
        self._patch_class(monkeypatch, "mixed")
        rects = _rects(f"{_CLEAN} filed today", "An order of the court here now")
        assert _is_garbled_scan_layer(page, rects, DEFAULT_OCR_QUALITY_THRESHOLD) is False

    def test_non_positive_threshold_disables(
        self, monkeypatch: pytest.MonkeyPatch, page: pdfium.PdfPage
    ) -> None:
        self._patch_class(monkeypatch, "mixed")
        assert _is_garbled_scan_layer(page, _rects(_GARBLED), 0.0) is False


# ---------------------------------------------------------------------------
# Pipeline behavior via parse_pdf + FakeOCREngine (no Tesseract needed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FakeOCREngine(OCREngine):
    """Records invocations and returns one canned line."""

    call_count_box: list[int]
    name: ClassVar[str] = "fake"

    def extract_sync(self, image: KaosImage) -> OCRResult:
        self.call_count_box[0] += 1
        _ = image
        return OCRResult(
            lines=[
                OCRLine(
                    text="re-ocr line",
                    bbox=BoundingBox(left=0.0, top=0.0, right=10.0, bottom=10.0),
                    confidence=0.9,
                )
            ],
            engine_name="fake",
        )


@pytest.fixture
def garbled_scan_pdf(fixtures_dir: Path) -> Path:
    """Canon scan whose first page ships a garbled native OCR layer."""
    return fixtures_dir / "staten_v_united_states.pdf"


class TestAutoReOcrPipeline:
    def test_auto_re_ocrs_garbled_scanned_page(self, garbled_scan_pdf: Path) -> None:
        counter = [0]
        engine = FakeOCREngine(call_count_box=counter)
        parse_pdf(str(garbled_scan_pdf), pages=[0], ocr="auto", ocr_engine=engine)
        assert counter[0] == 1, "garbled scanned page should be re-OCR'd under auto"

    def test_threshold_zero_restores_legacy_auto(self, garbled_scan_pdf: Path) -> None:
        counter = [0]
        engine = FakeOCREngine(call_count_box=counter)
        parse_pdf(
            str(garbled_scan_pdf),
            pages=[0],
            ocr="auto",
            ocr_engine=engine,
            ocr_quality_threshold=0.0,
        )
        assert counter[0] == 0, "threshold 0 disables garbled-layer detection"

    def test_never_does_not_re_ocr(self, garbled_scan_pdf: Path) -> None:
        counter = [0]
        engine = FakeOCREngine(call_count_box=counter)
        parse_pdf(str(garbled_scan_pdf), pages=[0], ocr="never", ocr_engine=engine)
        assert counter[0] == 0

    def test_born_digital_page_not_re_ocrd(self, federal_register_pdf: Path) -> None:
        counter = [0]
        engine = FakeOCREngine(call_count_box=counter)
        parse_pdf(str(federal_register_pdf), pages=[0], ocr="auto", ocr_engine=engine)
        assert counter[0] == 0, "born-digital text page must never be re-OCR'd"

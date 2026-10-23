"""Page furniture on OCRed pages (CM/ECF bands, running page numbers) is removed."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import pytest
from kaos_content.images.model import KaosImage
from kaos_content.model.attr import BoundingBox
from kaos_content.model.blocks import Paragraph

from kaos_pdf import extract_pdf
from kaos_pdf.ocr import OCREngine, OCRLine, OCRResult

reportlab = pytest.importorskip("reportlab")


def _blank_pdf(path: str, pages: int) -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(path, pagesize=letter)
    for _ in range(pages):
        c.showPage()
    c.save()


@dataclass
class PagedFakeEngine(OCREngine):
    """Returns a CM/ECF stamp band, a body line and a bare page number per call (page)."""

    pages: int
    calls: list[int] = field(default_factory=lambda: [0])
    name: ClassVar[str] = "paged-fake"

    def extract_sync(self, image: KaosImage) -> OCRResult:
        self.calls[0] += 1
        n = self.calls[0]
        w, h = image.width, image.height
        return OCRResult(
            lines=[
                OCRLine(
                    text=(
                        f"Case 1:13-cv-00133 Document 244 Filed 01/08/14 "
                        f"Page {n} of {self.pages} PageID #: 99{n}"
                    ),
                    bbox=BoundingBox(left=0.1 * w, top=0.02 * h, right=0.9 * w, bottom=0.035 * h),
                    confidence=0.9,
                ),
                OCRLine(
                    text=f"The court has considered the motion on page {n} and the record.",
                    bbox=BoundingBox(left=0.1 * w, top=0.4 * h, right=0.9 * w, bottom=0.42 * h),
                    confidence=0.9,
                ),
                OCRLine(
                    text=str(n),
                    bbox=BoundingBox(left=0.49 * w, top=0.95 * h, right=0.51 * w, bottom=0.965 * h),
                    confidence=0.9,
                ),
            ],
            engine_name="paged-fake",
        )


def _texts(doc) -> list[str]:
    out = []
    for b in doc.body:
        if isinstance(b, Paragraph):
            out.append(" ".join(getattr(c, "value", "") for c in b.children))
    return out


def test_ocr_pages_lose_ecf_bands_and_page_numbers(tmp_path) -> None:
    pdf = str(tmp_path / "scan.pdf")
    _blank_pdf(pdf, 3)
    doc = extract_pdf(pdf, ocr="always", ocr_engine=PagedFakeEngine(pages=3))
    texts = _texts(doc)
    assert not any("Document 244" in t for t in texts)
    assert not any(t.strip() in ("1", "2", "3") for t in texts)
    assert sum("has considered the motion" in t for t in texts) == 3
    removed = doc.metadata.extra.get("furniture_removed") or {}
    assert sum(removed.values()) == 6


def test_single_ocr_page_ecf_band_removed(tmp_path) -> None:
    pdf = str(tmp_path / "scan1.pdf")
    _blank_pdf(pdf, 1)
    doc = extract_pdf(pdf, ocr="always", ocr_engine=PagedFakeEngine(pages=1))
    texts = _texts(doc)
    assert not any("Document 244" in t for t in texts)
    assert any("has considered the motion" in t for t in texts)

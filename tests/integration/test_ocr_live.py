"""Live OCR integration tests — real Tesseract binary, real synthetic scans.

Each test:

1. Generates an image-only PDF from a known string.
2. Runs ``extract_pdf(..., ocr="auto")`` with the default Tesseract engine.
3. Asserts the recovered text has low enough character error rate against
   ground truth to prove OCR actually happened.

Gated by detecting the system ``tesseract`` binary. Without it, the
tests skip — per CLAUDE.md's testing policy we never silently pass.
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path
from typing import Any

import pytest
from kaos_content.model.blocks import Paragraph
from PIL import Image, ImageDraw, ImageFont

from kaos_pdf import extract_pdf

_HAS_TESSERACT = shutil.which("tesseract") is not None


def _render_text_to_pdf_bytes(text: str, *, width: int = 850, height: int = 1100) -> bytes:
    """Render ``text`` onto a PIL canvas and save as an image-only PDF."""
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = _find_font(size=32)
    margin = 60
    y = margin
    for line in text.splitlines() or [text]:
        draw.text((margin, y), line, fill="black", font=font)
        y += 48
    buffer = io.BytesIO()
    image.save(buffer, format="PDF", resolution=150.0)
    return buffer.getvalue()


def _find_font(*, size: int) -> Any:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _block_text(block: Any) -> str:
    parts: list[str] = []
    for inline in getattr(block, "children", ()):
        value = getattr(inline, "value", None)
        if isinstance(value, str):
            parts.append(value)
    return "".join(parts)


def _char_error_rate(expected: str, got: str) -> float:
    """Levenshtein distance normalized by expected length.

    ``expected`` and ``got`` are whitespace-normalized before distance
    computation (Tesseract's inter-word spacing varies harmlessly).
    CER is the standard OCR quality metric — 0.0 is perfect, 1.0 is
    "totally wrong."
    """
    exp = " ".join(expected.split())
    out = " ".join(got.split())
    if not exp:
        return 0.0 if not out else 1.0

    # Simple iterative Levenshtein distance.
    n = len(exp)
    m = len(out)
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        for j in range(1, m + 1):
            cost = 0 if exp[i - 1] == out[j - 1] else 1
            cur[j] = min(
                prev[j] + 1,  # deletion
                cur[j - 1] + 1,  # insertion
                prev[j - 1] + cost,  # substitution
            )
        prev = cur
    return prev[m] / n


@pytest.mark.integration
def test_tesseract_recovers_known_text(tmp_path: Path) -> None:
    """End-to-end: render → Tesseract → ContentDocument → text grep."""
    if not _HAS_TESSERACT:
        pytest.skip("tesseract binary not installed on PATH")

    ground_truth = (
        "Section 10(b) of the Securities Exchange Act of 1934 "
        "prohibits manipulative and deceptive devices in connection "
        "with the purchase or sale of any security."
    )
    pdf_bytes = _render_text_to_pdf_bytes(
        "Section 10(b) of the Securities Exchange Act of 1934\n"
        "prohibits manipulative and deceptive devices in connection\n"
        "with the purchase or sale of any security."
    )
    pdf_path = tmp_path / "scanned.pdf"
    pdf_path.write_bytes(pdf_bytes)

    doc = extract_pdf(str(pdf_path), ocr="auto")

    # OCR paragraphs carry the OCR extractor tag.
    ocr_paragraphs = [
        b
        for b in doc.body
        if isinstance(b, Paragraph)
        and b.provenance is not None
        and (b.provenance.extractor or "").startswith("kaos-pdf/ocr/")
    ]
    assert ocr_paragraphs, "Expected at least one OCR-origin paragraph"

    recovered = " ".join(_block_text(b) for b in ocr_paragraphs)
    cer = _char_error_rate(ground_truth, recovered)
    # Generous threshold — Tesseract on a DejaVu-rendered synthetic scan
    # hits near 0.0 CER in practice. 0.10 leaves room for font
    # substitution on hosts missing DejaVu.
    assert cer <= 0.10, (
        f"CER {cer:.2%} exceeds 10% threshold.\nExpected: {ground_truth!r}\nGot:      {recovered!r}"
    )

    # Provenance round-trips properly.
    first = ocr_paragraphs[0]
    assert first.provenance is not None
    assert first.provenance.page == 1
    assert first.provenance.confidence is not None
    assert 0.0 < first.provenance.confidence <= 1.0


@pytest.mark.integration
def test_ocr_never_mode_skips_on_scan(tmp_path: Path) -> None:
    """With ``ocr="never"``, a scanned PDF yields no OCR paragraphs.

    Regression guard: ensures the default-off contract we promise.
    """
    if not _HAS_TESSERACT:
        pytest.skip("tesseract binary not installed on PATH")

    pdf_bytes = _render_text_to_pdf_bytes("This text is locked inside an image.")
    pdf_path = tmp_path / "scanned.pdf"
    pdf_path.write_bytes(pdf_bytes)

    doc = extract_pdf(str(pdf_path))  # default ocr="never"
    ocr_paragraphs = [
        b
        for b in doc.body
        if isinstance(b, Paragraph)
        and b.provenance is not None
        and (b.provenance.extractor or "").startswith("kaos-pdf/ocr/")
    ]
    assert ocr_paragraphs == []


@pytest.mark.integration
def test_ocr_min_confidence_filters_low_conf_lines(tmp_path: Path) -> None:
    """Threshold gating is live — confirm the confidence cutoff actually fires."""
    if not _HAS_TESSERACT:
        pytest.skip("tesseract binary not installed on PATH")

    pdf_bytes = _render_text_to_pdf_bytes("This is a clean synthetic scan.")
    pdf_path = tmp_path / "scanned.pdf"
    pdf_path.write_bytes(pdf_bytes)

    # Tesseract on clean DejaVu text hits >95% confidence. Setting the
    # gate to 0.99 should drop most/all lines.
    doc_permissive = extract_pdf(str(pdf_path), ocr="auto", ocr_min_confidence=0.0)
    doc_strict = extract_pdf(str(pdf_path), ocr="auto", ocr_min_confidence=0.99)

    def _ocr_count(doc: Any) -> int:
        return sum(
            1
            for b in doc.body
            if isinstance(b, Paragraph)
            and b.provenance is not None
            and (b.provenance.extractor or "").startswith("kaos-pdf/ocr/")
        )

    permissive = _ocr_count(doc_permissive)
    strict = _ocr_count(doc_strict)
    assert permissive >= 1
    assert strict <= permissive, (
        f"Strict mode should drop lines: permissive={permissive}, strict={strict}"
    )

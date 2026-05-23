"""Defense-in-depth byte guard in ``kaos_pdf.extract_pdf`` (audit Fix 4).

When kaos-nlp-core 0.1.1+ is installed, ``extract_pdf`` rejects
non-PDF input with a typed :class:`PdfExtractionError` (carrying
``what`` / ``how_to_fix`` / ``alternative_tool``) BEFORE pypdfium2
sees the bytes. Pre-guard, mis-routed DOCX / PPTX / PNG bytes died
deep inside the C parser with an opaque error like ``invalid object
stream``; direct-Python callers had no way to know what they'd
actually passed in.

These tests are gated by ``pytest.importorskip("kaos_nlp_core.
content_type")`` — the kaos-pdf base install does not require
kaos-nlp-core, so when it's absent at runtime the guard is a no-op
(preserving legacy behavior) and the test isn't applicable.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("kaos_nlp_core.content_type")

from kaos_pdf import parse_pdf
from kaos_pdf.errors import PdfExtractionError


def _minimal_docx_bytes() -> bytes:
    """A valid OPC zip that kaos-nlp-core 0.1.1+ classifies as office-docx.

    Same shape as the synthetic helper in kaos-nlp-core's own test
    suite — the ``[Content_Types].xml`` Override is the marker the
    OPC fallback greps for.
    """
    buf = io.BytesIO()
    content_types_xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="xml" ContentType="application/xml"/>'
        b'<Override PartName="/word/document.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        b"</Types>"
    )
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("word/document.xml", b"<doc/>")
    return buf.getvalue()


class TestByteGuard:
    def test_docx_renamed_pdf_raises_typed_error(self, tmp_path: Path) -> None:
        """A DOCX renamed ``report.pdf`` must fail at the guard, NOT
        deep in pypdfium2. The error carries the detected group so
        downstream callers can route correctly.

        With kaos-nlp-core 0.1.1+ the detector's OPC fallback classifies
        the synthetic DOCX as ``office-docx`` (and the guard's
        ``alternative_tool`` points at ``kaos-office.parse_docx``).
        With 0.1.0 the detector reports the underlying zip container as
        ``archive`` and the guard still refuses with a typed error —
        less specific alternative_tool but the contract (don't crash
        in pypdfium2) holds. Assert the contract; tolerate either
        upstream classification."""
        spoofed = tmp_path / "report.pdf"
        spoofed.write_bytes(_minimal_docx_bytes())

        with pytest.raises(PdfExtractionError) as exc:
            parse_pdf(spoofed)

        details = exc.value.details
        # Guard fired (the load-bearing contract):
        assert details.get("detected_group") not in (None, "pdf"), (
            f"guard did not refuse spoofed DOCX; details: {details}"
        )
        # 0.1.1+ refinement: alternative_tool points to the office parser.
        if details.get("detected_group") == "office-docx":
            assert "kaos-office" in (details.get("alternative_tool") or "")
        # Filename surfaced in the message for human triage.
        assert "report.pdf" in str(exc.value)

    def test_png_renamed_pdf_raises_typed_error(self, tmp_path: Path) -> None:
        """Image bytes carried by a `.pdf` filename — same guard."""
        png = tmp_path / "diagram.pdf"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)

        with pytest.raises(PdfExtractionError) as exc:
            parse_pdf(png)

        details = exc.value.details
        assert details.get("detected_group") == "image"

    def test_real_pdf_passes_guard(self, tmp_path: Path) -> None:
        """A genuine PDF must NOT raise — the guard is strictly additive."""
        pdf = tmp_path / "real.pdf"
        # Minimal valid PDF skeleton so the guard accepts it without
        # invoking pypdfium2's full parse path (we test the guard
        # alone here; full parser exercise lives in test_extract.py).
        pdf.write_bytes(
            b"%PDF-1.4\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Count 0/Kids[]>>endobj\n"
            b"trailer<</Root 1 0 R>>\n"
            b"%%EOF\n"
        )
        # We don't assert success-of-parse (the synthetic PDF is too
        # minimal for full extraction); we assert the guard didn't
        # intercept — anything raised must NOT be a PdfExtractionError
        # carrying detected_group from our helper.
        try:
            parse_pdf(pdf)
        except PdfExtractionError as exc:
            assert "detected_group" not in exc.details, (
                "byte guard misfired on a real PDF — should have passed "
                f"through. Details: {exc.details}"
            )
        except Exception:
            # Any other parser error is fine for this test's purpose.
            pass

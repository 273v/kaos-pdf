"""OCR subsystem for kaos-pdf — run OCR on rendered pages of scanned PDFs.

The OCR layer closes the largest silent-failure mode in kaos-pdf:
``extract_pdf()`` on a scanned (image-only) PDF used to emit a
``ContentDocument`` with empty :class:`~kaos_content.model.block.Paragraph`
blocks. With OCR wired in, the same call returns real prose with full
provenance (page + bbox + confidence) so downstream consumers (the
FUND-1 verifier, the reviewer UI, the kaos-agents extraction loop)
can work against scanned input the same way they work against
born-digital PDFs.

Design highlights:

- **Pluggable engine protocol.** Any class implementing
  :class:`OCREngine` can be plugged in. Tesseract ships as the default
  (Apache-2.0, zero-config for anyone with the CLI installed).
  PaddleOCR lands under the ``[paddle]`` extra as a follow-up.
- **Sync + async surface.** Each engine implements ``extract_sync()``
  and ``extract()``; the ``extract_pdf()`` sync API calls the sync
  variant so we don't break the existing caller contract. Concurrent
  callers can await ``extract()`` to offload to the default executor.
- **Confidence on every line.** OCR outputs per-line confidence scores
  that flow all the way through to
  :attr:`kaos_content.model.attr.Provenance.confidence`, so the
  verifier can weight OCR-derived text differently from born-digital
  text.

See :mod:`docs/design/fund-3-ocr-pipeline.md` for the full design.
"""

from __future__ import annotations

from kaos_pdf.ocr.base import OCREngine, OCRLine, OCRResult
from kaos_pdf.ocr.rapidocr import RapidOcrEngine, RapidOcrNotInstalledError
from kaos_pdf.ocr.tesseract import TesseractEngine, TesseractNotInstalledError

__all__ = [
    "OCREngine",
    "OCRLine",
    "OCRResult",
    "RapidOcrEngine",
    "RapidOcrNotInstalledError",
    "TesseractEngine",
    "TesseractNotInstalledError",
    "get_default_engine",
]


def get_default_engine() -> OCREngine:
    """Return the default OCR engine.

    Currently always returns a :class:`TesseractEngine`. If Tesseract
    isn't installed on the host, the engine is still constructable —
    errors surface at ``extract()`` time with clear recovery guidance.
    Future: auto-detect Paddle / dots.OCR if the optional extras are
    installed and fall back to Tesseract.
    """
    return TesseractEngine()

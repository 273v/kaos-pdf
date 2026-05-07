"""Tesseract OCR engine — Apache-2.0, CPU-only, universally available.

Tesseract is the baseline engine we ship by default. It requires the
system ``tesseract`` binary (from
https://github.com/tesseract-ocr/tesseract) plus the ``pytesseract``
Python package. Both are in the ``[ocr]`` extra of kaos-pdf.

Why Tesseract first:

- Apache-2.0 license (Surya's GPL-3.0 is disqualified from our stack).
- Pre-installed on most legal-tech dev machines; packaged by every
  major Linux distro.
- Good-enough accuracy on clean scanned prose (~97% character accuracy
  on English).
- No GPU, no model weights, no Internet fetch at runtime.

It loses to PaddleOCR / dots.OCR on layout fidelity and non-English —
the ``[paddle]`` extra will add those as pluggable alternatives in a
follow-up.
"""

from __future__ import annotations

from typing import Any, ClassVar

from kaos_content.images.model import KaosImage
from kaos_content.model.attr import BoundingBox
from kaos_core.logging import get_logger

from kaos_pdf.ocr.base import OCREngine, OCRLine, OCRResult

logger = get_logger(__name__)


class TesseractNotInstalledError(RuntimeError):
    """Raised when Tesseract isn't available.

    Distinct from a transient OCR failure so callers can surface a
    clear install message. Inherits ``RuntimeError`` (not
    ``EnvironmentError``) because the failure mode is "tool missing",
    not "network down".
    """


class TesseractEngine(OCREngine):
    """OCR via the ``pytesseract`` wrapper over the Tesseract binary."""

    name: ClassVar[str] = "tesseract"

    def __init__(
        self,
        *,
        lang: str = "eng",
        config: str = "",
        oem: int = 3,
        psm: int = 6,
    ) -> None:
        """
        Args:
            lang: Tesseract language code, e.g. ``"eng"``, ``"eng+fra"``.
                See ``tesseract --list-langs`` on the host for installed
                language packs. Default English — the common case for
                US legal documents.
            config: Extra Tesseract config string (e.g.
                ``"-c preserve_interword_spaces=1"``). Appended verbatim
                to the pytesseract ``config=`` kwarg.
            oem: OCR Engine Mode. ``3`` (default) = default, based on
                what's available. Other values: ``0`` = legacy only,
                ``1`` = neural net LSTM, ``2`` = legacy + LSTM.
            psm: Page Segmentation Mode. ``6`` (default) = "assume a
                single uniform block of text." Good for scanned
                paragraphs. Switch to ``3`` for auto layout detection,
                ``11`` for sparse text.
        """
        self._lang = lang
        self._config = config
        self._oem = oem
        self._psm = psm

    def extract_sync(self, image: KaosImage) -> OCRResult:
        """Run Tesseract on ``image`` and return lines + confidences.

        Uses pytesseract's ``image_to_data(output_type=DATAFRAME)`` is
        tempting but pulls in pandas — we instead use the ``DICT``
        output shape which keeps us dependency-free beyond pytesseract
        itself.
        """
        try:
            import pytesseract  # type: ignore[import-not-found]
            from pytesseract import Output  # type: ignore[import-not-found]
        except ImportError as exc:
            raise TesseractNotInstalledError(
                "pytesseract is not installed. Fix: pip install "
                "'kaos-pdf[ocr]'. "
                "Alternative: pass ocr='never' to extract_pdf() or "
                "register a different OCREngine."
            ) from exc

        tess_config = f"--oem {self._oem} --psm {self._psm}"
        if self._config:
            tess_config = f"{tess_config} {self._config}"

        try:
            raw = pytesseract.image_to_data(
                image.pil,
                lang=self._lang,
                config=tess_config,
                output_type=Output.DICT,
            )
        except pytesseract.TesseractNotFoundError as exc:  # type: ignore[attr-defined]
            raise TesseractNotInstalledError(
                "The Tesseract binary is not on PATH. Fix: install "
                "tesseract (e.g. 'apt install tesseract-ocr' or "
                "'brew install tesseract'). "
                "Alternative: pass ocr='never' to extract_pdf()."
            ) from exc

        lines = _group_words_into_lines(raw)
        return OCRResult(lines=lines, engine_name=self.name)


def _group_words_into_lines(data: dict[str, Any]) -> list[OCRLine]:
    """Combine pytesseract's word-level output into lines.

    pytesseract's ``image_to_data`` returns per-word records keyed by
    ``(page_num, block_num, par_num, line_num, word_num)``. Group by
    the first four to reconstruct visual lines, then compute a tight
    bounding box and mean confidence.

    Low-confidence words (conf == -1, meaning Tesseract discarded
    them) are skipped. Empty lines (all words discarded) are omitted.
    """
    keys = ("page_num", "block_num", "par_num", "line_num")
    texts = data.get("text", [])
    confs = data.get("conf", [])
    lefts = data.get("left", [])
    tops = data.get("top", [])
    widths = data.get("width", [])
    heights = data.get("height", [])
    n = len(texts)

    # Group indices by (page, block, par, line).
    groups: dict[tuple[int, int, int, int], list[int]] = {}
    for i in range(n):
        key = (
            int(data[keys[0]][i]),
            int(data[keys[1]][i]),
            int(data[keys[2]][i]),
            int(data[keys[3]][i]),
        )
        bucket = groups.get(key)
        if bucket is None:
            bucket = []
            groups[key] = bucket
        bucket.append(i)

    lines: list[OCRLine] = []
    for key in sorted(groups):
        indices = groups[key]
        words: list[str] = []
        word_confs: list[float] = []
        bboxes: list[tuple[float, float, float, float]] = []
        for i in indices:
            text = (texts[i] or "").strip()
            if not text:
                continue
            conf = _parse_conf(confs[i])
            if conf is None:
                continue  # Tesseract discarded this word
            words.append(text)
            word_confs.append(conf)
            left = float(lefts[i])
            top = float(tops[i])
            bboxes.append((left, top, left + float(widths[i]), top + float(heights[i])))
        if not words:
            continue
        line_text = " ".join(words)
        mean_conf = sum(word_confs) / len(word_confs)
        line_bbox = _union_bbox(bboxes)
        lines.append(OCRLine(text=line_text, bbox=line_bbox, confidence=mean_conf))
    return lines


def _parse_conf(raw_conf: Any) -> float | None:
    """Normalize Tesseract conf (0-100 int, -1 for discarded) to [0,1]."""
    try:
        value = float(raw_conf)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return max(0.0, min(1.0, value / 100.0))


def _union_bbox(boxes: list[tuple[float, float, float, float]]) -> BoundingBox:
    """Compute the tight union of ``(left, top, right, bottom)`` boxes."""
    lefts = [b[0] for b in boxes]
    tops = [b[1] for b in boxes]
    rights = [b[2] for b in boxes]
    bottoms = [b[3] for b in boxes]
    return BoundingBox(
        left=min(lefts),
        top=min(tops),
        right=max(rights),
        bottom=max(bottoms),
    )


__all__ = ["TesseractEngine", "TesseractNotInstalledError"]

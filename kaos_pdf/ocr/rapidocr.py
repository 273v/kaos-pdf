"""RapidOCR engine — local ONNX OCR via the ``rapidocr`` package.

A higher-accuracy local alternative to Tesseract that runs entirely on
ONNX Runtime (PP-OCRv5 detection + recognition models converted to ONNX).
No PyTorch, no Hugging Face ``transformers`` runtime, no GPU required —
just ``onnxruntime`` on CPU. Both the RapidOCR code and the bundled
PP-OCR models are Apache-2.0 licensed, so this keeps kaos-pdf's
permissive-only dependency posture intact.

Ships behind the ``[onnx]`` extra (``pip install kaos-pdf[onnx]``) and is
imported lazily, so the base package never pulls ``onnxruntime`` /
``opencv-python`` and importing :mod:`kaos_pdf` does no model work. The
ONNX model files download on first use and are cached by ``rapidocr``;
nothing is fetched at import time.

Usage::

    from kaos_pdf import parse_pdf
    from kaos_pdf.ocr import RapidOcrEngine

    doc = parse_pdf("scan.pdf", ocr="always", ocr_engine=RapidOcrEngine())

RapidOCR detects and recognizes text line-by-line and reports a
per-line recognition confidence, which flows through to
:attr:`kaos_content.model.attr.Provenance.confidence` exactly like the
Tesseract path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from kaos_pdf.ocr.base import OCREngine, OCRLine, OCRResult

if TYPE_CHECKING:
    from kaos_content.images.model import KaosImage
    from kaos_content.model.attr import BoundingBox


class RapidOcrNotInstalledError(RuntimeError):
    """Raised when the ``[onnx]`` extra (rapidocr/onnxruntime) is missing.

    Carries actionable install guidance rather than a bare ImportError so
    callers can recover or surface a clear message.
    """

    def __init__(self) -> None:
        super().__init__(
            "RapidOCR is not installed. The local ONNX OCR engine requires "
            "the [onnx] extra.\n"
            "  Fix: pip install 'kaos-pdf[onnx]'  (installs rapidocr + onnxruntime)\n"
            "  Alternative: use the default Tesseract engine "
            "(pip install 'kaos-pdf[ocr]' + the system tesseract binary)."
        )


def _box_to_bbox(box: Any) -> BoundingBox | None:
    """Convert a RapidOCR 4-point polygon to an axis-aligned BoundingBox.

    ``box`` is a sequence of four ``(x, y)`` points (pixel coordinates).
    Returns ``None`` when the polygon can't be interpreted.
    """
    from kaos_content.model.attr import BoundingBox

    try:
        xs = [float(pt[0]) for pt in box]
        ys = [float(pt[1]) for pt in box]
    except (TypeError, ValueError, IndexError):
        return None
    if not xs or not ys:
        return None
    return BoundingBox(left=min(xs), top=min(ys), right=max(xs), bottom=max(ys))


class RapidOcrEngine(OCREngine):
    """OCR engine backed by RapidOCR (ONNX Runtime, PP-OCRv5 models).

    The engine constructs a single ``rapidocr.RapidOCR`` instance and
    reuses it across calls (model load is amortized in ``__init__``).
    Stateless and safe to call from multiple threads — RapidOCR holds no
    per-call mutable state on the public path.
    """

    name: ClassVar[str] = "rapidocr"

    def __init__(self, *, params: dict[str, Any] | None = None) -> None:
        """Construct the engine, loading the ONNX models eagerly.

        Args:
            params: Optional RapidOCR parameter overrides forwarded to
                ``rapidocr.RapidOCR(params=...)`` (e.g. language or model
                selection). ``None`` uses RapidOCR's defaults.

        Raises:
            RapidOcrNotInstalledError: When the ``[onnx]`` extra isn't
                installed.
        """
        try:
            from rapidocr import RapidOCR  # ty: ignore[unresolved-import]
        except ImportError as exc:
            raise RapidOcrNotInstalledError() from exc
        self._engine = RapidOCR(params=params) if params else RapidOCR()

    def extract_sync(self, image: KaosImage) -> OCRResult:
        """Run RapidOCR on ``image`` and return recognized lines.

        The image is handed to RapidOCR as PNG bytes so RapidOCR's own
        loader handles decoding and channel order — we never guess
        RGB-vs-BGR. An image with no detected text yields an empty
        ``OCRResult`` (valid: blank or non-text page).
        """
        png = image.to_bytes(format="png")
        output = self._engine(png)
        return _output_to_result(output, self.name)


def _output_to_result(output: Any, engine_name: str) -> OCRResult:
    """Map a ``rapidocr.RapidOCROutput`` to an :class:`OCRResult`.

    ``output`` carries parallel ``.boxes`` (4-point polygons), ``.txts``
    (strings), and ``.scores`` (recognition confidences in ``[0, 1]``).
    Any of them may be ``None`` when nothing was detected. Pure function,
    independent of the ``rapidocr`` import, so the mapping is unit-testable
    without the ``[onnx]`` extra.
    """
    txts = getattr(output, "txts", None)
    if not txts:
        return OCRResult(lines=[], engine_name=engine_name)

    boxes_attr = getattr(output, "boxes", None)
    scores_attr = getattr(output, "scores", None)
    boxes = list(boxes_attr) if boxes_attr is not None else []
    scores = list(scores_attr) if scores_attr is not None else []

    lines: list[OCRLine] = []
    for i, text in enumerate(txts):
        box = boxes[i] if i < len(boxes) else None
        bbox = _box_to_bbox(box) if box is not None else None
        try:
            confidence = float(scores[i]) if i < len(scores) else 1.0
        except (TypeError, ValueError):
            confidence = 1.0
        # RapidOCR scores are already normalized to [0, 1]; clamp defensively.
        confidence = min(1.0, max(0.0, confidence))
        lines.append(OCRLine(text=str(text), bbox=bbox, confidence=confidence))

    return OCRResult(lines=lines, engine_name=engine_name)


__all__ = ["RapidOcrEngine", "RapidOcrNotInstalledError"]

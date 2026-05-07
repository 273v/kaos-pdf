"""Base types and protocol for OCR engines.

Three moving pieces:

- :class:`OCRLine` — one recognized line: text + bounding box +
  confidence.
- :class:`OCRResult` — the complete output of running an engine on a
  single image: ordered list of lines + engine identifier.
- :class:`OCREngine` — ABC every engine implements. Exposes both a
  sync and async ``extract`` entry point so callers can pick whichever
  matches their execution context.

Kept deliberately small and framework-agnostic — engines that want to
attach extra metadata (e.g. PaddleOCR's table detection, dots.OCR's
formula boxes) carry it in ``OCRResult.extra`` so we don't have to
grow the core types every time.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from kaos_content.images.model import KaosImage
from kaos_content.model.attr import BoundingBox


@dataclass(frozen=True, slots=True)
class OCRLine:
    """One OCR-recognized line of text.

    Attributes:
        text: The recognized string. May contain trailing whitespace;
            callers typically ``.strip()`` when building blocks.
        bbox: Pixel-coordinate bounding box on the source image. ``None``
            when the engine doesn't report box-level geometry (rare;
            Tesseract's ``word_boxes`` mode always produces one).
        confidence: Engine-reported confidence, normalized to ``[0, 1]``.
            Engines that only produce percentages (0-100) MUST normalize
            before constructing :class:`OCRLine`. A value of ``1.0``
            means "no uncertainty reported"; a value of ``0.0`` is a
            real low-confidence signal, NOT a missing value.
    """

    text: str
    bbox: BoundingBox | None
    confidence: float


@dataclass(frozen=True, slots=True)
class OCRResult:
    """Complete OCR output for a single image.

    Attributes:
        lines: Ordered list of recognized lines (engine-defined order,
            typically reading order / top-to-bottom).
        engine_name: Machine identifier of the engine that produced
            this result (e.g. ``"tesseract"``, ``"paddle"``). Echoed
            onto :attr:`kaos_content.model.attr.Provenance.extractor`
            as ``"kaos-pdf/ocr/{engine_name}"``.
        extra: Engine-specific metadata. Not used by the extract_pdf
            pipeline; stored for diagnostics and future enrichers
            (table detection, layout analysis, etc.).
    """

    lines: list[OCRLine]
    engine_name: str
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """All recognized text joined by newlines. Empty string if no lines."""
        return "\n".join(line.text for line in self.lines)

    @property
    def mean_confidence(self) -> float:
        """Mean confidence across lines. ``0.0`` for empty results."""
        if not self.lines:
            return 0.0
        return sum(line.confidence for line in self.lines) / len(self.lines)


class OCREngine(ABC):
    """Abstract base for OCR engines.

    Subclasses MUST implement :meth:`extract_sync`. The default
    :meth:`extract` wraps the sync variant in
    :func:`asyncio.to_thread` so async callers don't block the event
    loop on CPU-bound recognition.

    Engines MUST be idempotent and stateless (safe to call from
    multiple threads). The cost of loading weights once should be
    amortized either at process start (Tesseract — no weights to load)
    or in ``__init__`` (Paddle — load the model there).
    """

    name: ClassVar[str]
    """Machine identifier used for logging + provenance. Lowercase, no
    spaces. Each concrete engine sets this as a class-level attribute."""

    @abstractmethod
    def extract_sync(self, image: KaosImage) -> OCRResult:
        """Synchronously run OCR on ``image``.

        Args:
            image: Rendered PDF page (or any KaosImage). Engines may
                downscale / binarize internally; they MUST NOT mutate
                the input.

        Returns:
            :class:`OCRResult` with recognized lines in reading order.
            Returning an empty ``lines`` list is valid (e.g. truly
            blank page) — callers decide how to treat that.

        Raises:
            OSError: When the engine's underlying binary / model files
                aren't available. Error messages include recovery
                guidance ("install tesseract", etc.).
        """

    async def extract(self, image: KaosImage) -> OCRResult:
        """Async wrapper around :meth:`extract_sync`.

        Default implementation offloads to :func:`asyncio.to_thread`.
        Engines that are native async (e.g. a remote OCR service) can
        override this to avoid the executor roundtrip.
        """
        return await asyncio.to_thread(self.extract_sync, image)


__all__ = ["OCREngine", "OCRLine", "OCRResult"]

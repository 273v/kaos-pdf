"""Base types and protocol for PDF table extraction engines.

Three moving pieces:

- :class:`ExtractedTable` — one table found on a page: rows + bbox +
  header heuristic.
- :class:`TableResult` — the full output of an engine run on a
  (possibly multi-page) PDF.
- :class:`TableEngine` — ABC every engine implements. Sync + async
  surface, same as FUND-3's OCR pluggability.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from kaos_content.model.attr import BoundingBox


@dataclass(frozen=True, slots=True)
class ExtractedTable:
    """One table extracted from a PDF page.

    Attributes:
        page: 1-indexed page number where the table appears.
        bbox: Page-coordinate bounding box. ``None`` if the engine
            doesn't report geometry (rare; pdfplumber always does).
        rows: Row-major tuple of cell tuples. Cells are ``str | None``;
            ``None`` means "blank cell" (engines distinguish this from
            the empty string to preserve column alignment on sparse
            rows).
        has_header: Heuristic flag — ``True`` when the first row is
            likely a header (short labels, no numeric content). The
            ``extract_pdf`` glue feeds this into
            :class:`kaos_content.model.table.TableSection` head/body
            partitioning.
        engine_name: Identifier of the engine that produced the table
            (e.g. ``"pdfplumber"``). Echoed onto
            :class:`~kaos_content.model.attr.Provenance.extractor` as
            ``"kaos-pdf/tables/{engine_name}"``.
        confidence: Optional engine-reported confidence in ``[0, 1]``.
            pdfplumber doesn't produce one natively; we leave
            ``None``. docling / VLM engines will fill this.
    """

    page: int
    bbox: BoundingBox | None
    rows: tuple[tuple[str | None, ...], ...]
    has_header: bool
    engine_name: str
    confidence: float | None = None

    @property
    def column_count(self) -> int:
        """Width of the widest row — engines may emit uneven rows."""
        return max((len(r) for r in self.rows), default=0)

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True, slots=True)
class TableResult:
    """Aggregate output of one engine run.

    Attributes:
        tables: All tables found, in (page, top-to-bottom) order.
        engine_name: Mirrors :attr:`ExtractedTable.engine_name` for
            log / audit convenience.
        extra: Engine-specific metadata (pdfplumber: per-page table
            settings; docling: layout labels). Not consumed by
            extract_pdf; preserved for diagnostics.
    """

    tables: tuple[ExtractedTable, ...]
    engine_name: str
    extra: dict[str, Any] = field(default_factory=dict)


class TableEngine(ABC):
    """Abstract base for PDF table engines.

    Subclasses MUST implement :meth:`extract_sync`. :meth:`extract`
    defaults to ``asyncio.to_thread(extract_sync, …)`` so async
    callers don't block the loop on CPU-bound detection.
    """

    name: ClassVar[str]
    """Machine identifier, lowercase. Echoed onto provenance."""

    @abstractmethod
    def extract_sync(
        self,
        source: str | Path,
        *,
        page_indices: list[int] | None = None,
    ) -> TableResult:
        """Synchronously run table extraction on a PDF.

        Args:
            source: Path to the PDF file.
            page_indices: Optional 0-based page filter. ``None`` means
                all pages.

        Returns:
            :class:`TableResult` with every detected table. Empty
            ``tables`` tuple is valid — the PDF simply has no tables.

        Raises:
            OSError: Engine binary / dep missing. Error messages
                include recovery guidance ("pip install
                'kaos-pdf[tables]'").
        """

    async def extract(
        self,
        source: str | Path,
        *,
        page_indices: list[int] | None = None,
    ) -> TableResult:
        """Async wrapper around :meth:`extract_sync`."""
        return await asyncio.to_thread(self.extract_sync, source, page_indices=page_indices)


__all__ = ["ExtractedTable", "TableEngine", "TableResult"]

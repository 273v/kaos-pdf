"""pdfplumber table engine — MIT, pure Python, no binary deps beyond pdfminer.

pdfplumber's ``page.find_tables()`` gives us:

- Structural detection via pixel-level line / edge inference.
- A ``Table`` object per detection with ``.bbox`` (page-coordinate) and
  ``.extract()`` → ``list[list[str | None]]`` row-major cells.
- Reasonable defaults on both bordered and borderless tables (the
  ``text`` strategy handles whitespace-demarcated tables; ``lines``
  handles ruled tables).

We do NOT depend on Ghostscript (required by camelot-lattice) —
pdfplumber's detection works on the PDF content stream directly.

Header detection is post-processing: pdfplumber doesn't mark header
rows. We apply a label-like heuristic (first-row cells are short and
non-numeric, second row has numeric content) so the downstream kaos-
content ``Table`` block partitions head/body correctly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from kaos_content.model.attr import BoundingBox
from kaos_core.logging import get_logger

from kaos_pdf.tables.base import ExtractedTable, TableEngine, TableResult

logger = get_logger(__name__)


class PdfplumberNotInstalledError(RuntimeError):
    """Raised when pdfplumber isn't importable."""


class PdfplumberEngine(TableEngine):
    """Default table engine — pdfplumber-backed."""

    name: ClassVar[str] = "pdfplumber"

    def __init__(
        self,
        *,
        table_settings: dict[str, Any] | None = None,
    ) -> None:
        """
        Args:
            table_settings: Passed through to pdfplumber's
                ``find_tables(table_settings=...)`` call. ``None``
                picks pdfplumber's defaults, which detect both ruled
                tables and text-aligned tables. Override to tune on a
                specific document layout (e.g. ``{"vertical_strategy":
                "text"}`` for purely borderless tables).
        """
        self._table_settings = table_settings

    def extract_sync(
        self,
        source: str | Path,
        *,
        page_indices: list[int] | None = None,
    ) -> TableResult:
        try:
            import pdfplumber  # type: ignore[import-not-found]
        except ImportError as exc:
            raise PdfplumberNotInstalledError(
                "pdfplumber is not installed. "
                "Fix: pip install 'kaos-pdf[tables]'. "
                "Alternative: pass tables='geometric' to extract_pdf() "
                "for the zero-dep legacy detector."
            ) from exc

        results: list[ExtractedTable] = []
        path = str(Path(source))
        # pdfplumber opens with a context manager to ensure the
        # underlying PDFMiner PDF handle is closed even on error.
        with pdfplumber.open(path) as pdf:
            n = len(pdf.pages)
            targets = page_indices if page_indices is not None else list(range(n))
            for page_idx in targets:
                if page_idx < 0 or page_idx >= n:
                    continue
                page = pdf.pages[page_idx]
                try:
                    # find_tables returns Table objects; extract_tables
                    # collapses to raw rows. We use find_tables so we
                    # keep bboxes for provenance.
                    detected = page.find_tables(table_settings=self._table_settings)
                except Exception as exc:
                    logger.warning(
                        "pdfplumber table detection failed on page %d: %s",
                        page_idx + 1,
                        exc,
                    )
                    continue
                for table in detected:
                    extracted = self._to_extracted_table(table, page_idx)
                    if extracted is not None:
                        results.append(extracted)

        return TableResult(tables=tuple(results), engine_name=self.name)

    def _to_extracted_table(self, table: Any, page_idx: int) -> ExtractedTable | None:
        """Normalize a pdfplumber ``Table`` into our frozen shape."""
        try:
            raw_rows = table.extract()
        except Exception as exc:
            logger.warning(
                "pdfplumber row extraction failed on page %d: %s",
                page_idx + 1,
                exc,
            )
            return None
        if not raw_rows:
            return None

        rows = tuple(tuple(_norm_cell(c) for c in row) for row in raw_rows)
        # Skip degenerate detections (0x0, 1x1 single cell).
        if not rows or all(all(c is None for c in r) for r in rows):
            return None
        if len(rows) == 1 and len(rows[0]) <= 1:
            return None

        bbox = _bbox_from_pdfplumber(table)
        has_header = _looks_like_header_row(rows)

        return ExtractedTable(
            page=page_idx + 1,
            bbox=bbox,
            rows=rows,
            has_header=has_header,
            engine_name=self.name,
        )


def _norm_cell(cell: Any) -> str | None:
    """Normalize a pdfplumber cell to ``str | None``.

    pdfplumber returns ``None`` for blank cells and ``str`` otherwise.
    It can also emit newline-laden strings for multi-line cells; we
    collapse whitespace so downstream consumers get tidy rows without
    losing the cell boundary.
    """
    if cell is None:
        return None
    if not isinstance(cell, str):
        cell = str(cell)
    stripped = " ".join(cell.split())
    return stripped if stripped else None


def _bbox_from_pdfplumber(table: Any) -> BoundingBox | None:
    """Convert pdfplumber's ``(x0, top, x1, bottom)`` tuple to ``BoundingBox``."""
    bbox = getattr(table, "bbox", None)
    if bbox is None:
        return None
    try:
        x0, top, x1, bottom = bbox
    except (TypeError, ValueError):
        return None
    return BoundingBox(
        left=float(x0),
        top=float(top),
        right=float(x1),
        bottom=float(bottom),
    )


def _looks_like_header_row(rows: tuple[tuple[str | None, ...], ...]) -> bool:
    """Heuristic: does ``rows[0]`` look like a header?

    A cell is "label-like" if it's non-empty, short (< 50 chars), and
    doesn't START with a digit. A row is "header-like" if ≥70% of its
    cells are label-like. We also require that the SECOND row has at
    least one numeric-looking cell — pure-prose tables shouldn't be
    partitioned.
    """
    if len(rows) < 2:
        return False
    first = rows[0]
    second = rows[1]

    label_like = 0
    non_empty = 0
    for cell in first:
        if cell is None or not cell.strip():
            continue
        non_empty += 1
        if len(cell) < 50 and not cell.strip()[:1].isdigit():
            label_like += 1
    if non_empty == 0:
        return False
    if label_like / non_empty < 0.7:
        return False

    # Require at least one numeric-looking cell in row 2.
    return any(cell and _looks_numeric(cell) for cell in second)


def _looks_numeric(cell: str) -> bool:
    """Return True if ``cell`` parses as a number (ignoring common noise)."""
    stripped = cell.strip().replace(",", "").replace("$", "").replace("%", "")
    if not stripped:
        return False
    try:
        float(stripped)
    except ValueError:
        return False
    return True


__all__ = ["PdfplumberEngine", "PdfplumberNotInstalledError"]

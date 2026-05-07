"""Table extraction subsystem for kaos-pdf (FUND-4).

Closes the "tables emerge as unstructured prose" failure mode. The
legacy geometric detector in :mod:`kaos_pdf.extract` (``extract_tables``
path) clusters text rectangles and emits flat ``list[list[str]]`` rows
— no multi-line cells, no borderless tables, no column typing.

This subsystem is a pluggable, engine-driven replacement:

- :class:`TableEngine` — ABC every engine implements (sync + async).
- :class:`ExtractedTable` / :class:`TableResult` — the wire types.
- :class:`PdfplumberEngine` — MIT-licensed, pure-Python default.

Callers opt in via the new ``tables="engine"`` parameter on
:func:`kaos_pdf.extract_pdf`. The old geometric path stays for
backward compatibility under ``tables="geometric"`` (the default).

Use :func:`kaos_pdf.extract_pdf_with_tables` to get a sidecar
:class:`~kaos_content.model.tabular.TabularDocument` with typed
columns — downstream analytical tools (Polars, DuckDB, verifier) prefer
the typed shape.
"""

from __future__ import annotations

from kaos_pdf.tables.base import (
    ExtractedTable,
    TableEngine,
    TableResult,
)

__all__ = [
    "ExtractedTable",
    "TableEngine",
    "TableResult",
    "get_default_engine",
]


def get_default_engine() -> TableEngine:
    """Return the default table engine.

    Currently a :class:`~kaos_pdf.tables.pdfplumber.PdfplumberEngine`.
    Engine construction is lazy so the ``[tables]`` extra only pays
    its import cost when a caller actually asks for table extraction.
    """
    from kaos_pdf.tables.pdfplumber import PdfplumberEngine

    return PdfplumberEngine()

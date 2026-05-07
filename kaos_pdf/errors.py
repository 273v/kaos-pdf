"""PDF error hierarchy for kaos-pdf.

All errors subclass KaosPdfError → KaosCoreError, carrying structured
details for agent-friendly error messages and middleware decision-making.
"""

from __future__ import annotations

from kaos_core.exceptions import KaosCoreError


class KaosPdfError(KaosCoreError):
    """Base error for all kaos-pdf operations."""


class PdfExtractionError(KaosPdfError):
    """PDF content extraction failed (corrupt file, unsupported encoding, etc.)."""


class PdfRenderError(KaosPdfError):
    """Page rendering failed (invalid page index, renderer error, etc.)."""


class PdfNotFoundError(KaosPdfError):
    """PDF file not found at the specified path."""

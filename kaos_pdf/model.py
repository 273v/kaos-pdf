"""Typed result models for kaos-pdf public APIs.

These dataclasses replace the previous ``dict[str, Any]`` /
``list[dict[str, Any]]`` return shapes from
:func:`kaos_pdf.get_pdf_metadata` and
:func:`kaos_pdf.get_pdf_outline`. They preserve the prior wire-format
semantics through ``to_dict()`` helpers (sparse: optional fields are
omitted when ``None``) so MCP tool outputs and existing callers that
check ``if "author" in meta`` keep working.

Audit-01 PDF-003.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "PdfMetadata",
    "PdfOutlineEntry",
]


@dataclass(frozen=True, slots=True)
class PdfMetadata:
    """PDF document-info metadata.

    Mirrors the keys defined in the PDF specification's Document
    Information Dictionary (PDF 32000-1:2008 §14.3.3). Each field is
    ``None`` when the source PDF does not declare it.

    ``page_count`` is the only field guaranteed to be present.
    """

    page_count: int
    title: str | None = None
    author: str | None = None
    subject: str | None = None
    keywords: str | None = None
    creator: str | None = None
    producer: str | None = None

    def to_dict(self) -> dict[str, str | int]:
        """Return a sparse dict (omit ``None``-valued optional fields).

        Preserves the pre-PDF-003 wire format: callers receive
        ``{"page_count": int}`` plus whatever optional string fields
        the PDF declared. Used at the MCP tool / CLI boundary so JSON
        consumers don't see ``"author": null`` when the field is absent.
        """
        result: dict[str, str | int] = {"page_count": self.page_count}
        if self.title is not None:
            result["title"] = self.title
        if self.author is not None:
            result["author"] = self.author
        if self.subject is not None:
            result["subject"] = self.subject
        if self.keywords is not None:
            result["keywords"] = self.keywords
        if self.creator is not None:
            result["creator"] = self.creator
        if self.producer is not None:
            result["producer"] = self.producer
        return result


@dataclass(frozen=True, slots=True)
class PdfOutlineEntry:
    """One entry in a PDF's bookmark / outline tree.

    Equivalent to one row in the table of contents. ``level`` is the
    nesting depth (0 = top-level). ``page`` is the 0-based target page
    index, or ``None`` when the bookmark has no destination page (rare;
    typically pure-action bookmarks).
    """

    title: str
    level: int
    page: int | None

    def to_dict(self) -> dict[str, str | int | None]:
        """Return the dict shape used in pre-PDF-003 wire output."""
        return {"title": self.title, "level": self.level, "page": self.page}

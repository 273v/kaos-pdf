"""Text-quality heuristics for detecting garbled native text layers.

Many scanned PDFs ship a *native* text layer produced by the scanner's or
producer's own OCR (Canon devices, Adobe Paper Capture, etc.). That layer is
often partially garbled — a stylized title block comes back as
``"0RlGlt\\IAt  lJn tbe @nitp! btutts"`` while the body text is fine. The
default :func:`kaos_pdf.extract_pdf` ``ocr="auto"`` policy only re-runs OCR when
the native layer is *empty*; a present-but-garbled layer slips through and the
caller receives the garbage verbatim.

This module provides a cheap, dependency-free legibility signal so ``"auto"``
can also recover those pages. The signal is an English-dictionary hit-rate
computed per text line (block), scored as the *worst* substantial line on the
page. Empirically (see the repository's OCR evaluation), localized garbage on an
otherwise-readable scanned page is best detected at line granularity: a garbled
title line scores near ``0.0`` while every substantial line of clean text — even
clean text with the occasional proper noun or citation — stays well above
``0.4``.

The heuristic is intentionally conservative and is gated by the caller on a
*structural* signal (the page must look like a scan, i.e. carry raster image
content) so born-digital text pages are never re-OCR'd. The dictionary is a
permissively-licensed SCOWL word list shipped at
``kaos_pdf/data/english_words.txt.gz`` (see ``WORDLIST_PROVENANCE.md``); it is
loaded lazily on first use, so importing :mod:`kaos_pdf` does no file I/O and
callers that never use ``ocr="auto"`` never pay for it.
"""

from __future__ import annotations

import gzip
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

#: Default worst-line legibility below which an ``ocr="auto"`` scanned page is
#: treated as having a garbled native text layer and re-OCR'd. Tuned on real
#: scanned fixtures: genuinely garbled title/caption blocks score below ~0.35
#: while the worst substantial line of clean text stays at or above ~0.5.
DEFAULT_OCR_QUALITY_THRESHOLD = 0.35

#: A line must contain at least this many alphabetic tokens to be scored.
#: Short lines (page numbers, single-word headings, stray punctuation) carry too
#: little signal and are skipped so they cannot drag the page score in either
#: direction.
_MIN_LINE_TOKENS = 4

# Markdown / layout markup that the serializer or block builder may introduce.
# Stripped before tokenizing so e.g. a heading ``"# Title"`` is scored as
# ``"Title"`` rather than penalized for the leading ``#``.
_MARKUP = re.compile(r"[#*>`\[\]\\_~|]+")
_ALPHA_TOKEN = re.compile(r"[A-Za-z]+")


@lru_cache(maxsize=1)
def _english_words() -> frozenset[str]:
    """Load and cache the lowercase English word set (lazy, once per process)."""
    data = resources.files("kaos_pdf.data").joinpath("english_words.txt.gz").read_bytes()
    text = gzip.decompress(data).decode("ascii")
    return frozenset(text.split())


def line_legibility(text: str) -> float:
    """Return the dictionary hit-rate of ``text`` in ``[0, 1]``.

    Markup is stripped, the line is tokenized into alphabetic runs, tokens
    shorter than two characters are ignored (initials, stray letters), and the
    result is the fraction of remaining tokens that are real English words.

    Returns ``1.0`` for a line with no scorable tokens — absence of evidence is
    not treated as evidence of garbage.
    """
    cleaned = _MARKUP.sub(" ", text)
    tokens = [t.lower() for t in _ALPHA_TOKEN.findall(cleaned) if len(t) >= 2]
    if not tokens:
        return 1.0
    words = _english_words()
    hits = sum(1 for t in tokens if t in words)
    return hits / len(tokens)


@dataclass(frozen=True, slots=True)
class LayerQuality:
    """Legibility assessment of a page's native text layer.

    Attributes:
        score: Worst (minimum) :func:`line_legibility` across the page's
            substantial lines, in ``[0, 1]``. ``1.0`` when the page has no
            substantial lines to score (treated as "no evidence of garbage").
        substantial_lines: Number of lines that met the token threshold and
            were scored.
        worst_line: The text of the lowest-scoring substantial line, for
            diagnostics. Empty when no line was scored.
    """

    score: float
    substantial_lines: int
    worst_line: str


def assess_text_quality(text: str, *, min_line_tokens: int = _MIN_LINE_TOKENS) -> LayerQuality:
    """Assess a block of native text, scoring the worst substantial line.

    ``text`` is split on newlines; each line carrying at least
    ``min_line_tokens`` alphabetic tokens is scored with :func:`line_legibility`
    and the minimum is reported. Localized garbage (a single mangled title or
    caption line) therefore drives the page score even when the surrounding body
    text is clean.
    """
    worst = 1.0
    worst_line = ""
    scored = 0
    for raw in text.split("\n"):
        cleaned = _MARKUP.sub(" ", raw)
        if len([t for t in _ALPHA_TOKEN.findall(cleaned) if len(t) >= 2]) < min_line_tokens:
            continue
        scored += 1
        score = line_legibility(raw)
        if score < worst:
            worst = score
            worst_line = raw.strip()
    if scored == 0:
        return LayerQuality(score=1.0, substantial_lines=0, worst_line="")
    return LayerQuality(score=worst, substantial_lines=scored, worst_line=worst_line)


def is_low_quality_layer(
    text: str,
    *,
    threshold: float = DEFAULT_OCR_QUALITY_THRESHOLD,
    min_line_tokens: int = _MIN_LINE_TOKENS,
) -> bool:
    """Return ``True`` if ``text`` looks like a garbled OCR layer.

    A layer is low-quality when it has at least one substantial line and its
    worst substantial line scores below ``threshold``. A page with no
    substantial lines returns ``False`` (the ``ocr="auto"`` empty-layer check
    handles genuinely empty pages separately).
    """
    quality = assess_text_quality(text, min_line_tokens=min_line_tokens)
    return quality.substantial_lines > 0 and quality.score < threshold


__all__ = [
    "DEFAULT_OCR_QUALITY_THRESHOLD",
    "LayerQuality",
    "assess_text_quality",
    "is_low_quality_layer",
    "line_legibility",
]

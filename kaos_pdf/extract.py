"""PDF extraction: convert PDF files to ContentDocument AST using pypdfium2.

Ported from kelvin_pdf's proven extraction patterns, targeting kaos-content's
Block/Inline AST with full provenance (page numbers, bounding boxes).

Thread safety: All pypdfium2 calls are serialized through ``_PDFIUM_LOCK``.
PDFium's C++ code has global mutable state and is NOT thread-safe — even
operating on different documents from different threads is unsafe. See
``docs/THREAD_SAFETY.md`` for details and parallelism recommendations.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
from kaos_content.builders.builder import DocumentBuilder
from kaos_content.images.model import KaosImage
from kaos_content.layout import TextBlock
from kaos_content.model.attr import BoundingBox, Provenance, SourceRef
from kaos_content.model.document import ContentDocument
from kaos_core.logging import get_logger

from kaos_pdf._text_markers import HyphenResolver, clean_line_text, clean_page_text
from kaos_pdf._text_markers import join_hyphenated_lines as _join_hyphenated_line_rects
from kaos_pdf.errors import PdfExtractionError
from kaos_pdf.model import PdfMetadata, PdfOutlineEntry
from kaos_pdf.quality import DEFAULT_OCR_QUALITY_THRESHOLD, is_low_quality_layer

if TYPE_CHECKING:
    from kaos_content.model.tabular import TabularDocument

    from kaos_pdf.ocr.base import OCREngine, OCRResult
    from kaos_pdf.tables.base import ExtractedTable, TableEngine

# ``ocr`` parameter vocabulary: "never" preserves historical behavior
# (scanned pages emit empty paragraphs); "auto" runs OCR only on pages
# whose native text extraction yielded nothing; "always" runs OCR on
# every page and ignores native text (useful for re-OCR of bad scans
# embedded inside partially-text PDFs).
OCRMode = Literal["never", "auto", "always"]

# ``tables`` parameter vocabulary: "geometric" = legacy rect-clustering
# detector (zero-dep, narrow coverage); "engine" = pluggable
# :class:`kaos_pdf.tables.TableEngine` (pdfplumber by default, MIT);
# "disabled" = skip table detection entirely.
TableMode = Literal["geometric", "engine", "disabled"]

# ``reading_order`` parameter vocabulary: "auto" = PDFium's content-stream
# line order when it is geometrically plausible for the page, else the
# geometric (column-detection + top-to-bottom sort) order; "stream" = always
# PDFium order; "geometric" = always the geometric order (pre-0.1.9 behavior).
ReadingOrder = Literal["auto", "stream", "geometric"]

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Thread safety: PDFium has global mutable C state and is NOT thread-safe.
# All calls must be serialized. See docs/THREAD_SAFETY.md.
# ---------------------------------------------------------------------------
_PDFIUM_LOCK = threading.Lock()

# pypdfium2 page object type constants
FPDF_PAGEOBJ_TEXT = 1
FPDF_PAGEOBJ_IMAGE = 3
FPDF_PAGEOBJ_PATH = 2
FPDF_PAGEOBJ_FORM = 5


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# ---- Defense-in-depth: byte-level type guard ----------------------------


def _assert_pdf_or_skip(path: Path) -> None:
    """Refuse non-PDF input with a typed error when kaos-nlp-core is
    available; silently pass-through (preserving legacy behavior) when
    it isn't.

    The audit at
    ``kaos-modules/docs/audits/2026-05-22-content-type-detection-unused.md``
    Fix 4 calls this out as defense-in-depth: even with bytes-routing
    dispatchers upstream (kaos-ui PR #26, kaos-agents PR #71),
    direct-Python callers that import :func:`extract_pdf` /
    :func:`parse_pdf` deserve a clear error instead of a pypdfium2
    traceback when they accidentally hand us a DOCX or PNG.

    Returns ``None`` and leaves the caller to invoke pypdfium2 when:
    - ``kaos-nlp-core`` isn't importable (preserves the kaos-pdf-only
      install path), or
    - detection succeeds and returns ``group="pdf"`` or ``group="unknown"``
      (the latter covers in-the-wild PDFs whose first 64KB don't
      contain a clean magic-byte signature — let pypdfium2 try).

    Raises :class:`~kaos_pdf.errors.PdfExtractionError` when detection
    succeeds and returns a non-PDF, non-unknown group.
    """
    try:
        from kaos_nlp_core.content_type import detect
    except ImportError:
        return  # kaos-nlp-core not on the path — preserve legacy behavior.

    try:
        with path.open("rb") as fh:
            head = fh.read(65536)  # 64KB — generous; the OPC fallback
            # inside detect() opens the central directory itself when
            # the head doesn't contain enough signal.
        result = detect(head)
    except FileNotFoundError:
        # Let the existing pypdfium2 path produce the canonical
        # not-found error rather than swallow it here.
        return
    except Exception:  # pragma: no cover — defensive
        # Any detection failure should not block extraction; pypdfium2
        # remains the authoritative parser.
        return

    if result.group in ("pdf", "unknown"):
        return

    alternative: str | None = None
    if result.group == "office-docx":
        alternative = "kaos-office.parse_docx"
    elif result.group == "office-pptx":
        alternative = "kaos-office.parse_pptx"
    elif result.group == "office-xlsx":
        alternative = "kaos-office.parse_xlsx"
    elif result.group == "image":
        alternative = "kaos-content image bridge (PIL.Image.open + KaosImage)"
    elif result.group == "html":
        alternative = "kaos-content.parsers.html.parse_html"

    message = (
        f"expected a PDF at {path.name!r} but the bytes look like "
        f"content-type group {result.group!r} "
        f"(detected MIME: {result.mime_type or 'unknown'}). "
        "Route this file through the parser that matches its real type, "
        "or check whether the upload was renamed / corrupted."
    )
    raise PdfExtractionError(
        message,
        what=(
            f"expected a PDF at {path.name!r} but the bytes look like "
            f"content-type group {result.group!r} "
            f"(detected MIME: {result.mime_type or 'unknown'})"
        ),
        how_to_fix=(
            "route this file through the parser that matches its real "
            "type, or check whether the upload was renamed / corrupted"
        ),
        alternative_tool=alternative,
        detected_group=result.group,
        detected_mime=result.mime_type,
        path=str(path),
    )


def extract_pdf(
    source: str | Path,
    *,
    pages: list[int] | None = None,
    extract_tables: bool = True,
    extract_images: bool = False,
    image_src_builder: ImageSrcBuilder | None = None,
    detect_headings: bool = True,
    heading_font_ratio: float = 1.2,
    combine_fragments: bool = True,
    y_tolerance: float = 2.0,
    merge_column_paragraphs: bool = False,
    join_hyphenated_lines: bool = True,
    hyphen_resolver: HyphenResolver | None = None,
    reading_order: ReadingOrder = "auto",
    ocr: OCRMode = "never",
    ocr_engine: OCREngine | None = None,
    ocr_dpi: int = 300,
    ocr_min_confidence: float = 0.0,
    ocr_quality_threshold: float = DEFAULT_OCR_QUALITY_THRESHOLD,
    tables: TableMode = "geometric",
    table_engine: TableEngine | None = None,
    strip_line_numbers: bool = True,
) -> ContentDocument:
    """Extract a PDF file into a ContentDocument AST.

    Args:
        source: Path to the PDF file.
        pages: Optional list of 0-based page indices to extract. None = all.
        extract_tables: Whether to detect and extract tables.
        extract_images: Whether to extract embedded images from each page.
            When ``True`` every embedded ``FPDF_PAGEOBJ_IMAGE`` is decoded
            into bytes, wrapped in a ``Figure(Paragraph(Image))``, and
            given a ``src`` via ``image_src_builder``.
        image_src_builder: Strategy for computing ``Image.src`` from
            extracted image bytes. Signature
            ``(data, fmt, page, index) -> str`` where ``data`` is the raw
            image bytes, ``fmt`` the MIME subtype
            (``"jpeg"`` / ``"png"`` / ``"jp2"``), ``page`` the 1-based
            page number, and ``index`` the 1-based image index within
            the page. Default inlines the bytes as a ``data:`` URL so
            documents stay self-contained. Callers who need out-of-band
            storage supply their own builder — e.g. persist to the
            filesystem and return ``file://...``, synchronously register
            with an artifact store and return ``kaos://artifacts/{id}/body``,
            or emit bare logical URIs (``pdf://page-N/image-K.jpeg``)
            and keep the bytes in a side channel. Ignored when
            ``extract_images`` is ``False``.
        detect_headings: Whether to infer headings from font size/style heuristics.
        heading_font_ratio: Font size ratio above body text to classify as heading.
        combine_fragments: Combine text fragments on same line (recommended).
        y_tolerance: Y-coordinate tolerance for fragment combining (points).
        merge_column_paragraphs: When ``True``, consecutive single-line
            rects within the same detected column that look like
            column-wrap continuations (previous line ends without
            sentence-terminating punctuation, vertical gap ≤ 1.6 line
            heights, no font-size jump) are merged into a single
            paragraph rect. Default ``False`` for backward compatibility.
            Recommended for downstream NLP on multi-column publications
            (Federal Register, court reporters, GPO bulletins) where the
            line-level rects otherwise look heading-shaped to per-line
            scorers. No effect on single-column PDFs.
        join_hyphenated_lines: When ``True`` (default), a line that PDFium
            marks as ending in a hyphenated word break (its end-of-line
            hyphen marker U+0002, the legacy U+FFFE, or a trailing U+00AD
            soft hyphen) is merged with the next line when that line
            starts with a lowercase letter: ``"Repre"`` + ``"sentatives"``
            becomes one paragraph reading ``"Representatives"``, with the
            union bbox as provenance. A marker followed by an
            uppercase letter, a digit, or nothing renders as ``"-"`` and
            the line break stays. ``False`` never merges lines and renders
            every marker as ``"-"``. Either way no PDFium marker or other
            C0 control character (except tab / newline / carriage return)
            reaches the output; dot-leader glyph runs render as ``"."``.
            PDFium marks real compound hyphens at a line end exactly like
            word-break hyphens, so the default decision
            (:func:`kaos_pdf.hyphen_join_decision`) drops the hyphen only
            when the joined form is a dictionary word, keeps ``"-"`` for
            compounds of two words (``"well-being"``, ``"day-to-day"``),
            and when unsure keeps both ``"-"`` and the line break.
        hyphen_resolver: Optional :data:`kaos_pdf.HyphenResolver` that
            replaces the default join-or-hyphen decision, e.g. one backed
            by corpus counts or a domain lexicon. Called as
            ``resolver(left_fragment, right_word)``; return ``"join"``,
            ``"hyphen"``, or ``None`` (keep ``"-"`` and the line break).
            Ignored when ``join_hyphenated_lines`` is ``False``.
        reading_order: Line order within a page. ``"auto"`` (default)
            uses PDFium's text lines in content-stream order (the order
            alea-preprocess / KL3M extraction used) when that order reads
            plausibly top-down and column by column, and otherwise falls
            back to ``"geometric"``. ``"stream"`` always uses PDFium's
            order. ``"geometric"`` is the pre-0.1.9 behavior: column
            detection plus a top-to-bottom sort, which interleaves the
            columns of a page whose gutter goes undetected. With
            ``combine_fragments=False`` the order is always geometric.
        ocr: OCR policy. ``"never"`` (default, safe) preserves legacy
            behavior — scanned pages emit empty paragraphs. ``"auto"``
            runs OCR on pages where native text extraction produced
            nothing, AND on structurally-scanned pages (those carrying
            raster image content) whose native text layer looks garbled
            per ``ocr_quality_threshold`` — this recovers scanned PDFs
            that ship a present-but-mangled OCR layer (Canon scans,
            stale Paper-Capture passes). Born-digital text pages always
            pass through untouched. ``"always"`` runs OCR on every page
            and ignores native text — the deterministic option for
            re-OCR of mixed PDFs with known-bad embedded text layers.
            Default is ``"never"`` so existing callers don't silently
            acquire an OCR dependency; switch to ``"auto"`` when you
            want scan-aware extraction.
        ocr_engine: Optional :class:`~kaos_pdf.ocr.base.OCREngine`
            instance. If ``None`` and OCR would run, a default Tesseract
            engine is created lazily. Callers wanting PaddleOCR or a
            custom engine construct it here.
        ocr_dpi: DPI to render pages at before running OCR. 300 is the
            standard for printed legal documents; 600 for small type.
            Higher DPI = slower + better accuracy. Only used when OCR
            actually runs for a page.
        ocr_min_confidence: Drop OCR lines whose per-line confidence is
            below this threshold. 0.0 (default) keeps everything; 0.5
            is a sensible "real signal only" floor for noisy scans.
        ocr_quality_threshold: Worst-line legibility (English-dictionary
            hit-rate in ``[0, 1]``) below which an ``ocr="auto"``
            scanned page is treated as having a garbled native text
            layer and re-OCR'd. Default
            :data:`~kaos_pdf.quality.DEFAULT_OCR_QUALITY_THRESHOLD`
            (0.35) only triggers on genuinely mangled blocks; raise it
            to re-OCR more aggressively, lower it (or use 0.0) to
            disable garbled-layer detection and restore the legacy
            empty-layer-only ``"auto"`` behavior. Only consulted for
            ``ocr="auto"``.
        tables: Table detection strategy (FUND-4). ``"geometric"``
            (default) preserves the legacy rect-clustering detector so
            existing callers get byte-for-byte identical output.
            ``"engine"`` delegates to ``table_engine`` (pdfplumber by
            default, MIT) for robust multi-line / borderless detection;
            requires the ``[tables]`` extra. ``"disabled"`` skips table
            detection entirely — rows come through as plain paragraphs.
        table_engine: Optional :class:`~kaos_pdf.tables.TableEngine`.
            ``None`` + ``tables="engine"`` constructs the default
            pdfplumber engine lazily. Override with a custom engine
            (docling, custom ML pipeline) when needed.
        strip_line_numbers: When ``True`` (default), a pleading-paper
            line-number column (court filings, bills: the 1-28 numbers in
            the left margin, one per ruled line) is left out of the text.
            Detection is geometric and per page: a narrow, aligned column of
            number-only fragments counting up at one line pitch, with no
            other text crossing it. Numbered paragraphs, list labels,
            footnote numbers and table row numbers are kept. ``False``
            keeps the numbers.
            Independently of this flag, running page furniture is removed:
            repeated headers/footers, running stamps whose only change is a
            page counter (CM/ECF ``"Case … Document … Filed … Page n of
            m"``, bare page numbers) and repeated vertical margin text. The
            document's ``metadata.extra["furniture_removed"]`` counts what
            was removed, by rule.

    Returns:
        A ContentDocument with provenance (page numbers, bounding
        boxes, and — for OCR-produced blocks — confidence scores).
    """
    path = Path(source).resolve()
    source_uri = path.as_uri()

    # Defense-in-depth: refuse non-PDF input with a clear, actionable
    # error before handing it to pypdfium2. Without this guard,
    # mis-routed DOCX / PPTX / image / unknown bytes died deep inside
    # the C parser with an opaque error like "invalid object stream";
    # downstream callers (capstones, scripts, third-party integrations
    # that don't already sniff bytes upstream) had no way to see what
    # the file actually was. The byte-sniff is best-effort: when
    # kaos-nlp-core is unavailable at runtime we skip the guard
    # rather than acquire a hard runtime dependency on it.
    _assert_pdf_or_skip(path)

    # Engine-mode table detection runs BEFORE the pypdfium2 pipeline so
    # extracted tables are indexed by page and stitched into the
    # ContentDocument below. pdfplumber is thread-safe for its own
    # handles but we keep the PDFium serialization lock separate.
    engine_tables: list[ExtractedTable] = []
    if tables == "engine":
        engine_tables = _run_table_engine(str(path), table_engine, page_indices=pages)

    with _PDFIUM_LOCK:
        doc = pdfium.PdfDocument(str(path))
        try:
            return _extract_document(
                doc,
                source_uri=source_uri,
                pages=pages,
                extract_tables=extract_tables,
                extract_images=extract_images,
                image_src_builder=image_src_builder,
                detect_headings=detect_headings,
                heading_font_ratio=heading_font_ratio,
                combine_fragments=combine_fragments,
                y_tolerance=y_tolerance,
                merge_column_paragraphs=merge_column_paragraphs,
                join_hyphenated_lines=join_hyphenated_lines,
                hyphen_resolver=hyphen_resolver,
                reading_order=reading_order,
                ocr=ocr,
                ocr_engine=ocr_engine,
                ocr_dpi=ocr_dpi,
                ocr_min_confidence=ocr_min_confidence,
                ocr_quality_threshold=ocr_quality_threshold,
                tables=tables,
                engine_tables=engine_tables,
                strip_line_numbers=strip_line_numbers,
            )
        finally:
            doc.close()


def extract_pdf_with_tables(
    source: str | Path,
    *,
    pages: list[int] | None = None,
    table_engine: TableEngine | None = None,
    **kwargs: Any,
) -> tuple[ContentDocument, TabularDocument]:
    """Extract a PDF and return a ``(ContentDocument, TabularDocument)`` pair.

    Sibling to :func:`extract_pdf` that always runs the FUND-4 engine
    pipeline and additionally emits a
    :class:`~kaos_content.model.tabular.TabularDocument` sidecar with
    one :class:`~kaos_content.model.tabular.Table` per detected table
    and inferred :class:`~kaos_content.model.tabular.ColumnType`
    metadata on each :class:`~kaos_content.model.tabular.Column`.

    Use this when downstream consumers are Polars / DuckDB / verify
    pipelines that want typed cells rather than stringly-typed AST
    table rows.

    Args:
        source: Path to the PDF file.
        pages: Optional list of 0-based page indices.
        table_engine: Optional engine override. Default = pdfplumber.
        **kwargs: Forwarded to :func:`extract_pdf` (OCR params, heading
            detection, etc.). ``tables`` is forced to ``"engine"``;
            passing ``tables`` in ``kwargs`` raises.

    Returns:
        Tuple of ``(ContentDocument, TabularDocument)``. The
        ``TabularDocument`` may contain zero :class:`Table` objects
        when no tables were detected; it's never ``None``.
    """
    if "tables" in kwargs:
        msg = (
            "extract_pdf_with_tables always uses tables='engine'. "
            "Pass a custom engine via table_engine= instead of "
            "overriding 'tables'."
        )
        raise TypeError(msg)

    path = str(Path(source).resolve())
    engine_tables = _run_table_engine(path, table_engine, page_indices=pages)
    doc = extract_pdf(
        source,
        pages=pages,
        tables="engine",
        table_engine=table_engine,
        **kwargs,
    )
    tabular = _build_tabular_document(path, engine_tables)
    return doc, tabular


def extract_pdf_bytes(
    data: bytes,
    *,
    filename: str = "document.pdf",
    **kwargs: Any,
) -> ContentDocument:
    """Extract a PDF from in-memory bytes."""
    with _PDFIUM_LOCK:
        doc = pdfium.PdfDocument(data)
        try:
            return _extract_document(
                doc,
                source_uri=f"memory://{filename}",
                **kwargs,
            )
        finally:
            doc.close()


# Canonical aliases per docs/guides/python-api-naming.md.
# ``parse_pdf`` / ``parse_pdf_bytes`` are the names callers should reach for —
# ``extract_pdf`` / ``extract_pdf_bytes`` are kept as deprecation wrappers in
# ``kaos_pdf/__init__.py``. Both call into the same underlying implementation.
parse_pdf = extract_pdf
parse_pdf_bytes = extract_pdf_bytes


def get_page_count(source: str | Path) -> int:
    """Return the number of pages in a PDF."""
    with _PDFIUM_LOCK:
        doc = pdfium.PdfDocument(str(source))
        try:
            return len(doc)
        finally:
            doc.close()


def get_pdf_metadata(source: str | Path) -> PdfMetadata:
    """Return PDF metadata (title, author, subject, page_count, etc.).

    Returns a typed :class:`~kaos_pdf.model.PdfMetadata` whose optional
    string fields are ``None`` when the source PDF doesn't declare them.
    Use ``.to_dict()`` to get the prior sparse-dict wire format.
    """
    with _PDFIUM_LOCK:
        doc = pdfium.PdfDocument(str(source))
        try:
            return _extract_metadata(doc, page_count=len(doc))
        finally:
            doc.close()


def extract_page_text(
    source: str | Path,
    page_number: int,
    *,
    join_hyphenated_lines: bool = True,
    hyphen_resolver: HyphenResolver | None = None,
) -> str:
    """Extract plain text from a single page (0-based index).

    PDFium's end-of-line hyphen markers are resolved as described for
    :func:`extract_pdf` (``join_hyphenated_lines``, ``hyphen_resolver``), dot-leader glyph runs
    render as ``"."``, and no C0 control character other than tab, newline,
    or carriage return is returned.
    """
    with _PDFIUM_LOCK:
        doc = pdfium.PdfDocument(str(source))
        try:
            if page_number < 0 or page_number >= len(doc):
                msg = f"Page {page_number} out of range (0-{len(doc) - 1})"
                raise IndexError(msg)
            page = doc[page_number]
            tp = page.get_textpage()
            text = tp.get_text_bounded()
            tp.close()
            cleaned, dropped = clean_page_text(
                text, join_hyphenated_lines=join_hyphenated_lines, resolver=hyphen_resolver
            )
            if dropped:
                logger.debug(
                    "dropped %d unmapped control character(s) from page %d text",
                    dropped,
                    page_number + 1,
                )
            return cleaned
        finally:
            doc.close()


def render_page(
    source: str | Path,
    page_number: int,
    *,
    dpi: int = 300,
    grayscale: bool = False,
) -> KaosImage:
    """Render a single PDF page as a KaosImage."""
    with _PDFIUM_LOCK:
        doc = pdfium.PdfDocument(str(source))
        try:
            if page_number < 0 or page_number >= len(doc):
                msg = f"Page {page_number} out of range (0-{len(doc) - 1})"
                raise IndexError(msg)
            page = doc[page_number]
            return _render_page_to_image(
                page,
                page_number,
                source_uri=Path(source).as_uri(),
                dpi=dpi,
                grayscale=grayscale,
            )
        finally:
            doc.close()


def get_pdf_outline(source: str | Path) -> list[PdfOutlineEntry]:
    """Extract the PDF's bookmark/outline tree (table of contents).

    Returns a list of :class:`~kaos_pdf.model.PdfOutlineEntry`. Each
    entry has ``title``, ``level`` (0-based nesting depth), and ``page``
    (0-based target page index, or ``None`` for action-only bookmarks).
    """
    with _PDFIUM_LOCK:
        doc = pdfium.PdfDocument(str(source))
        try:
            return _extract_outline(doc)
        finally:
            doc.close()


def _extract_outline(doc: pdfium.PdfDocument) -> list[PdfOutlineEntry]:
    """Extract outline entries from a PdfDocument."""
    entries: list[PdfOutlineEntry] = []
    try:
        for bookmark in doc.get_toc(max_depth=10):
            page: int | None = None
            dest = bookmark.get_dest()
            if dest is not None:
                page_idx = dest.get_index()
                if page_idx is not None and page_idx >= 0:
                    page = page_idx
            entries.append(
                PdfOutlineEntry(
                    title=bookmark.get_title(),
                    level=bookmark.level,
                    page=page,
                )
            )
    except (OSError, AttributeError, ValueError, RuntimeError):
        logger.debug("Failed to extract PDF outline", exc_info=True)
    return entries


def classify_page(source: str | Path, page_number: int) -> str:
    """Classify a page as 'text', 'image', 'mixed', or 'blank'."""
    with _PDFIUM_LOCK:
        doc = pdfium.PdfDocument(str(source))
        try:
            if page_number < 0 or page_number >= len(doc):
                msg = f"Page {page_number} out of range (0-{len(doc) - 1})"
                raise IndexError(msg)
            page = doc[page_number]
            return _classify_page(page)
        finally:
            doc.close()


def classify_document(source: str | Path) -> str:
    """Classify entire document as 'text', 'image', 'mixed', 'scanned', or 'blank'."""
    with _PDFIUM_LOCK:
        doc = pdfium.PdfDocument(str(source))
        try:
            n = len(doc)
            if n == 0:
                return "blank"

            sample_count = min(n, 10)
            page_types = []
            for i in range(sample_count):
                page_types.append(_classify_page(doc[i]))

            text_count = page_types.count("text")
            image_count = page_types.count("image")
            blank_count = page_types.count("blank")

            if blank_count == sample_count:
                return "blank"
            if image_count == sample_count:
                return "scanned"
            if text_count == sample_count:
                return "text"
            if image_count > 0 and text_count > 0:
                return "mixed"
            if text_count / sample_count >= 0.7:
                return "text"
            return "mixed"
        finally:
            doc.close()


# ---------------------------------------------------------------------------
# Internal: document extraction
# ---------------------------------------------------------------------------


def _extract_document(
    doc: pdfium.PdfDocument,
    *,
    source_uri: str,
    pages: list[int] | None = None,
    extract_tables: bool = True,
    extract_images: bool = False,
    image_src_builder: ImageSrcBuilder | None = None,
    detect_headings: bool = True,
    heading_font_ratio: float = 1.2,
    combine_fragments: bool = True,
    y_tolerance: float = 2.0,
    merge_column_paragraphs: bool = False,
    join_hyphenated_lines: bool = True,
    hyphen_resolver: HyphenResolver | None = None,
    reading_order: ReadingOrder = "auto",
    ocr: OCRMode = "never",
    ocr_engine: OCREngine | None = None,
    ocr_dpi: int = 300,
    ocr_min_confidence: float = 0.0,
    ocr_quality_threshold: float = DEFAULT_OCR_QUALITY_THRESHOLD,
    tables: TableMode = "geometric",
    engine_tables: list[ExtractedTable] | None = None,
    strip_line_numbers: bool = True,
) -> ContentDocument:
    """Core extraction logic — shared by extract_pdf and extract_pdf_bytes.

    When ``tables="engine"``, ``engine_tables`` must carry the
    pre-computed pdfplumber (or other engine) output — the caller runs
    it outside the PDFium lock because pdfplumber opens the PDF with
    its own handle.
    """
    n_pages = len(doc)
    meta = _extract_metadata(doc, page_count=n_pages)

    from kaos_content.model.attr import SourceRef as ContentSourceRef

    source_ref = ContentSourceRef(uri=source_uri, mime_type="application/pdf")
    builder = DocumentBuilder(title=meta.title)
    builder.set_source(uri=source_uri, mime_type="application/pdf")
    builder.set_metadata(source=source_ref)

    extra: dict[str, Any] = {"page_count": n_pages}
    if meta.producer:
        extra["pdf_producer"] = meta.producer
    if meta.creator:
        extra["pdf_creator"] = meta.creator
    builder.set_metadata(extra=extra)

    if meta.author:
        builder.set_metadata(authors=(meta.author,))

    page_indices = pages if pages is not None else list(range(n_pages))

    # Phase 1: Extract raw text blocks from all pages (column-aware)
    all_page_rects: list[
        list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]]
    ] = []
    # Parallel: per-page link annotations as (bbox, url) tuples.
    all_page_links: list[list[tuple[tuple[float, float, float, float], str]]] = []
    # Parallel: per-page embedded images as (bbox, width_pt, height_pt, data, fmt) tuples.
    all_page_images: list[
        list[tuple[tuple[float, float, float, float], float, float, bytes, str]]
    ] = []
    # Parallel list: per-page OCR result, or None for pages where OCR
    # didn't run. Populated by Phase 1.5 below.
    all_page_ocr: list[OCRResult | None] = []
    line_number_pages = 0
    line_number_fragments = 0
    page_heights: list[float] = []
    page_rotations: list[int] = []
    for page_idx in page_indices:
        if page_idx < 0 or page_idx >= n_pages:
            all_page_rects.append([])
            all_page_links.append([])
            all_page_images.append([])
            all_page_ocr.append(None)
            page_heights.append(0.0)
            page_rotations.append(0)
            continue
        page = doc[page_idx]
        page_width = page.get_width()
        page_heights.append(page.get_height())
        page_rotations.append(page.get_rotation())
        tp = page.get_textpage()
        line_numbers: list[tuple[str, tuple[float, float, float, float], dict[str, Any]]] = []
        try:
            rects = _get_text_rectangles_column_aware(
                tp,
                page_width=page_width,
                combine_fragments=combine_fragments,
                y_tolerance=y_tolerance,
                merge_column_paragraphs=merge_column_paragraphs,
                join_hyphenated_lines=join_hyphenated_lines,
                hyphen_resolver=hyphen_resolver,
                reading_order=reading_order,
                strip_line_numbers=strip_line_numbers,
                removed=line_numbers,
            )
        finally:
            tp.close()
        if line_numbers:
            line_number_pages += 1
            line_number_fragments += len(line_numbers)
        rects = _resolve_text_markers(
            rects,
            join_hyphenated_lines=join_hyphenated_lines,
            hyphen_resolver=hyphen_resolver,
            page_number=page_idx + 1,
        )
        all_page_rects.append(rects)
        # Extract link annotations for this page. Failures are silent —
        # annotation enumeration can fail on malformed PDFs without
        # invalidating text extraction.
        try:
            page_links = _get_link_annotations(page, doc)
        except (OSError, AttributeError, ValueError):
            page_links = []
        all_page_links.append(page_links)
        # Embedded image objects. Gated by ``extract_images`` to avoid
        # paying the rasterization cost for callers that only want text.
        if extract_images:
            try:
                page_images = _get_page_images(page)
            except (OSError, AttributeError, ValueError, RuntimeError):
                page_images = []
        else:
            page_images = []
        all_page_images.append(page_images)
        all_page_ocr.append(None)  # may be filled in Phase 1.5

    # Phase 1.5: OCR per the policy. Runs AFTER native extraction so
    # ``ocr="auto"`` can see whether native text was empty.
    if ocr != "never":
        all_page_ocr = _run_ocr_per_page(
            doc,
            page_indices=page_indices,
            all_page_rects=all_page_rects,
            ocr=ocr,
            ocr_engine=ocr_engine,
            ocr_dpi=ocr_dpi,
            ocr_min_confidence=ocr_min_confidence,
            ocr_quality_threshold=ocr_quality_threshold,
            source_uri=source_uri,
        )

    # Phase 2: Filter headers/footers (cross-page repeated content)
    furniture: dict[str, int] = {}
    filter_indices = _filter_headers_footers(
        all_page_rects,
        page_numbers=page_indices,
        page_heights=page_heights,
        page_rotations=page_rotations,
        report=furniture,
    )
    # Debug record of what page furniture was removed (non-zero counts only,
    # so documents without any keep their previous metadata).
    removed_report = {k: v for k, v in furniture.items() if v}
    if line_number_fragments:
        removed_report["line_number_fragments"] = line_number_fragments
        removed_report["line_number_pages"] = line_number_pages
    if removed_report:
        extra["furniture_removed"] = removed_report
        builder.set_metadata(extra=extra)

    # Phase 3: Classify headings + assign adaptive depths
    heading_indices: set[int] = set()
    depth_map: dict[int, int] = {}
    all_flat = [r for pr in all_page_rects for r in pr]
    if detect_headings:
        heading_indices = _classify_headings_scored(
            all_page_rects, heading_font_ratio=heading_font_ratio
        )
        if heading_indices:
            depth_map = _build_heading_depth_map(heading_indices, all_flat)

    # Phase 3.5: Merge consecutive heading blocks (multi-line headings)
    if heading_indices and depth_map:
        heading_indices, depth_map, all_page_rects, filter_indices = _merge_consecutive_headings(
            heading_indices, depth_map, all_flat, all_page_rects, filter_indices
        )

    # Phase 3.7: Detect table regions and mark table blocks
    # Maps flat_idx -> table_id for blocks that are part of a table
    table_membership: dict[int, int] = {}
    # Maps table_id -> list of (text, bbox) grouped into rows
    table_data: dict[int, list[list[str]]] = {}
    table_bboxes: dict[int, BoundingBox] = {}
    table_page: dict[int, int] = {}

    # Only run the legacy geometric table detector when the caller
    # opted into it via ``tables="geometric"`` (the default, for
    # backward compat). ``tables="engine"`` uses the pluggable engine
    # output appended after Phase 4; ``tables="disabled"`` skips both.
    if extract_tables and tables == "geometric":
        table_id_counter = 0
        flat_idx = 0
        for i, page_idx in enumerate(page_indices):
            page_rects = all_page_rects[i]
            # Build TextBlocks for this page (excluding filtered blocks)
            page_blocks: list[tuple[int, TextBlock]] = []
            for text, (left, bottom, right, top), _fi in page_rects:
                if text.strip() and flat_idx not in filter_indices:
                    page_blocks.append(
                        (
                            flat_idx,
                            TextBlock(
                                left=left,
                                top=min(top, bottom),
                                right=right,
                                bottom=max(top, bottom),
                                text=text,
                            ),
                        )
                    )
                flat_idx += 1

            if not page_blocks:
                continue

            blocks_only = [b for _, b in page_blocks]
            regions = _detect_page_tables(blocks_only)
            if not regions:
                continue

            for region in regions:
                tl, tt, tr, tb = region
                # Find blocks within this region
                region_entries: list[tuple[int, TextBlock]] = []
                for fidx, block in page_blocks:
                    if (
                        block.left >= tl - 5
                        and block.right <= tr + 5
                        and block.top >= tt - 5
                        and block.bottom <= tb + 5
                    ):
                        region_entries.append((fidx, block))

                if len(region_entries) < 4:
                    continue

                # Build table rows from these blocks
                rows = _build_table_rows_from_blocks([b for _, b in region_entries])
                if rows and len(rows) >= 2:
                    tid = table_id_counter
                    table_id_counter += 1
                    table_data[tid] = rows
                    table_bboxes[tid] = BoundingBox(left=tl, top=tt, right=tr, bottom=tb)
                    table_page[tid] = page_idx
                    for fidx, _ in region_entries:
                        table_membership[fidx] = tid

    # Phase 4: Build ContentDocument from classified blocks
    emitted_tables: set[int] = set()
    flat_idx = 0
    for i, page_idx in enumerate(page_indices):
        if page_idx < 0 or page_idx >= n_pages:
            continue

        # OCR supersedes native text for this page if Phase 1.5
        # produced a result. We still advance flat_idx past this page's
        # native rects so downstream phase-3 indices stay aligned.
        page_ocr = all_page_ocr[i]
        if page_ocr is not None:
            _emit_ocr_blocks(
                builder,
                page_idx=page_idx,
                ocr_result=page_ocr,
                min_confidence=ocr_min_confidence,
            )
            flat_idx += len(all_page_rects[i])
            continue

        rects = all_page_rects[i]
        page_links = all_page_links[i] if i < len(all_page_links) else []
        for text, bbox, font_info in rects:
            text_stripped = text.strip()
            if not text_stripped:
                flat_idx += 1
                continue

            # Skip filtered headers/footers/watermarks
            if flat_idx in filter_indices:
                flat_idx += 1
                continue

            left, bottom, right, top = bbox
            # PDF coords are bottom-left origin (top has the larger y), but
            # BoundingBox defaults to top-left where bottom >= top is the
            # invariant. Normalize via min/max so the validator accepts the
            # box regardless of which y-value pypdfium2 returned as "top".
            bb_top = min(top, bottom)
            bb_bottom = max(top, bottom)
            bb = BoundingBox(left=left, top=bb_top, right=right, bottom=bb_bottom)

            # Check if this block is part of a table
            if flat_idx in table_membership:
                tid = table_membership[flat_idx]
                if tid not in emitted_tables:
                    emitted_tables.add(tid)
                    rows = table_data[tid]
                    if rows:
                        builder.table(headers=rows[0], rows=rows[1:])
                        builder.with_provenance(
                            page=page_idx + 1,
                            bbox=table_bboxes[tid],
                            extractor="kaos-pdf/pypdfium2",
                        )
                flat_idx += 1
                continue

            # Headings stay flat-text (depth + text) — the heading
            # detector already classifies based on font size, so we
            # don't need to preserve Strong/Emphasis inside the title.
            if flat_idx in heading_indices:
                depth = depth_map.get(flat_idx, 2)
                builder.heading(depth, text_stripped)
            else:
                url: str | None = None
                for link_bbox, link_url in page_links:
                    if link_url and _text_rect_covered_by(bbox, link_bbox):
                        url = link_url
                        break

                # Apply bold/italic/link wrapping using the new shortcuts.
                styled = _style_text(text_stripped, font_info, url=url)
                # If no styling applied, ``_style_text`` returns a bare
                # Text; pass it through ``builder.paragraph`` to keep the
                # same emission path.
                builder.paragraph(styled)
            builder.with_provenance(page=page_idx + 1, bbox=bb, extractor="kaos-pdf/pypdfium2")
            flat_idx += 1

    # Phase 4.5: Emit embedded image Figures. Images don't live in text
    # flow (PDF XObjects sit in a separate graphics stream), so we append
    # one Figure per image after each page's text blocks. Consumers that
    # care about page-relative placement can read provenance.bbox.
    if extract_images:
        src_builder = image_src_builder if image_src_builder is not None else _image_data_uri
        for page_idx, page_images in zip(page_indices, all_page_images, strict=False):
            for img_idx, (bbox, width_pt, height_pt, data, fmt) in enumerate(page_images):
                src = src_builder(data, fmt, page_idx + 1, img_idx + 1)
                img_left, img_bottom, img_right, img_top = bbox
                # Same PDF→top-left normalization as text bboxes above.
                img_bb_top = min(img_top, img_bottom)
                img_bb_bottom = max(img_top, img_bottom)
                img_bb = BoundingBox(
                    left=img_left,
                    top=img_bb_top,
                    right=img_right,
                    bottom=img_bb_bottom,
                )
                builder.image(
                    src=src,
                    alt=f"PDF page {page_idx + 1} image {img_idx + 1}",
                    width=width_pt,
                    height=height_pt,
                )
                builder.with_provenance(
                    page=page_idx + 1,
                    bbox=img_bb,
                    extractor="kaos-pdf/pypdfium2",
                )

    # Phase 5: Emit engine-produced tables (FUND-4). We append them at
    # the END of the document rather than interleaving with paragraphs
    # because pdfplumber's (x, y) ordering doesn't map cleanly onto
    # pypdfium2's text-rect ordering. Consumers that care about
    # position can read ``provenance.page``/``provenance.bbox``.
    if tables == "engine" and engine_tables:
        for extracted in engine_tables:
            _emit_engine_table(builder, extracted)

    return builder.build()


def _extract_metadata(doc: pdfium.PdfDocument, *, page_count: int) -> PdfMetadata:
    """Extract PDF metadata fields into a typed :class:`PdfMetadata`.

    Caller supplies ``page_count`` (typically ``len(doc)``) to avoid
    re-scanning the page tree. Optional string fields are ``None`` when
    the PDF doesn't declare them.
    """
    fields: dict[str, str] = {}
    for key in ("title", "author", "subject", "keywords", "creator", "producer"):
        try:
            val = doc.get_metadata_value(key)
            if val:
                fields[key] = val
        except (OSError, AttributeError, ValueError, RuntimeError):
            logger.debug("Failed to extract PDF metadata field '%s'", key, exc_info=True)
    return PdfMetadata(page_count=page_count, **fields)


def _get_link_annotations(
    page: pdfium.PdfPage,
    doc: Any,
) -> list[tuple[tuple[float, float, float, float], str]]:
    """Extract hyperlink annotations from a page.

    Returns a list of ``(bbox, url)`` tuples where ``bbox`` is
    ``(left, bottom, right, top)`` in PDF coordinates and ``url`` is the
    URI. Links without a URI (e.g., pure destinations) are skipped.

    ``doc`` is the pypdfium2 ``PdfDocument`` handle (``.raw`` exposes
    the underlying ``FPDF_DOCUMENT`` pointer needed by
    ``FPDFAction_GetURIPath``).

    Caller MUST hold ``_PDFIUM_LOCK`` — raw annotation API calls are
    not thread-safe.
    """
    import ctypes

    raw_page = page.raw
    raw_doc = doc.raw if hasattr(doc, "raw") else doc
    try:
        n_annots = pdfium_raw.FPDFPage_GetAnnotCount(raw_page)
    except (OSError, AttributeError):
        return []

    results: list[tuple[tuple[float, float, float, float], str]] = []
    for i in range(n_annots):
        annot = pdfium_raw.FPDFPage_GetAnnot(raw_page, i)
        if not annot:
            continue
        try:
            subtype = pdfium_raw.FPDFAnnot_GetSubtype(annot)
            if subtype != pdfium_raw.FPDF_ANNOT_LINK:
                continue

            rect = pdfium_raw.FS_RECTF()
            if not pdfium_raw.FPDFAnnot_GetRect(annot, ctypes.byref(rect)):
                continue
            bbox = (
                float(rect.left),
                float(rect.bottom),
                float(rect.right),
                float(rect.top),
            )

            url = _get_annot_uri(annot, raw_doc)
            if not url:
                # Skip links with no URI (internal destinations, etc.)
                continue
            results.append((bbox, url))
        finally:
            pass

    return results


def _get_annot_uri(annot: Any, doc: Any) -> str:
    """Extract the URI string from a link annotation.

    PDF link annotations store URIs inside an `/A` action dict:
    ``/A <</S /URI /URI (https://...)>>``. The raw URI is not a
    top-level string on the annotation, so the canonical API is
    ``FPDFAnnot_GetLink`` → ``FPDFLink_GetAction`` →
    ``FPDFAction_GetURIPath`` (ASCII URI).

    Returns ``""`` on any failure — callers treat missing URIs as
    non-hyperlink annotations.
    """
    import ctypes

    try:
        link_handle = pdfium_raw.FPDFAnnot_GetLink(annot)
        if not link_handle:
            return ""
        action = pdfium_raw.FPDFLink_GetAction(link_handle)
        if not action:
            return ""
        # FPDFAction_GetURIPath(document, action, buf, buflen) fills the
        # buffer with ASCII + trailing NUL. Probe for size first.
        needed = pdfium_raw.FPDFAction_GetURIPath(doc, action, None, 0)
        if needed <= 1:  # at minimum returns 1 for the NUL
            return ""
        buf = ctypes.create_string_buffer(needed)
        written = pdfium_raw.FPDFAction_GetURIPath(doc, action, buf, needed)
        if written <= 1:
            return ""
        return buf.raw[: max(0, written - 1)].decode("utf-8", errors="replace")
    except (OSError, AttributeError, ValueError):
        return ""


def _rects_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    """Rect overlap test in PDF coordinates (left, bottom, right, top)."""
    a_left, a_bottom, a_right, a_top = a
    b_left, b_bottom, b_right, b_top = b
    if a_right < b_left or b_right < a_left:
        return False
    return not (a_top < b_bottom or b_top < a_bottom)


def _text_rect_covered_by(
    text_bbox: tuple[float, float, float, float],
    link_bbox: tuple[float, float, float, float],
    coverage: float = 0.5,
) -> bool:
    """True when at least ``coverage`` fraction of ``text_bbox`` lies inside ``link_bbox``.

    Adjacent text rects (e.g. the word immediately following a link) can
    touch or graze a link annotation rect — a bare overlap test tags
    them as links. Area-coverage avoids that by requiring the majority
    of the text rect to fall inside the link region.
    """
    t_left, t_bottom, t_right, t_top = text_bbox
    l_left, l_bottom, l_right, l_top = link_bbox
    text_area = max(0.0, t_right - t_left) * max(0.0, t_top - t_bottom)
    if text_area <= 0.0:
        return False
    ix_left = max(t_left, l_left)
    ix_right = min(t_right, l_right)
    ix_bottom = max(t_bottom, l_bottom)
    ix_top = min(t_top, l_top)
    iw = ix_right - ix_left
    ih = ix_top - ix_bottom
    if iw <= 0.0 or ih <= 0.0:
        return False
    return (iw * ih) / text_area >= coverage


_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB per image — anything larger is skipped


def _get_page_images(
    page: pdfium.PdfPage,
) -> list[tuple[tuple[float, float, float, float], float, float, bytes, str]]:
    """Enumerate embedded images on a PDF page and extract their bytes.

    Returns a list of ``(bbox, width_pt, height_pt, data, fmt)`` tuples
    where ``bbox`` is ``(left, bottom, right, top)`` in PDF points,
    ``data`` is the extracted image bytes (JPEG preserved natively,
    others re-encoded as PNG), and ``fmt`` is a MIME subtype
    (``"jpeg"``, ``"png"``, ``"jp2"``).

    Caller MUST hold :data:`_PDFIUM_LOCK` — image object enumeration is
    not thread-safe.

    Images larger than :data:`_MAX_IMAGE_BYTES` are skipped to prevent
    runaway memory use on pathological PDFs.
    """
    import io

    results: list[tuple[tuple[float, float, float, float], float, float, bytes, str]] = []
    try:
        objs = list(page.get_objects(filter=(FPDF_PAGEOBJ_IMAGE,)))
    except (OSError, AttributeError, ValueError):
        return results

    for obj in objs:
        try:
            left, bottom, right, top = obj.get_bounds()
        except (OSError, AttributeError, ValueError):
            continue
        bbox = (float(left), float(bottom), float(right), float(top))
        width_pt = float(right - left)
        height_pt = float(top - bottom)
        if width_pt <= 0.0 or height_pt <= 0.0:
            continue

        buf = io.BytesIO()
        try:
            obj.extract(buf, fb_format="png")
        except (OSError, AttributeError, ValueError, RuntimeError):
            continue
        data = buf.getvalue()
        if not data:
            continue
        if len(data) > _MAX_IMAGE_BYTES:
            logger.info(
                "pdf image skipped — exceeds %d byte cap (%d bytes)",
                _MAX_IMAGE_BYTES,
                len(data),
            )
            continue

        if data[:3] == b"\xff\xd8\xff":
            fmt = "jpeg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            fmt = "png"
        elif data[:4] == b"\x00\x00\x00\x0c" and data[4:8] == b"jP  ":
            fmt = "jp2"
        else:
            fmt = "png"  # conservative — fb_format was png
        results.append((bbox, width_pt, height_pt, data, fmt))
    return results


#: Signature for ``image_src_builder`` — receives the extracted image
#: bytes plus positional metadata (MIME subtype, 1-based page, 1-based
#: index within the page) and returns the string that becomes
#: ``Image.src``. Callers inject their own URI strategy: data URI (the
#: default), logical ``pdf://`` URI paired with out-of-band storage,
#: ``file://`` after writing to disk, ``kaos://artifacts/{id}/body``
#: after a synchronous artifact-store call, etc.
ImageSrcBuilder = Callable[[bytes, str, int, int], str]


def _image_data_uri(data: bytes, fmt: str, page: int = 0, index: int = 0) -> str:
    """Encode image bytes as a ``data:`` URL.

    The default ``image_src_builder`` for :func:`extract_pdf`. Keeps
    extracted documents self-contained at the cost of document size
    (base64 inflation ≈ 4/3). Callers who want out-of-band storage
    pass their own builder — see ``image_src_builder`` on
    :func:`extract_pdf`.
    """
    import base64

    _ = page, index  # unused in the default encoding; declared for the
    # signature contract so callers can swap in a builder that uses them.
    return f"data:image/{fmt};base64,{base64.b64encode(data).decode('ascii')}"


def _style_text(text: str, font_info: dict[str, Any] | None, url: str | None = None) -> Any:
    """Wrap plain text in Strong/Emphasis/Link based on font info and URL.

    Returns an Inline node (or tuple for multiple children). Uses the
    new kaos_content.shortcuts helpers for construction.
    """
    from kaos_content.shortcuts import bold, italic, link
    from kaos_content.shortcuts import text as _text

    node: Any = _text(text)
    if font_info:
        if font_info.get("is_italic"):
            node = italic(node)
        if font_info.get("is_bold"):
            node = bold(node)
    if url:
        node = link(url, node)
    return node


def _font_info_for_char(textpage: pdfium.PdfTextPage, char_idx: int | None) -> dict[str, Any]:
    """Return font metadata (size, name, bold, italic, flags) for one char index."""
    import ctypes

    raw_tp = textpage.raw
    info: dict[str, Any] = {
        "font_size": 0.0,
        "font_name": "",
        "is_bold": False,
        "is_italic": False,
        "flags": 0,
    }

    if char_idx is None:
        return info

    info["font_size"] = pdfium_raw.FPDFText_GetFontSize(raw_tp, char_idx)

    # Extract font name and flags
    buf = ctypes.create_string_buffer(256)
    flags = ctypes.c_int(0)
    result = pdfium_raw.FPDFText_GetFontInfo(raw_tp, char_idx, buf, 256, ctypes.byref(flags))
    if result > 0:
        font_name = buf.raw[:result].decode("utf-8", errors="replace").rstrip("\x00")
        flag_val = flags.value
        info["font_name"] = font_name
        info["flags"] = flag_val

        # Detect bold from font name (most reliable signal)
        name_lower = font_name.lower()
        info["is_bold"] = (
            "bold" in name_lower
            or "demi" in name_lower
            or "heavy" in name_lower
            or "black" in name_lower
            or bool(flag_val & (1 << 18))  # ForceBold flag
        )

        # Detect italic from font name + flag
        info["is_italic"] = (
            "italic" in name_lower
            or "oblique" in name_lower
            or bool(flag_val & (1 << 6))  # Italic flag
        )

    return info


def _get_text_rectangles(
    textpage: pdfium.PdfTextPage,
    *,
    combine_fragments: bool = True,
    y_tolerance: float = 2.0,
    strip_line_numbers: bool = False,
    removed: list[tuple[str, tuple[float, float, float, float], dict[str, Any]]] | None = None,
) -> list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]]:
    """Extract text rectangles from a textpage.

    Returns list of (text, (left, bottom, right, top), font_info).
    font_info is {"font_size": float, "rect_height": float} when available.
    Follows kelvin_pdf's extract_text_rectangles with combine_fragments.

    With ``strip_line_numbers`` the fragments of a pleading-paper
    line-number column (see :func:`_line_number_gutter`) are dropped before
    lines are built, so a number never glues onto the text line beside it.
    Dropped fragments are appended to ``removed`` when it is given.
    """
    raw_rects = _get_char_fragments(textpage)
    if strip_line_numbers and raw_rects:
        drop = _line_number_gutter(raw_rects)
        if drop:
            if removed is not None:
                removed.extend(raw_rects[i] for i in sorted(drop))
            raw_rects = [r for i, r in enumerate(raw_rects) if i not in drop]

    if not combine_fragments or not raw_rects:
        return [(text, bbox, fi) for text, bbox, fi in raw_rects]

    return _combine_rects(list(raw_rects), y_tolerance)


# Pleading-paper line numbers (court filings, bills): a column of
# number-only fragments in the left margin, one per ruled line, counting
# 1, 2, 3, ... down the page at a constant pitch. Detection is per page and
# purely geometric: the column must be narrow, aligned (left or right edge)
# within ``_GUTTER_X_TOLERANCE``, at least ``_GUTTER_MIN_ROWS`` rows of increasing
# values whose spacing matches one pitch per step, starting at most at
# ``_GUTTER_MAX_FIRST_VALUE`` and spanning ``_GUTTER_MIN_SPAN_SHARE`` of the
# page's text height, with no other text crossing its x-band over its
# height and nearly all of the page's text in that height to its right.
# Numbered paragraphs, list labels, footnote numbers and table row-number
# columns fail at least one of these: their numbers share a fragment with
# text, sit at the body's left edge where other lines start, do not keep one
# pitch per step, or do not start near 1 and run down the page.
_GUTTER_MIN_ROWS = 8
_GUTTER_MAX_VALUE = 99
_GUTTER_MAX_WIDTH = 30.0
_GUTTER_X_TOLERANCE = 4.0
_GUTTER_ROW_Y_TOLERANCE = 2.0
_GUTTER_PITCH_TOLERANCE = 0.2
_GUTTER_MIN_STEP_ONE_SHARE = 0.75
_GUTTER_MIN_BODY_RIGHT_SHARE = 0.8
_GUTTER_MAX_FIRST_VALUE = 3
_GUTTER_MAX_OUTSIDE_CROSSING_SHARE = 0.15
_GUTTER_MIN_OUTSIDE_CROSSING_ROWS = 3
_GUTTER_MIN_SPAN_SHARE = 0.25


def _line_number_gutter(
    frags: Sequence[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]],
) -> set[int]:
    """Return indices of one page's pleading-paper line-number fragments.

    ``frags`` are the page's char fragments (:func:`_get_char_fragments`).
    The result is empty unless a whole line-number column is found; see the
    module constants above for the rules.
    """
    cands = _gutter_candidates(frags)
    if len(cands) < _GUTTER_MIN_ROWS:
        return set()
    page_extent = max(max(b[1], b[3]) for _t, b, _f in frags) - min(
        min(b[1], b[3]) for _t, b, _f in frags
    )

    # Cluster by alignment: line numbers are right-aligned ("9" / "10") or
    # left-aligned; two numbers share a column when either edge lines up.
    parent = list(range(len(cands)))

    def _root(k: int) -> int:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    # In one dimension, linking neighbours in sorted order gives the same
    # groups as comparing every pair, in O(n log n).
    for edge in (2, 3):
        order = sorted(range(len(cands)), key=lambda k: cands[k][edge])
        for a, b in itertools.pairwise(order):
            if cands[b][edge] - cands[a][edge] <= _GUTTER_X_TOLERANCE:
                parent[_root(a)] = _root(b)
    by_root: dict[int, list[tuple[tuple[int, ...], int, float, float, float]]] = {}
    for k, c in enumerate(cands):
        by_root.setdefault(_root(k), []).append(c)
    clusters = list(by_root.values())

    drop: set[int] = set()
    for cluster in clusters:
        if len(cluster) < _GUTTER_MIN_ROWS:
            continue
        run = _gutter_run(cluster)
        if run is None:
            continue
        # Line numbers restart near 1 on every page and run down most of the
        # sheet; a stack of one-line footnotes ("4", "5", ... "12") does not.
        if run[0][0] > _GUTTER_MAX_FIRST_VALUE:
            continue
        if run[0][1] - run[-1][1] < _GUTTER_MIN_SPAN_SHARE * page_extent:
            continue
        garbled = _gutter_margin_extras(run, frags)
        if garbled is not None:
            drop.update(i for row in run for i in row[2])
            drop.update(garbled)
    return drop


# Some producers draw each digit of "10" as its own text object; digits on
# one row closer together than this share of the digit height form one
# number (a centred "1" leaves a wide gap: "1" + "1" is 3.4 pt apart at 9.5 pt).
_GUTTER_DIGIT_JOIN_GAP = 0.6


def _gutter_candidates(
    frags: Sequence[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]],
) -> list[tuple[tuple[int, ...], int, float, float, float]]:
    """Number-only fragments of a page as ``(indices, value, left, right, y)``.

    Adjacent single-digit pieces on one row are joined into one number; an
    overlaid copy of the same number (a doubled glyph) joins its original.
    """
    # (index, digits, left, right, y-center, height)
    pieces: list[tuple[int, str, float, float, float, float]] = []
    for i, (text, (left, bottom, right, top), _fi) in enumerate(frags):
        tokens = text.split()
        # A digit piece ("0" of a split "10") or a whole, possibly doubled,
        # number; the value range is checked once pieces are joined.
        if not tokens or len(set(tokens)) != 1 or not (tokens[0].isascii() and tokens[0].isdigit()):
            continue
        lo, hi = min(left, right), max(left, right)
        if hi > lo:
            pieces.append((i, tokens[0], lo, hi, (bottom + top) / 2.0, abs(top - bottom)))
    pieces.sort(key=lambda p: -p[4])
    rows: list[list[tuple[int, str, float, float, float, float]]] = []
    for p in pieces:
        if rows and abs(rows[-1][0][4] - p[4]) <= _GUTTER_ROW_Y_TOLERANCE:
            rows[-1].append(p)
        else:
            rows.append([p])

    cands: list[tuple[tuple[int, ...], int, float, float, float]] = []
    for row in rows:
        row.sort(key=lambda p: p[2])
        groups: list[tuple[list[int], str, float, float, float]] = []
        for i, digits, lo, hi, yc, height in row:
            if groups:
                ids, gdigits, glo, ghi, gyc = groups[-1]
                gap = lo - ghi
                if gap >= -1.0 and gap <= _GUTTER_DIGIT_JOIN_GAP * height and lo > glo + 1.0:
                    groups[-1] = ([*ids, i], gdigits + digits, glo, hi, gyc)
                    continue
                if digits == gdigits and lo < ghi:
                    groups[-1] = ([*ids, i], gdigits, glo, max(ghi, hi), gyc)
                    continue
            groups.append(([i], digits, lo, hi, yc))
        for ids, digits, lo, hi, yc in groups:
            if len(digits) > 2 or digits.startswith("0"):
                continue
            value = int(digits)
            if 1 <= value <= _GUTTER_MAX_VALUE and hi - lo <= _GUTTER_MAX_WIDTH:
                cands.append((tuple(ids), value, lo, hi, yc))
    return cands


def _gutter_run(
    cluster: list[tuple[tuple[int, ...], int, float, float, float]],
) -> list[tuple[int, float, list[int], float, float]] | None:
    """Longest constant-pitch run of increasing values in one x-cluster.

    Returns rows ``(value, y_center, fragment indices, left, right)`` top to
    bottom, or ``None`` when no run reaches ``_GUTTER_MIN_ROWS``.
    """
    # Rows top to bottom (PDF y grows upward); a doubled glyph at the same
    # height with the same value joins its row.
    rows: list[tuple[int, float, list[int], float, float]] = []
    for idx, value, lo, hi, yc in sorted(cluster, key=lambda c: -c[4]):
        if rows and abs(rows[-1][1] - yc) <= _GUTTER_ROW_Y_TOLERANCE and rows[-1][0] == value:
            v, y, ids, rlo, rhi = rows[-1]
            rows[-1] = (v, y, [*ids, *idx], min(rlo, lo), max(rhi, hi))
        else:
            rows.append((value, yc, list(idx), lo, hi))
    if len(rows) < _GUTTER_MIN_ROWS:
        return None

    steps = [
        (rows[k + 1][0] - rows[k][0], rows[k][1] - rows[k + 1][1]) for k in range(len(rows) - 1)
    ]
    ratios = [gap / dv for dv, gap in steps if dv >= 1 and gap > 0]
    if not ratios:
        return None
    pitch = _median(ratios)
    if pitch <= 0:
        return None

    def _ok(step: tuple[int, float]) -> bool:
        dv, gap = step
        return dv >= 1 and abs(gap / dv - pitch) <= _GUTTER_PITCH_TOLERANCE * pitch

    best: tuple[int, int] = (0, 0)  # [start, end) row range
    start = 0
    for k, step in enumerate(steps):
        if not _ok(step):
            start = k + 1
            continue
        if (k + 2) - start > best[1] - best[0]:
            best = (start, k + 2)
    run = rows[best[0] : best[1]]
    if len(run) < _GUTTER_MIN_ROWS:
        return None
    run_steps = [run[k + 1][0] - run[k][0] for k in range(len(run) - 1)]
    if sum(1 for dv in run_steps if dv == 1) < _GUTTER_MIN_STEP_ONE_SHARE * len(run_steps):
        return None
    return run


# OCR layers misread a line number now and then ("io" for "10"); a fragment
# of at most this many characters lying inside the column's x-band is taken
# as one of its entries.
_GUTTER_MAX_GARBLED_CHARS = 3


def _gutter_margin_extras(
    run: list[tuple[int, float, list[int], float, float]],
    frags: Sequence[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]],
) -> set[int] | None:
    """Check that a number run is a left-margin column rather than body text.

    No other fragment may cross the run's x-band within its height, at
    least ``_GUTTER_MIN_BODY_RIGHT_SHARE`` of the horizontal text there must
    lie to its right (vertical sidebar text is not counted), and above or
    below it text crossing the band may not exceed
    ``_GUTTER_MAX_OUTSIDE_CROSSING_SHARE`` of the page's horizontal text on
    ``_GUTTER_MIN_OUTSIDE_CROSSING_ROWS`` or more rows (one or two full-width
    caption, stamp or footer lines are fine; running paragraphs are not).
    Returns
    ``None`` when the run is not a margin column, otherwise the indices of
    short fragments lying inside the band (misread line numbers), which
    belong to the column too.
    """
    band_lo = min(r[3] for r in run)
    band_hi = max(r[4] for r in run)
    pitch = (run[0][1] - run[-1][1]) / max(run[-1][0] - run[0][0], 1)
    y_top = run[0][1] + pitch / 2.0
    y_bottom = run[-1][1] - pitch / 2.0
    run_ids = {i for r in run for i in r[2]}
    garbled: set[int] = set()
    right_chars = 0
    total_chars = 0
    page_chars = 0
    crossing_chars = 0
    crossing_rows: set[int] = set()
    for i, (text, (left, bottom, right, top), _fi) in enumerate(frags):
        stripped = text.strip()
        if i in run_ids or not stripped:
            continue
        lo, hi = min(left, right), max(left, right)
        yc = (bottom + top) / 2.0
        horizontal = abs(top - bottom) <= 2.0 * (hi - lo)
        if horizontal:
            page_chars += len(stripped)
        if not y_bottom <= yc <= y_top:
            # Above or below the column: body lines that start at or left
            # of it (a numbered list inside running text) mean it is not a
            # margin; a lone full-width footer or caption line is allowed.
            if horizontal and lo < band_hi - 0.5 and hi > band_lo + 0.5:
                crossing_chars += len(stripped)
                crossing_rows.add(round(yc))
            continue
        if lo < band_hi - 0.5 and hi > band_lo + 0.5:
            if (
                lo >= band_lo - 1.0
                and hi <= band_hi + 1.0
                and len(stripped) <= _GUTTER_MAX_GARBLED_CHARS
            ):
                garbled.add(i)
                continue
            return None  # other text crosses the column: not a margin
        if not horizontal:
            continue  # vertical margin text (a rotated sidebar) is not body
        total_chars += len(stripped)
        if lo >= band_hi - 0.5:
            right_chars += len(stripped)
    if total_chars and right_chars < _GUTTER_MIN_BODY_RIGHT_SHARE * total_chars:
        return None
    if (
        len(crossing_rows) >= _GUTTER_MIN_OUTSIDE_CROSSING_ROWS
        and crossing_chars > _GUTTER_MAX_OUTSIDE_CROSSING_SHARE * page_chars
    ):
        return None
    return garbled


def _page_chars(raw_tp: Any, n_chars: int) -> list[str]:
    """Return one string per PDFium char index (``FPDFText_GetUnicode``).

    ``FPDFText_GetText`` is not index-aligned with the char list: it drops
    characters whose Unicode value is 0 (unmapped glyphs) and rewrites the
    U+0002 hyphen marker as U+FFFE, so on some pages (Census tables with
    unmapped dashes) its offsets drift away from char indices. Reading each
    char keeps text and boxes aligned. A UTF-16 surrogate pair becomes one
    character at the first index and an empty string at the second; an
    unpaired surrogate becomes U+FFFD.
    """
    cps = [pdfium_raw.FPDFText_GetUnicode(raw_tp, i) for i in range(n_chars)]
    out: list[str] = []
    i = 0
    while i < n_chars:
        cp = cps[i]
        if 0xD800 <= cp <= 0xDBFF and i + 1 < n_chars and 0xDC00 <= cps[i + 1] <= 0xDFFF:
            out.append(chr(0x10000 + ((cp - 0xD800) << 10) + (cps[i + 1] - 0xDC00)))
            out.append("")
            i += 2
            continue
        out.append("\ufffd" if 0xD800 <= cp <= 0xDFFF else chr(cp))
        i += 1
    return out


def _get_char_fragments(
    textpage: pdfium.PdfTextPage,
) -> list[tuple[str, tuple[float, float, float, float], dict[str, Any]]]:
    """Split a page's characters into text fragments, each with exact text.

    A fragment is a maximal run of characters from one page text object on
    one PDFium text line (PDFium separates lines with generated CR/LF). This
    is the same granularity as ``FPDFText_GetRect`` rectangles, but the text
    comes from the characters' own indices instead of
    ``FPDFText_GetBoundedText(rect)``. The bounded query returns every
    character whose box touches the rectangle, so neighbours of adjacent or
    overlapping rects were extracted twice (``"forward, I "`` then
    ``" I "``, ``"h his"``, ``"fifive"``).

    Returns ``(text, (left, bottom, right, top), font_info)`` tuples in
    PDFium's character order; ``font_info["line_id"]`` numbers PDFium's text
    lines so :func:`_combine_rects` can keep a line together even when its
    words sit at slightly different baselines (OCR layers). Generated spaces stay in the text of the
    fragment they follow; whitespace does not extend a fragment's box.
    """
    import ctypes

    n_chars = textpage.count_chars()
    if n_chars <= 0:
        return []
    raw_tp = textpage.raw
    text = _page_chars(raw_tp, n_chars)

    frags: list[tuple[str, tuple[float, float, float, float], dict[str, Any]]] = []
    chars: list[str] = []
    box: list[float] | None = None
    first_idx: int | None = None
    cur_obj: int | None = None
    line_id = 0
    line_ref: list[float] | None = None  # box of the current line's first fragment

    def _flush() -> None:
        nonlocal chars, box, first_idx, line_id, line_ref
        if box is not None and first_idx is not None:
            if line_ref is None:
                line_ref = list(box)
            elif box[1] >= line_ref[3] or box[3] <= line_ref[1]:
                # No vertical overlap with the line so far: PDFium kept going
                # on one text line across a visual line break.
                line_id += 1
                line_ref = list(box)
            fi = _font_info_for_char(textpage, first_idx)
            fi["rect_height"] = abs(box[3] - box[1])
            fi["line_id"] = line_id
            frags.append(("".join(chars), (box[0], box[1], box[2], box[3]), fi))
        elif chars and frags:
            # Whitespace-only run: keep it with the previous fragment.
            t, b, f = frags[-1]
            frags[-1] = (t + "".join(chars), b, f)
        chars = []
        box = None
        first_idx = None

    for i in range(n_chars):
        ch = text[i]
        if not ch:
            continue  # second half of a surrogate pair
        if ch in "\r\n":
            _flush()
            if ch == "\n":
                line_id += 1
                line_ref = None
            continue
        if pdfium_raw.FPDFText_IsGenerated(raw_tp, i):
            chars.append(ch)
            continue
        obj = ctypes.cast(pdfium_raw.FPDFText_GetTextObject(raw_tp, i), ctypes.c_void_p).value
        if obj != cur_obj:
            if box is not None:
                _flush()
            cur_obj = obj
        chars.append(ch)
        if ch.isspace():
            continue
        if pdfium_raw.FPDFText_IsHyphen(raw_tp, i):
            # PDFium's end-of-line hyphen: its page text continues the word
            # without a line break, but the next characters are on the next
            # visual line.
            left, bottom, right, top = textpage.get_charbox(i)
            if box is None:
                box = [left, bottom, right, top]
                first_idx = i
            _flush()
            line_id += 1
            line_ref = None
            cur_obj = None
            continue
        left, bottom, right, top = textpage.get_charbox(i)
        if right < left or top < bottom or (right == left and top == bottom == 0):
            continue
        if box is None:
            box = [left, bottom, right, top]
            first_idx = i
        else:
            box[0] = min(box[0], left)
            box[1] = min(box[1], bottom)
            box[2] = max(box[2], right)
            box[3] = max(box[3], top)
    _flush()
    return frags


def _detect_char_level_columns(
    textpage: pdfium.PdfTextPage,
    *,
    page_width: float = 612.0,
    min_gutter_width: float = 8.0,
) -> list[tuple[float, float]] | None:
    """Detect column boundaries using character-level bounding boxes.

    Instead of relying on text rectangles (which can span column gutters),
    this extracts individual character positions and builds a horizontal
    projection profile to find column gutters.

    Returns list of (left, right) column boundaries, or None if single column
    or detection fails.
    """
    n_chars = textpage.count_chars()
    if n_chars <= 0:
        return None

    # Extract character X positions (using char-level bounding boxes)
    # We sample characters to keep this fast for long documents
    max_chars = min(n_chars, 5000)
    step = max(1, n_chars // max_chars)

    char_x_positions: list[float] = []
    for i in range(0, n_chars, step):
        try:
            left, _bottom, right, _top = textpage.get_charbox(i)
            if right > left and right - left < page_width * 0.5:
                # Record the center-x of each character
                char_x_positions.append((left + right) / 2)
        except (OSError, AttributeError, ValueError, RuntimeError):
            continue

    if len(char_x_positions) < 20:
        return None

    # Build horizontal projection profile: histogram of character X positions
    # Use 1-point resolution bins
    x_min = min(char_x_positions)
    x_max = max(char_x_positions)
    content_width = x_max - x_min
    if content_width < 100:
        return None

    resolution = 1.0
    n_bins = int(content_width / resolution) + 1
    profile = [0] * n_bins

    for x in char_x_positions:
        bin_idx = int((x - x_min) / resolution)
        if 0 <= bin_idx < n_bins:
            profile[bin_idx] += 1

    # Find valleys (gaps) in the profile — potential column gutters
    # A valley is a contiguous run of zero or near-zero bins
    if not profile:
        return None

    # Adaptive threshold: bins with count <= 5% of max are "empty"
    max_count = max(profile)
    if max_count == 0:
        return None
    empty_threshold = max(1, int(max_count * 0.05))

    # Find contiguous runs of "empty" bins
    valleys: list[tuple[int, int, int]] = []  # (start_bin, end_bin, width)
    run_start: int | None = None
    for i, count in enumerate(profile):
        if count <= empty_threshold:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None:
                width = i - run_start
                if width >= int(min_gutter_width / resolution):
                    valleys.append((run_start, i, width))
                run_start = None
    if run_start is not None:
        width = n_bins - run_start
        if width >= int(min_gutter_width / resolution):
            valleys.append((run_start, n_bins, width))

    if not valleys:
        return None

    # Filter: valleys must be in the interior (not at edges)
    interior_margin = int(content_width * 0.1 / resolution)
    interior_valleys = [
        v for v in valleys if v[0] > interior_margin and v[1] < n_bins - interior_margin
    ]

    if not interior_valleys:
        return None

    # Sort by width (widest first) — the most prominent gutter
    interior_valleys.sort(key=lambda v: -v[2])

    # Build column boundaries from the widest gutter(s)
    # For now, use only the widest gutter (2-column detection)
    # TODO: support 3+ columns by using multiple valleys
    gutter = interior_valleys[0]

    # Column 1: content_left to gutter_left, Column 2: gutter_right to content_right
    col1_right = x_min + gutter[0] * resolution
    col2_left = x_min + gutter[1] * resolution

    columns = [
        (x_min, col1_right),
        (col2_left, x_max),
    ]

    # If there are more prominent gutters, add more columns
    if len(interior_valleys) >= 2:
        # Sort all used gutters by position
        used_gutters = sorted(interior_valleys[:3], key=lambda v: v[0])
        boundaries = [x_min]
        for g in used_gutters:
            boundaries.append(x_min + g[0] * resolution)
            boundaries.append(x_min + g[1] * resolution)
        boundaries.append(x_max)
        columns = [(boundaries[i], boundaries[i + 1]) for i in range(0, len(boundaries) - 1, 2)]

    # Validate: each column should contain a reasonable number of characters
    min_chars_per_col = len(char_x_positions) * 0.1
    valid_columns = []
    for col_left, col_right in columns:
        count = sum(1 for x in char_x_positions if col_left <= x <= col_right)
        if count >= min_chars_per_col:
            valid_columns.append((col_left, col_right))

    if len(valid_columns) <= 1:
        return None

    return valid_columns


def _get_text_rectangles_column_aware(
    textpage: pdfium.PdfTextPage,
    *,
    page_width: float = 612.0,
    combine_fragments: bool = True,
    y_tolerance: float = 2.0,
    min_gutter_width: float = 15.0,
    merge_column_paragraphs: bool = False,
    join_hyphenated_lines: bool = False,
    hyphen_resolver: HyphenResolver | None = None,
    reading_order: ReadingOrder = "geometric",
    strip_line_numbers: bool = False,
    removed: list[tuple[str, tuple[float, float, float, float], dict[str, Any]]] | None = None,
) -> list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]]:
    """Extract text rectangles with column detection.

    ``strip_line_numbers`` and ``removed`` are passed to
    :func:`_get_text_rectangles` (pleading-paper line-number columns).

    With ``reading_order`` ``"stream"`` (or ``"auto"`` when
    :func:`_stream_order_is_plausible` accepts the page), lines come straight
    from PDFium's text lines in content-stream order instead; see
    :func:`_stream_lines`.

    Uses a two-tier approach:
    1. Character-level column detection: extracts individual character bounding
       boxes to find column gutters (most robust, handles text runs spanning gutters)
    2. Fallback to XY-Cut++ pre-masking on text rectangles if char-level fails

    Within detected columns, fragments are combined and sorted independently.
    Full-width elements (titles, etc.) are processed separately and re-inserted
    at their Y position.
    """
    from kaos_content.layout import TextBlock as LTextBlock
    from kaos_content.layout import detect_columns

    # Get raw uncombined rectangles
    # Layout decisions (stream-order plausibility, column detection) are
    # made on all fragments, line numbers included, so stripping a
    # pleading-paper line-number column only removes its numbers and never
    # changes how the rest of the page is ordered.
    raw_all = _get_text_rectangles(textpage, combine_fragments=False, y_tolerance=y_tolerance)
    if not raw_all:
        return []
    raw = raw_all
    if strip_line_numbers:
        drop = _line_number_gutter(raw_all)
        if drop:
            if removed is not None:
                removed.extend((t, b, f or {}) for i, (t, b, f) in enumerate(raw_all) if i in drop)
            raw = [r for i, r in enumerate(raw_all) if i not in drop]
            if not raw:
                return []

    if combine_fragments and reading_order != "geometric":
        stream = _stream_lines(raw)
        plausible = _stream_order_is_plausible(stream if raw is raw_all else _stream_lines(raw_all))
        if reading_order == "stream" or plausible:
            if join_hyphenated_lines:
                stream = _join_hyphenated_line_rects(stream, resolver=hyphen_resolver)
            if merge_column_paragraphs:
                stream = _merge_column_paragraphs(stream, keep_order=True)
            return stream

    # Build TextBlocks for column detection
    layout_blocks: list[LTextBlock] = []
    for text, (left, bottom, right, top), _fi in raw_all:
        if text.strip():
            layout_blocks.append(
                LTextBlock(
                    left=left,
                    top=min(top, bottom),
                    right=right,
                    bottom=max(top, bottom),
                    text=text,
                )
            )

    if not layout_blocks:
        return (
            raw
            if not combine_fragments
            else _get_text_rectangles(
                textpage,
                combine_fragments=True,
                y_tolerance=y_tolerance,
                strip_line_numbers=strip_line_numbers,
            )
        )

    # --- XY-Cut++ pre-masking: separate full-width from narrow rects ---
    content_left = min(b.left for b in layout_blocks)
    content_right = max(b.right for b in layout_blocks)
    content_width = content_right - content_left
    fullwidth_threshold = content_width * 0.6

    narrow_blocks: list[LTextBlock] = []
    for b in layout_blocks:
        if b.width < fullwidth_threshold:
            narrow_blocks.append(b)

    if not narrow_blocks:
        return _get_text_rectangles(
            textpage,
            combine_fragments=combine_fragments,
            y_tolerance=y_tolerance,
            strip_line_numbers=strip_line_numbers,
        )

    # --- Tier 1: Character-level column detection ---
    char_columns = _detect_char_level_columns(
        textpage, page_width=page_width, min_gutter_width=min_gutter_width * 0.5
    )

    # --- Tier 2: Rect-level column detection (fallback) ---
    if char_columns is None:
        narrow_widths = sorted(b.width for b in narrow_blocks)
        median_block_width = narrow_widths[len(narrow_widths) // 2] if narrow_widths else 50.0
        adaptive_gutter = max(min_gutter_width, median_block_width * 0.15)

        col_result = detect_columns(
            narrow_blocks, page_width=page_width, min_gutter_width=adaptive_gutter
        )

        if len(col_result.columns) <= 1:
            return _get_text_rectangles(
                textpage,
                combine_fragments=combine_fragments,
                y_tolerance=y_tolerance,
                strip_line_numbers=strip_line_numbers,
            )
        detected_columns = list(col_result.columns)
    else:
        detected_columns = char_columns

    # Separate full-width rects from column-assigned rects
    fullwidth_rects: list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]] = []
    column_rects: dict[
        int, list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]]
    ] = {i: [] for i in range(len(detected_columns))}

    for text, (left, bottom, right, top), fi in raw:
        if not text.strip():
            continue
        rect_width = abs(right - left)
        if rect_width >= fullwidth_threshold:
            fullwidth_rects.append((text, (left, bottom, right, top), fi))
        else:
            x_center = (left + right) / 2
            assigned = False
            for ci, (col_left, col_right) in enumerate(detected_columns):
                if col_left <= x_center <= col_right:
                    column_rects[ci].append((text, (left, bottom, right, top), fi))
                    assigned = True
                    break
            if not assigned:
                # Assign to nearest column
                min_dist = float("inf")
                best_col = 0
                for ci, (col_left, col_right) in enumerate(detected_columns):
                    col_center = (col_left + col_right) / 2
                    dist = abs(x_center - col_center)
                    if dist < min_dist:
                        min_dist = dist
                        best_col = ci
                column_rects[best_col].append((text, (left, bottom, right, top), fi))

    # Process each column: combine fragments, sort top-to-bottom
    col_outputs: list[
        list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]]
    ] = []
    for ci in range(len(detected_columns)):
        rects = column_rects[ci]
        if not rects:
            col_outputs.append([])
            continue
        if combine_fragments:
            line_rects = _combine_rects(rects, y_tolerance)
            if join_hyphenated_lines:
                # Before paragraph merging, which would put a space
                # between the marker and the continuation.
                line_rects = _join_hyphenated_line_rects(line_rects, resolver=hyphen_resolver)
            if merge_column_paragraphs:
                line_rects = _merge_column_paragraphs(line_rects)
            col_outputs.append(line_rects)
        else:
            col_outputs.append(rects)

    # Combine full-width rects with column output, sorted by Y position.
    # Full-width elements are inserted at their Y position between column blocks.
    fullwidth_combined = _combine_rects(fullwidth_rects, y_tolerance) if fullwidth_rects else []

    # Interleave: first all column blocks in reading order (col 0 top-to-bottom,
    # then col 1, etc.), with full-width blocks inserted at their Y positions.
    result: list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]] = []
    for col_out in col_outputs:
        result.extend(col_out)

    # Insert full-width blocks at correct Y positions
    for fw_rect in fullwidth_combined:
        fw_y = max(fw_rect[1][1], fw_rect[1][3])  # top of full-width rect
        # Find insertion point in result
        insert_at = len(result)
        for i, (_, (_, b, _, t), _) in enumerate(result):
            if max(b, t) > fw_y:
                insert_at = i
                break
        result.insert(insert_at, fw_rect)

    return result


def _detect_page_tables(
    blocks: list[TextBlock],
    *,
    min_cols: int = 3,
    min_rows: int = 3,
    alignment_tolerance: float = 5.0,
    y_tolerance: float = 3.0,
) -> list[tuple[float, float, float, float]]:
    """Detect table regions on a page using text alignment patterns.

    Conservative detection to avoid false positives from multi-column layout:
    - Tables must have >= 3 columns (2-col is too easily confused with doc layout)
    - Tables must span < 60% of content width (full-width = likely columns)
    - Rows must have highly consistent column count (>= 70% within ±1)
    - Each row's cells must have short text (long paragraphs are not table cells)
    """
    from kaos_content.layout import detect_table_regions, group_into_lines

    if len(blocks) < min_rows * min_cols:
        return []

    content_left = min(b.left for b in blocks)
    content_right = max(b.right for b in blocks)
    content_width = content_right - content_left
    if content_width < 50:
        return []

    regions = detect_table_regions(
        blocks,
        min_cols=min_cols,
        min_rows=min_rows,
        alignment_tolerance=alignment_tolerance,
        y_tolerance=y_tolerance,
    )

    filtered: list[tuple[float, float, float, float]] = []
    for region in regions:
        tl, _tt, tr, _tb = region
        region_width = tr - tl
        # Reject if too wide — likely document columns
        if region_width / content_width > 0.60:
            continue

        region_blocks = [
            b
            for b in blocks
            if (
                b.left >= tl - 5
                and b.right <= tr + 5
                and b.top >= region[1] - 5
                and b.bottom <= region[3] + 5
            )
        ]
        if len(region_blocks) < min_rows * min_cols:
            continue

        lines = group_into_lines(region_blocks, y_tolerance=y_tolerance)
        if len(lines) < min_rows:
            continue

        # Column count consistency check
        cell_counts = [len(line.blocks) for line in lines]
        if not cell_counts:
            continue
        median_count = sorted(cell_counts)[len(cell_counts) // 2]
        if median_count < min_cols:
            continue
        consistent = sum(1 for c in cell_counts if abs(c - median_count) <= 1)
        if consistent / len(cell_counts) < 0.7:
            continue

        # Short text check: table cells should be short
        # If most cells have >50 chars, it's probably flowing text, not a table
        all_texts = [b.text.strip() for b in region_blocks]
        long_cells = sum(1 for t in all_texts if len(t) > 50)
        if long_cells / len(all_texts) > 0.5:
            continue

        filtered.append(region)

    return filtered


def _build_table_rows_from_blocks(
    blocks: list[TextBlock],
    *,
    y_tolerance: float = 3.0,
) -> list[list[str]]:
    """Convert a list of TextBlocks into table rows (list of cell strings).

    Groups blocks into lines (rows), sorts cells left-to-right within each row.
    Normalizes column count by padding shorter rows with empty strings.
    """
    from kaos_content.layout import group_into_lines

    lines = group_into_lines(blocks, y_tolerance=y_tolerance)
    if not lines:
        return []

    rows: list[list[str]] = []
    max_cols = 0
    for line in lines:
        cells = sorted(line.blocks, key=lambda b: b.left)
        row = [b.text.strip() for b in cells]
        rows.append(row)
        max_cols = max(max_cols, len(row))

    # Pad rows to consistent column count
    for row in rows:
        while len(row) < max_cols:
            row.append("")

    return rows


# Sentence-terminating punctuation. A line ending in any of these is
# treated as a paragraph boundary; a line that doesn't end with one is
# (under `_merge_column_paragraphs`) treated as a column-wrap continuation.
# Inclusion of `;` is conservative — semicolon-terminated clauses can
# legitimately wrap, but we treat them as boundaries to avoid over-merging
# enumerated lists. Exclusion of comma is deliberate: comma-wrapped lists
# in 2-column PDFs are exactly the case we want to merge.
_PARA_TERMINATORS = (".", "!", "?", ":", ";", "”", '"', "'")


def _merge_column_paragraphs(
    rects: list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]],
    *,
    max_gap_ratio: float = 0.8,
    max_font_size_jump: float = 0.5,
    min_lines: int = 2,
    keep_order: bool = False,
) -> list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]]:
    """Merge consecutive single-line rects in the same column into paragraphs.

    Multi-column PDFs (Federal Register, court reporters, GPO publications)
    extract each visual line as its own text rectangle. Downstream NLP that
    treats each rect as a paragraph then sees fragments instead of flowing
    text — every line of a column-wrapped paragraph appears short and
    heading-shaped, so the structure layer over-fires on heading detection.

    This helper merges adjacent line-rects when the previous line does NOT
    end in sentence-terminating punctuation AND the vertical gap to the next
    line is at most ``max_gap_ratio`` times the current line's height. Both
    constraints must hold; either alone gives unsafe merges.

    The bbox of the merged rect is the union of the input bboxes; the
    metadata (font_size, etc.) is inherited from the first line.

    Conservative by design — line-rects that look like headings (different
    font size, terminal punctuation, or a vertical gap larger than the
    typical paragraph spacing) are never merged. Always-safe to enable for
    multi-column PDFs; for single-column PDFs the merge is largely a no-op
    because line spacing already separates paragraphs.

    Args:
        rects: rects already grouped into single visual lines (output of
            ``_combine_rects``), in any order.
        max_gap_ratio: maximum allowed gap-to-line-height ratio for a
            merge. Default 0.8 — same-paragraph spacing is typically
            ≤ 0.3-0.5x line height; ≥ 1.0x is paragraph-break territory.
            Keep below 1.0 to preserve paragraph boundaries.
        max_font_size_jump: maximum allowed font-size delta in points
            between consecutive lines for a merge. Default 0.5pt — page
            headers/folios that sit visually close to body text but use
            a different font size are reliably caught by this guard.
        min_lines: minimum number of consecutive merge-eligible lines
            required before any merging happens. ``min_lines=2`` means at
            least one merge is attempted; raising this reduces aggressive
            merging but offers little upside.
        keep_order: keep the input order (lines already in reading order,
            e.g. PDFium stream order) instead of sorting top-to-bottom. A
            line is then only merged into the previous one when it sits
            below it and overlaps it horizontally (the next line in stream
            order may start another column).

    Returns:
        New rect list with column-wrap fragments coalesced. Length ≤ input.
    """
    if len(rects) < min_lines:
        return list(rects)

    # Sort top-to-bottom (PDF coords: top is the larger Y), unless the caller
    # passes lines already in reading order (PDFium stream order).
    sorted_rects = (
        list(rects) if keep_order else sorted(rects, key=lambda r: -max(r[1][1], r[1][3]))
    )

    merged: list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]] = []
    cur_text: str = ""
    cur_bbox: list[float] = []
    cur_meta: dict[str, Any] | None = None
    cur_bottom: float = 0.0
    cur_lh: float = 0.0

    def _flush() -> None:
        if cur_text:
            merged.append(
                (cur_text, (cur_bbox[0], cur_bbox[1], cur_bbox[2], cur_bbox[3]), cur_meta)
            )

    for text, bbox, meta in sorted_rects:
        line_top = max(bbox[1], bbox[3])
        line_bottom = min(bbox[1], bbox[3])
        line_h = line_top - line_bottom

        if not cur_text:
            cur_text = text
            cur_bbox = [bbox[0], bbox[1], bbox[2], bbox[3]]
            cur_meta = meta
            cur_bottom = line_bottom
            cur_lh = line_h
            continue

        gap = max(0.0, cur_bottom - line_top)
        prev_terminal = cur_text.rstrip().endswith(_PARA_TERMINATORS)
        # Font-size jump signals a heading / folio boundary — never merge.
        cur_fs = (cur_meta or {}).get("font_size", 0.0) or cur_lh
        new_fs = (meta or {}).get("font_size", 0.0) or line_h
        font_jump = abs(new_fs - cur_fs) > max_font_size_jump

        # In stream order the next line may sit in another column: only merge
        # a line that is below the current one and overlaps it horizontally.
        below = not keep_order or (
            line_top <= cur_bottom + 0.5 * cur_lh
            and bbox[0] < cur_bbox[2]
            and bbox[2] > cur_bbox[0]
        )
        if (not prev_terminal) and (gap <= max_gap_ratio * cur_lh) and (not font_jump) and below:
            cur_text = cur_text.rstrip() + " " + text.lstrip()
            cur_bbox[0] = min(cur_bbox[0], bbox[0])
            cur_bbox[1] = min(cur_bbox[1], bbox[1])
            cur_bbox[2] = max(cur_bbox[2], bbox[2])
            cur_bbox[3] = max(cur_bbox[3], bbox[3])
            cur_bottom = line_bottom
        else:
            _flush()
            cur_text = text
            cur_bbox = [bbox[0], bbox[1], bbox[2], bbox[3]]
            cur_meta = meta
            cur_bottom = line_bottom
            cur_lh = line_h

    _flush()
    return merged


def _resolve_text_markers(
    rects: list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]],
    *,
    join_hyphenated_lines: bool,
    hyphen_resolver: HyphenResolver | None = None,
    page_number: int,
) -> list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]]:
    """Resolve PDFium hyphen markers and control characters in one page's lines.

    ``rects`` are the page's lines in reading order. With
    ``join_hyphenated_lines`` the marker-ended lines are merged with their
    lowercase continuation first (this pass also catches continuations that
    cross a column boundary); then every line is cleaned with
    :func:`~kaos_pdf._text_markers.clean_line_text` so no marker or other C0
    control reaches the AST. Unmapped controls are dropped and logged.
    """
    if join_hyphenated_lines:
        rects = _join_hyphenated_line_rects(rects, resolver=hyphen_resolver)
    out: list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]] = []
    dropped = 0
    for text, bbox, fi in rects:
        cleaned, n = clean_line_text(text)
        dropped += n
        out.append((cleaned, bbox, fi))
    if dropped:
        logger.debug(
            "dropped %d unmapped control character(s) from page %d text", dropped, page_number
        )
    return out


def _line_from_fragments(
    frags: list[tuple[str, tuple[float, float, float, float], dict[str, Any]]],
    *,
    order_by_x: bool,
) -> tuple[str, tuple[float, float, float, float], dict[str, Any]]:
    """Build one line rect (text, union bbox, line font info) from fragments."""
    ordered = sorted(frags, key=lambda r: r[1][0]) if order_by_x else frags
    font_name = ""
    for _, _, f in frags:
        if f.get("font_name"):
            font_name = f["font_name"]
            break
    left = min(b[0] for _, b, _ in frags)
    bottom = min(b[1] for _, b, _ in frags)
    right = max(b[2] for _, b, _ in frags)
    top = max(b[3] for _, b, _ in frags)
    return (
        "".join(t for t, _, _ in ordered),
        (left, bottom, right, top),
        {
            "font_size": max(f.get("font_size", 0.0) for _, _, f in frags),
            "rect_height": abs(top - bottom),
            "is_bold": any(f.get("is_bold") for _, _, f in frags),
            "is_italic": any(f.get("is_italic") for _, _, f in frags),
            "font_name": font_name,
        },
    )


def _stream_lines(
    frags: list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]],
) -> list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]]:
    """Group char fragments into PDFium text lines, kept in content-stream order.

    Fragments from :func:`_get_char_fragments` arrive in PDFium's character
    order and carry ``font_info["line_id"]``. Consecutive fragments with one
    line id form one line; the text keeps PDFium's character order. This is
    the order alea-preprocess / KL3M extraction used, and for well-formed
    PDFs it is the authored reading order: column by column, without the
    interleaving that a global top-to-bottom sort produces when column
    detection misses a gutter.
    """
    lines: list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]] = []
    cur: list[tuple[str, tuple[float, float, float, float], dict[str, Any]]] = []
    cur_id: object = None
    for text, bbox, fi in frags:
        finfo = fi or {}
        lid = finfo.get("line_id")
        if cur and (lid is None or lid != cur_id):
            lines.append(_line_from_fragments(cur, order_by_x=False))
            cur = []
        cur.append((text, bbox, finfo))
        cur_id = lid
    if cur:
        lines.append(_line_from_fragments(cur, order_by_x=False))
    return lines


def _stream_order_is_plausible(
    lines: list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]],
    *,
    max_backward_ratio: float = 0.1,
    min_backward_allowed: int = 3,
) -> bool:
    """Return whether PDFium's line order reads plausibly for this page.

    A step from one line to the next is *forward* when the next line starts
    lower on the page (or on the same baseline), or moves up into a column
    to the right (its left edge is right of the current line's centre). Any
    other step goes back up the same column. Pages whose content stream is
    authored out of order (text drawn bottom-up, in random blocks) have many
    such steps and fall back to the geometric order. A few backward steps are
    normal: running headers and page numbers are often drawn last.
    """
    steps = 0
    backward = 0
    prev: tuple[float, float, float, float] | None = None
    for text, bbox, _fi in lines:
        if not text.strip():
            continue
        left, bottom, right, top = bbox
        if prev is not None:
            p_left, p_bottom, p_right, p_top = prev
            steps += 1
            height = max(1.0, p_top - p_bottom)
            goes_down = top <= p_top + 0.5 * height
            new_column = left > (p_left + p_right) / 2
            if not (goes_down or new_column):
                backward += 1
        prev = (left, bottom, right, top)
    if steps == 0:
        return True
    return backward <= max(min_backward_allowed, max_backward_ratio * steps)


def _combine_rects(
    rects: list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]],
    y_tolerance: float = 2.0,
) -> list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]]:
    """Combine text fragments into visual lines.

    Fragments that PDFium put on the same text line (same
    ``font_info["line_id"]``, set by :func:`_get_char_fragments`) always stay
    together: OCR text layers place words of one line at baselines a few
    points apart, and splitting them by y alone moved words onto lines of
    their own (``"Whereas it to this meeting"`` / ``"appears now"``). Those
    line groups, and fragments without a line id, are then merged when their
    vertical centres are within ``y_tolerance`` (the historical rule, which
    also joins separate text runs drawn on one baseline). Texts in a line
    are ordered left to right.
    """
    if not rects:
        return []

    # Group by PDFium line id (fragments without one form their own group).
    groups: list[list[tuple[str, tuple[float, float, float, float], dict[str, Any]]]] = []
    by_line: dict[int, int] = {}
    for text, bbox, fi in rects:
        finfo = fi or {}
        lid = finfo.get("line_id")
        if isinstance(lid, int) and lid in by_line:
            groups[by_line[lid]].append((text, bbox, finfo))
            continue
        if isinstance(lid, int):
            by_line[lid] = len(groups)
        groups.append([(text, bbox, finfo)])

    def _gbox(
        g: list[tuple[str, tuple[float, float, float, float], dict[str, Any]]],
    ) -> list[float]:
        return [
            min(b[0] for _, b, _ in g),
            min(b[1] for _, b, _ in g),
            max(b[2] for _, b, _ in g),
            max(b[3] for _, b, _ in g),
        ]

    boxed = [(_gbox(g), g) for g in groups]
    # Top-to-bottom, then left-to-right, by each group's union box.
    boxed.sort(key=lambda bg: (-bg[0][3], bg[0][0]))

    combined: list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]] = []
    cur: list[tuple[str, tuple[float, float, float, float], dict[str, Any]]] = []
    cb: list[float] = []
    current_y: float | None = None

    def _flush() -> None:
        if not cur:
            return
        ordered = sorted(cur, key=lambda r: r[1][0])
        line_text = "".join(t for t, _, _ in ordered)
        font_name = ""
        for _, _, f in cur:
            if f.get("font_name"):
                font_name = f["font_name"]
                break
        combined.append(
            (
                line_text,
                (cb[0], cb[1], cb[2], cb[3]),
                {
                    "font_size": max(f.get("font_size", 0.0) for _, _, f in cur),
                    "rect_height": abs(cb[3] - cb[1]),
                    "is_bold": any(f.get("is_bold") for _, _, f in cur),
                    "is_italic": any(f.get("is_italic") for _, _, f in cur),
                    "font_name": font_name,
                },
            )
        )

    for gb, g in boxed:
        yc = (gb[1] + gb[3]) / 2
        if current_y is not None and abs(yc - current_y) <= y_tolerance:
            cb = [min(cb[0], gb[0]), min(cb[1], gb[1]), max(cb[2], gb[2]), max(cb[3], gb[3])]
            cur.extend(g)
            continue
        _flush()
        cur = list(g)
        cb = list(gb)
        current_y = yc

    _flush()
    return combined


# ---------------------------------------------------------------------------
# Internal: heading detection
# ---------------------------------------------------------------------------


def _estimate_body_font_size(
    rects: list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]],
) -> tuple[float, str]:
    """Estimate the body text font size from text rectangles.

    Returns (median_size, size_source) where size_source is "font_size" or
    "rect_height". Uses font_size from pypdfium2 when it varies; falls back
    to rect_height when font_size is uniform (normalized coordinate systems).
    """
    font_sizes: list[float] = []
    rect_heights: list[float] = []

    for text, _bbox, font_info in rects:
        if not text.strip():
            continue
        if font_info:
            fs = font_info.get("font_size", 0)
            if fs > 0:
                font_sizes.append(fs)
            rh = font_info.get("rect_height", 0)
            if 3 < rh < 100:
                rect_heights.append(rh)

    # Check if font_size actually varies (not all the same value)
    if font_sizes and len({round(s, 1) for s in font_sizes}) > 1:
        font_sizes.sort()
        return font_sizes[len(font_sizes) // 2], "font_size"

    # Fall back to rect_height
    if rect_heights:
        rect_heights.sort()
        return rect_heights[len(rect_heights) // 2], "rect_height"

    return 12.0, "font_size"


def _classify_heading(
    text: str,
    font_info: dict[str, Any] | None,
    body_size: float,
    font_ratio: float,
    size_source: str = "font_size",
) -> int | None:
    """Classify text as a heading based on font size ratio vs body text.

    Uses the same size metric (font_size or rect_height) that was used to
    compute body_size, ensuring consistent comparison.
    Returns heading depth (1-6) or None.
    """
    if not font_info:
        return None

    text = text.strip()
    if not text or len(text) > 200:
        return None

    # Lines with many words are unlikely to be headings
    if len(text.split()) > 20:
        return None

    # Use the same metric that body_size was computed from
    size = font_info.get(size_source, 0)
    if size <= 0:
        return None

    ratio = size / body_size if body_size > 0 else 1.0

    if ratio < font_ratio:
        return None

    # Map ratio to heading depth
    if ratio >= 2.0:
        return 1
    elif ratio >= 1.6:
        return 2
    elif ratio >= 1.3:
        return 3
    elif ratio >= font_ratio:
        return 4
    return None


def _filter_headers_footers(
    all_page_rects: list[
        list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]]
    ],
    *,
    page_numbers: list[int] | None = None,
    page_heights: list[float] | None = None,
    page_rotations: list[int] | None = None,
    report: dict[str, int] | None = None,
) -> set[int]:
    """Identify header/footer/watermark blocks to exclude from output.

    Uses these strategies:
    1. Cross-page repeated content at a consistent position in the
       top/bottom zone (see :func:`_repeated_page_furniture`). The first
       occurrence is kept: a repeated table title, column-header row,
       station line or issue date is real content once.
    2. Running stamps and page numbers whose text changes from page to page
       only in a counter that follows the page number, e.g. the CM/ECF
       ``"Case 1:13-cv-00133 Document 244 Filed 01/08/14 Page 2 of 141"``
       band or a bare ``"2"`` (see :func:`_running_page_stamps`). Every
       occurrence is excluded, the first page's included.
    3. Court e-filing stamps in the top/bottom zone of any page, including
       one-page filings (see :func:`_is_ecf_stamp`).
    4. Vertical text repeated at the same place in a side margin, e.g. a
       rotated court-name sidebar (see :func:`_vertical_margin_furniture`).
    5. Pattern matching: known production-slug patterns (see
       :func:`_is_watermark`).

    ``page_numbers`` gives each entry of ``all_page_rects`` its page index
    (default: list position), ``page_heights`` its displayed height and
    ``page_rotations`` its ``/Rotate`` angle, used by strategies 2 and 3
    (default: the page's text extent, unrotated). When
    ``report`` is given, the number of blocks each strategy excluded is
    added under ``repeated_lines``, ``page_stamps``, ``ecf_stamps``,
    ``vertical_margin`` and ``watermarks``.

    Returns set of flat indices to exclude.
    """
    found: dict[str, set[int]] = {}

    if len(all_page_rects) >= 2:
        found["repeated_lines"] = _repeated_page_furniture(all_page_rects)
        found["page_stamps"] = _running_page_stamps(
            all_page_rects, page_numbers, page_heights, page_rotations
        )
        found["vertical_margin"] = _vertical_margin_furniture(all_page_rects)

    found["ecf_stamps"] = {
        idx
        for idx, _page, _zone, _rel, text in _edge_zone_blocks(
            all_page_rects, page_heights, page_rotations
        )
        if _is_ecf_stamp(text)
    }

    watermarks: set[int] = set()
    flat_idx = 0
    for page_rects in all_page_rects:
        for text, _bbox, _fi in page_rects:
            if _is_watermark(text):
                watermarks.add(flat_idx)
            flat_idx += 1
    found["watermarks"] = watermarks

    exclude: set[int] = set()
    for name, indices in found.items():
        if report is not None:
            # Count each block once, under the first strategy that took it.
            report[name] = report.get(name, 0) + len(indices - exclude)
        exclude.update(indices)
    return exclude


# Cross-page furniture detection: zone height and minimum page share follow
# the previous detect_headers_footers(zone_fraction=0.12, min_occurrence=0.4)
# call; the y tolerance is a fraction of the page's text height.
_FURNITURE_ZONE_FRACTION = 0.12
_FURNITURE_MIN_OCCURRENCE = 0.4
_FURNITURE_Y_TOLERANCE = 0.03
# Repeated number-only cells ("1-2", "60+") are table row labels, not furniture.
_FURNITURE_MIN_LETTERS = 3


def _repeated_page_furniture(
    all_page_rects: list[
        list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]]
    ],
) -> set[int]:
    """Return flat indices of running headers/footers, except their first page.

    A block is furniture when the same text (case-insensitive) sits in the
    top or bottom zone of at least ``_FURNITURE_MIN_OCCURRENCE`` of the
    pages (and at least two) at the same relative height, within
    ``_FURNITURE_Y_TOLERANCE``. The occurrence on the first such page is
    kept; later repeats are excluded. Text that repeats at different
    heights (e.g. headings that share a prefix) and text with fewer than
    ``_FURNITURE_MIN_LETTERS`` letters (numeric row labels) are not
    furniture.
    """
    n_pages = len(all_page_rects)
    min_pages = max(2, int(n_pages * _FURNITURE_MIN_OCCURRENCE))
    # (zone, normalized text) -> [(page, flat_idx, relative y-center)]
    occurrences: dict[tuple[str, str], list[tuple[int, int, float]]] = {}

    flat_idx = 0
    for page_no, page_rects in enumerate(all_page_rects):
        if not page_rects:
            continue
        lows = [min(b[1], b[3]) for _t, b, _f in page_rects]
        highs = [max(b[1], b[3]) for _t, b, _f in page_rects]
        page_low, page_high = min(lows), max(highs)
        height = page_high - page_low
        for (text, _bbox, _fi), low, high in zip(page_rects, lows, highs, strict=True):
            idx = flat_idx
            flat_idx += 1
            normalized = text.strip().lower()
            if height <= 0 or sum(c.isalpha() for c in normalized) < _FURNITURE_MIN_LETTERS:
                continue
            rel = ((low + high) / 2.0 - page_low) / height
            if rel < _FURNITURE_ZONE_FRACTION:
                zone = "low"
            elif rel > 1.0 - _FURNITURE_ZONE_FRACTION:
                zone = "high"
            else:
                continue
            occurrences.setdefault((zone, normalized), []).append((page_no, idx, rel))

    exclude: set[int] = set()
    for occs in occurrences.values():
        if len({page for page, _i, _y in occs}) < min_pages:
            continue
        ys = sorted(y for _p, _i, y in occs)
        anchor = ys[len(ys) // 2]
        aligned = [o for o in occs if abs(o[2] - anchor) <= _FURNITURE_Y_TOLERANCE]
        aligned_pages = {page for page, _i, _y in aligned}
        if len(aligned_pages) < min_pages:
            continue
        first_page = min(aligned_pages)
        exclude.update(idx for page, idx, _y in aligned if page != first_page)
    return exclude


def _edge_zone_blocks(
    all_page_rects: list[
        list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]]
    ],
    page_heights: list[float] | None = None,
    page_rotations: list[int] | None = None,
) -> list[tuple[int, int, str, float, str]]:
    """Blocks in a page's top or bottom zone.

    Returns ``(flat index, page position, zone, relative y-center, text)``
    for every non-empty block whose center lies in the top or bottom
    ``_FURNITURE_ZONE_FRACTION`` of the page. With ``page_heights`` (the
    displayed page height) the zone is measured on the physical page (court
    e-filing stamps sit at a fixed place on the sheet, and on a scanned
    exhibit the stamp is the page's only text); otherwise on the page's
    text extent. On a page displayed with a 90 or 270 degree
    ``page_rotations`` entry the displayed vertical is the x axis of the
    text coordinates, so the zone is measured along x there.
    """
    out: list[tuple[int, int, str, float, str]] = []
    flat_idx = 0
    for page_no, page_rects in enumerate(all_page_rects):
        if not page_rects:
            continue
        sideways = (
            page_rotations is not None
            and page_no < len(page_rotations)
            and page_rotations[page_no] % 180 == 90
        )
        axis = (0, 2) if sideways else (1, 3)
        lows = [min(b[axis[0]], b[axis[1]]) for _t, b, _f in page_rects]
        highs = [max(b[axis[0]], b[axis[1]]) for _t, b, _f in page_rects]
        page_low, page_high = min(lows), max(highs)
        if page_heights is not None and page_no < len(page_heights) and page_heights[page_no] > 0:
            page_low, page_high = 0.0, page_heights[page_no]
        height = page_high - page_low
        for (text, _bbox, _fi), low, high in zip(page_rects, lows, highs, strict=True):
            idx = flat_idx
            flat_idx += 1
            if height <= 0 or not text.strip():
                continue
            rel = ((low + high) / 2.0 - page_low) / height
            if rel < _FURNITURE_ZONE_FRACTION:
                out.append((idx, page_no, "low", rel, text))
            elif rel > 1.0 - _FURNITURE_ZONE_FRACTION:
                out.append((idx, page_no, "high", rel, text))
    return out


def _digit_key(text: str) -> tuple[str, tuple[int, ...]]:
    """Split a block's text into a digit-free key and its ASCII digit runs.

    ``"Page 2 of 141"`` gives ``("page # of #", (2, 141))``. The key is a
    comparison key only; the text itself is never rewritten.
    """
    parts: list[str] = []
    runs: list[int] = []
    for is_digit, group in itertools.groupby(
        " ".join(text.lower().split()), key=lambda c: c.isascii() and c.isdigit()
    ):
        chunk = "".join(group)
        if is_digit:
            parts.append("#")
            runs.append(int(chunk))
        else:
            parts.append(chunk)
    return "".join(parts), tuple(runs)


def _running_page_stamps(
    all_page_rects: list[
        list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]]
    ],
    page_numbers: list[int] | None = None,
    page_heights: list[float] | None = None,
    page_rotations: list[int] | None = None,
) -> set[int]:
    """Return flat indices of running stamps and page numbers.

    Blocks in the top or bottom zone are grouped by their text with digit
    runs masked (:func:`_digit_key`) and aligned at one relative height as
    in :func:`_repeated_page_furniture`. A group whose texts differ is a
    running stamp when one digit run moves in step with the page number
    (same offset from the page index) on at least
    ``_FURNITURE_MIN_OCCURRENCE`` of the pages (and at least two): the
    CM/ECF ``"Page 2 of 141"`` band, ``"2 of 76"``, ``"-2-"`` or a bare
    ``"2"``. All of those occurrences are excluded. Numbered table rows or
    captions that repeat at the page edge do not advance by exactly one per
    page and are left alone. A group found on only two pages must also sit
    on adjacent pages and read like a counter (:func:`_counter_like`).
    """
    n_pages = len(all_page_rects)
    min_pages = max(2, int(n_pages * _FURNITURE_MIN_OCCURRENCE))
    pages = (
        page_numbers
        if page_numbers is not None and len(page_numbers) == n_pages
        else list(range(n_pages))
    )
    # (zone, key) -> [(page, flat_idx, relative y, normalized text, digit runs)]
    occurrences: dict[tuple[str, str], list[tuple[int, int, float, str, tuple[int, ...]]]] = {}
    for idx, page_no, zone, rel, text in _edge_zone_blocks(
        all_page_rects, page_heights, page_rotations
    ):
        key, runs = _digit_key(text)
        if not runs:
            continue
        normalized = " ".join(text.lower().split())
        occurrences.setdefault((zone, key), []).append((pages[page_no], idx, rel, normalized, runs))

    exclude: set[int] = set()
    for occs in occurrences.values():
        if len({o[0] for o in occs}) < min_pages:
            continue
        # Several stamps can share a key ("2 of 76" and "2 of 74"), so take
        # the best page-tracking, aligned subset and repeat on the rest.
        remaining = list(occs)
        while True:
            best = _best_page_tracking_subset(remaining)
            n_best = len({o[0] for o in best})
            if n_best < min_pages:
                break
            if n_best < _STAMP_MIN_PAGES_ANY_TEXT:
                best_pages = sorted({o[0] for o in best})
                adjacent = best_pages[-1] - best_pages[0] == len(best_pages) - 1
                if not adjacent or not all(_counter_like(o[3]) for o in best):
                    break
            exclude.update(o[1] for o in best)
            taken = {o[1] for o in best}
            remaining = [o for o in remaining if o[1] not in taken]
    return exclude


# Two pages are thin evidence: there a page-tracking group must sit on
# adjacent pages and read like a page counter ("2", "- 2 -", "2 of 76",
# "Page 2", "Pg 2 of 37 Pg ID 2050", an e-filing stamp), so footnotes 2 and 4
# at the foot of pages 2 and 4 stay; from three pages on any text qualifies.
_STAMP_MIN_PAGES_ANY_TEXT = 3
_COUNTER_MAX_TOKENS = 3
_COUNTER_PAGE_WORDS = frozenset({"page", "pg", "p", "pp", "pageid"})


def _counter_like(text: str) -> bool:
    """Whether a block reads like a page counter or a filing stamp."""
    tokens = [t.strip(".:#,-") for t in text.lower().split()]
    if len(tokens) <= _COUNTER_MAX_TOKENS:
        return True
    return any(t in _COUNTER_PAGE_WORDS for t in tokens) or _is_ecf_stamp(text)


def _best_page_tracking_subset(
    occs: list[tuple[int, int, float, str, tuple[int, ...]]],
) -> list[tuple[int, int, float, str, tuple[int, ...]]]:
    """Largest set of occurrences at one height whose digit run tracks the page.

    ``occs`` are ``(page, flat_idx, relative y, text, digit runs)`` sharing
    one digit-masked key. For each digit-run position the occurrences are
    grouped by ``run value - page``; within a group the ones within
    ``_FURNITURE_Y_TOLERANCE`` of its median height count. Returns the
    group covering the most pages (empty when ``occs`` is empty).
    """
    best: list[tuple[int, int, float, str, tuple[int, ...]]] = []
    if not occs:
        return best
    for j in range(len(occs[0][4])):
        by_offset: dict[int, list[tuple[int, int, float, str, tuple[int, ...]]]] = {}
        for o in occs:
            by_offset.setdefault(o[4][j] - o[0], []).append(o)
        for group in by_offset.values():
            ys = sorted(o[2] for o in group)
            anchor = ys[len(ys) // 2]
            aligned = [o for o in group if abs(o[2] - anchor) <= _FURNITURE_Y_TOLERANCE]
            if len({o[0] for o in aligned}) > len({o[0] for o in best}):
                best = aligned
    return best


# CM/ECF and appellate e-filing stamps: "Case 1:13-cv-00133 Document 244
# Filed 01/08/14 Page 2 of 141", "Case 20-10250-btb Doc 13 Entered 02/05/20
# Page 2 of 90", "Case: 18-17274, 12/07/2018, ID: 11114605, DktEntry: 10-1,
# Page 2 of 65". Token-based like :func:`_is_watermark`, and only applied to
# whole blocks in a page's top or bottom zone.
_ECF_FILING_TOKENS = frozenset({"document", "doc", "dktentry", "filed", "entered"})
_ECF_MAX_CHARS = 300


def _is_ecf_stamp(text: str) -> bool:
    """Whether a block is a court e-filing header stamp."""
    if len(text) > _ECF_MAX_CHARS:
        return False
    tokens = [t.strip(":,;.()#") for t in text.lower().split()]
    has_case = has_page = False
    for i, tok in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok == "case" and any(c.isdigit() for c in nxt):
            has_case = True
        elif tok == "page" and nxt[:1].isdigit():
            has_page = True
    if not has_case:
        return False
    if has_page:
        return any(t in _ECF_FILING_TOKENS for t in tokens)
    # Bankruptcy stamps may carry no page counter: "Case 05-41406 Doc 8-1
    # Filed 10/30/05 Entered 10/30/05 10:27:08 Desc Schedule ...".
    return {"doc", "filed", "entered"} <= set(tokens)


# Vertical text in a side margin (a rotated court-name sidebar, a
# "Downloaded from" strip): taller than wide by this factor and at least this
# tall, repeated with the same text within this many points of one position.
_VERTICAL_MIN_ASPECT = 3.0
_VERTICAL_MIN_HEIGHT = 40.0
_VERTICAL_POSITION_TOLERANCE = 3.0


def _vertical_margin_furniture(
    all_page_rects: list[
        list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]]
    ],
) -> set[int]:
    """Return flat indices of vertical text repeated at one place on many pages.

    All occurrences are excluded: running vertical text is never part of the
    body flow, and keeping one would drop it mid-sentence on its first page.
    """
    n_pages = len(all_page_rects)
    min_pages = max(2, int(n_pages * _FURNITURE_MIN_OCCURRENCE))
    # normalized text -> [(page, flat_idx, left, bottom)]
    occurrences: dict[str, list[tuple[int, int, float, float]]] = {}
    flat_idx = 0
    for page_no, page_rects in enumerate(all_page_rects):
        for text, (left, bottom, right, top), _fi in page_rects:
            idx = flat_idx
            flat_idx += 1
            width = abs(right - left)
            height = abs(top - bottom)
            normalized = " ".join(text.lower().split())
            if (
                height < _VERTICAL_MIN_HEIGHT
                or height < _VERTICAL_MIN_ASPECT * width
                or sum(c.isalpha() for c in normalized) < _FURNITURE_MIN_LETTERS
            ):
                continue
            occurrences.setdefault(normalized, []).append(
                (page_no, idx, min(left, right), min(bottom, top))
            )

    exclude: set[int] = set()
    for occs in occurrences.values():
        if len({o[0] for o in occs}) < min_pages:
            continue
        xs = sorted(o[2] for o in occs)
        ys = sorted(o[3] for o in occs)
        ax, ay = xs[len(xs) // 2], ys[len(ys) // 2]
        aligned = [
            o
            for o in occs
            if abs(o[2] - ax) <= _VERTICAL_POSITION_TOLERANCE
            and abs(o[3] - ay) <= _VERTICAL_POSITION_TOLERANCE
        ]
        if len({o[0] for o in aligned}) >= min_pages:
            exclude.update(o[1] for o in aligned)
    return exclude


def _classify_headings_scored(
    all_page_rects: list[
        list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]]
    ],
    *,
    heading_font_ratio: float = 1.2,
    max_heading_pct: float = 0.35,
) -> set[int]:
    """Classify headings using multi-signal adaptive scoring.

    Each text block receives a continuous heading score from independent signals:
    - Font size ratio vs body text (larger = more heading-like)
    - Bold weight (bold text is more heading-like)
    - ALL CAPS text (common legal/government heading convention)
    - Text length (shorter = more heading-like)
    - Indentation (headings tend to have different left margin than body)

    The score distribution is then adaptively thresholded using Otsu on the
    scores themselves — no hard-coded cutoff. If the score distribution is
    unimodal (no clear heading/body separation), returns empty.

    Returns set of flat indices into the concatenated rect list.
    """
    from kaos_content.layout import otsu_threshold

    all_flat_rects = [r for page_rects in all_page_rects for r in page_rects]
    if not all_flat_rects:
        return set()

    # --- Compute document-wide statistics for adaptive weighting ---
    body_size, size_source = _estimate_body_font_size(all_flat_rects)

    # Font diversity analysis
    sizes_seen: set[float] = set()
    total_text = 0
    bold_count = 0
    for text, _bbox, fi in all_flat_rects:
        if not text.strip():
            continue
        total_text += 1
        if fi:
            s = fi.get(size_source, 0.0)
            if s > 0:
                sizes_seen.add(round(s, 1))
            if fi.get("is_bold"):
                bold_count += 1

    if total_text == 0:
        return set()

    bold_fraction = bold_count / total_text
    has_size_diversity = len(sizes_seen) > 2

    # Guard: if all font sizes are unrealistically small (e.g., 1.0pt from OCR/embedded text),
    # font metadata is unreliable — be very conservative
    font_metadata_reliable = True
    if len(sizes_seen) <= 1:
        # Only one font size — no size-based heading signal possible
        font_metadata_reliable = body_size > 3.0  # Sizes <= 3pt are likely bad metadata

    # Guard: if most blocks have very short text (fragments from tables/figures),
    # heading detection should be very conservative
    short_fragments = sum(
        1 for text, _, _ in all_flat_rects if text.strip() and len(text.strip()) < 10
    )
    high_fragment_ratio = short_fragments / total_text > 0.5 if total_text > 0 else False

    # Adaptive signal weights.
    size_weight = 3.0 if has_size_diversity else 0.0
    # Bold: weight inversely proportional to frequency
    bold_weight = max(0.0, 3.0 * (1.0 - bold_fraction)) if bold_fraction < 0.6 else 0.0
    # CAPS: moderate weight
    caps_weight = 1.5

    # If font metadata is unreliable or document has too many fragments,
    # reduce weights to avoid over-detection
    if not font_metadata_reliable:
        bold_weight = min(bold_weight, 1.0)
        caps_weight = 0.5
    if high_fragment_ratio:
        bold_weight *= 0.5
        caps_weight *= 0.5

    # If the document has high bold AND high short-text ratios simultaneously,
    # heading detection is unreliable — these are likely scanned documents,
    # tables, or figures where most text fragments look "heading-like"
    short_text_count = sum(
        1 for text, _, _ in all_flat_rects if text.strip() and len(text.strip().split()) <= 6
    )
    short_text_ratio = short_text_count / total_text if total_text > 0 else 0
    if bold_fraction > 0.4 and short_text_ratio > 0.4:
        bold_weight = 0.0
        caps_weight = 0.5
    elif bold_fraction > 0.5:
        # High bold ratio: rely more on size diversity, less on bold
        bold_weight = min(bold_weight, 0.5)

    # --- Score each block ---
    scores: list[float] = []
    valid_indices: list[int] = []

    for idx, (text, (_left, _bottom, _right, _top), font_info) in enumerate(all_flat_rects):
        text_stripped = text.strip()
        if not text_stripped:
            scores.append(0.0)
            continue
        if _is_watermark(text_stripped):
            scores.append(0.0)
            continue
        if len(text_stripped) > 200 or len(text_stripped.split()) > 20:
            scores.append(0.0)
            continue
        # Minimum viable heading: at least 3 alpha characters
        alpha_count = sum(1 for c in text_stripped if c.isalpha())
        if alpha_count < 3:
            scores.append(0.0)
            continue
        # Reject very short fragments that are likely table cells/labels
        if len(text_stripped) < 3:
            scores.append(0.0)
            continue
        # Text quality filter: headings should be mostly alphanumeric + spaces
        # OCR artifacts like "Â\nfy", ",^--i==â crk" have high noise ratios
        alnum_count = sum(1 for c in text_stripped if c.isalnum() or c.isspace())
        if len(text_stripped) > 0 and alnum_count / len(text_stripped) < 0.6:
            scores.append(0.0)
            continue

        score = 0.0
        fi = font_info or {}

        # Signal 1: Font size ratio (weight adapts to document)
        if size_weight > 0:
            size_val = fi.get(size_source, 0.0)
            if body_size > 0 and size_val > 0:
                ratio = size_val / body_size
                if ratio > 1.0:
                    score += min((ratio - 1.0) * size_weight, size_weight)

        # Signal 2: Bold (weight adapts to rarity)
        if bold_weight > 0 and fi.get("is_bold"):
            score += bold_weight

        # Signal 3: ALL CAPS (weight adapts to rarity)
        if caps_weight > 0:
            alpha_chars = [c for c in text_stripped if c.isalpha()]
            if len(alpha_chars) >= 3:
                upper_ratio = sum(1 for c in alpha_chars if c.isupper()) / len(alpha_chars)
                if upper_ratio > 0.8:
                    score += caps_weight

        # Signal 4: Short text bonus (always 0-1)
        word_count = len(text_stripped.split())
        if word_count <= 6:
            score += 1.0
        elif word_count <= 10:
            score += 0.5

        scores.append(score)
        if score > 0:
            valid_indices.append(idx)

    if not valid_indices:
        return set()

    # Adaptive threshold: use Otsu on the positive scores to find the
    # natural split between "body-like" and "heading-like" scores
    positive_scores = [scores[i] for i in valid_indices]
    if len({round(s, 1) for s in positive_scores}) < 2:
        # All scores are the same — no differentiation possible
        return set()

    threshold_result = otsu_threshold(positive_scores)

    # Guard: the threshold must create meaningful separation
    if threshold_result.above_count == 0:
        return set()
    if threshold_result.below_mean <= 0 and threshold_result.above_mean <= 0:
        return set()
    # If above class is too large, the scoring didn't differentiate
    if threshold_result.above_count / len(positive_scores) > max_heading_pct:
        return set()
    # Require meaningful gap between classes
    if threshold_result.above_mean > 0 and threshold_result.below_mean > 0:
        class_ratio = threshold_result.above_mean / threshold_result.below_mean
        if class_ratio < 1.5:
            return set()

    # Select blocks above threshold, with minimum absolute score
    # Headings should score at least 2.0 (one strong signal or two moderate ones)
    # This prevents noise blocks that just barely pass Otsu from being headings
    min_absolute_score = 2.0
    effective_threshold = max(threshold_result.threshold, min_absolute_score)

    heading_indices: set[int] = set()
    for idx in valid_indices:
        if scores[idx] >= effective_threshold:
            heading_indices.add(idx)

    # Final proportion guard
    if len(heading_indices) / total_text > max_heading_pct:
        return set()

    # Per-page heading density guard: if any page has too many headings
    # relative to its block count, the classifier is likely confused by
    # tables, figures, or OCR noise on that page. Remove those headings.
    page_start = 0
    for page_rects in all_page_rects:
        page_end = page_start + len(page_rects)
        page_blocks = sum(
            1
            for i in range(page_start, page_end)
            if i < len(all_flat_rects) and all_flat_rects[i][0].strip()
        )
        page_headings = sum(1 for i in range(page_start, page_end) if i in heading_indices)
        # If >40% of a page's blocks are headings AND there are >15 headings,
        # this page is likely a table/figure — remove its headings
        if page_headings > 15 and page_blocks > 0 and page_headings / page_blocks > 0.4:
            for i in range(page_start, page_end):
                heading_indices.discard(i)
        page_start = page_end

    return heading_indices


def _median(vals: list[float]) -> float:
    """Compute median of a list of floats."""
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


# Known production-slug patterns in GPO / Federal Register PDFs, e.g.
# "VerDate Mar<15>2010 19:36 Feb 07, 2011 Jkt 223001 PO 00000 Frm 00001 Fmt 4717
# Sfmt 4717 E:\\FR\\FM\\08FEN3.SGM" and "emcdonald on DSK2BSOYB1PROD with
# NOTICES3". Matching is token-based: a bare substring such as "dsk" also
# matches ordinary words ("groundskeeping"), which dropped real table rows.
_WATERMARK_SUBSTRINGS = ("verdate", "e:\\fr\\fm\\")
# Slug keyword followed by a number token: "Jkt 223001", "Frm 00001", ...
_WATERMARK_NUMBERED_KEYS = frozenset({"jkt", "frm", "fmt", "sfmt"})


def _is_watermark(text: str) -> bool:
    """Check if text is a production watermark or page furniture."""
    lower = text.strip().lower()
    if any(pat in lower for pat in _WATERMARK_SUBSTRINGS):
        return True
    tokens = lower.split()
    for i, tok in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok in _WATERMARK_NUMBERED_KEYS and nxt[:1].isdigit():
            return True
        if tok == "billing" and nxt == "code":
            after = tokens[i + 2] if i + 2 < len(tokens) else ""
            if after[:1].isdigit():
                return True
        # GPO operator slug: "DSK" + alphanumeric id, usually glued to "PROD".
        if tok.startswith("dsk") and len(tok) >= 6 and any(c.isdigit() for c in tok):
            return True
        if tok.endswith("prod") and nxt == "with" and len(tokens) <= 8:
            return True
    return False


def _build_heading_depth_map(
    heading_indices: set[int],
    all_flat_rects: list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]],
) -> dict[int, int]:
    """Build adaptive heading depth mapping from the font hierarchy.

    Examines the distinct (font_name, font_size, is_bold) combinations
    among heading blocks, sorts them by visual prominence (size desc,
    bold first), and assigns depths 1..N to the N distinct font styles.

    Returns: flat_index -> depth (1-6).
    """

    if not heading_indices:
        return {}

    # Collect font signatures for heading blocks
    signatures: list[tuple[int, float, bool, str]] = []  # (idx, size, bold, name)
    for idx in heading_indices:
        _text, _bbox, fi = all_flat_rects[idx]
        fi = fi or {}
        size = fi.get("font_size", 0.0) or fi.get("rect_height", 0.0)
        bold = fi.get("is_bold", False)
        name = fi.get("font_name", "")
        signatures.append((idx, size, bold, name))

    # Cluster by (font_name, rounded_size, bold) to find distinct heading styles
    style_counts: dict[tuple[str, float, bool], list[int]] = {}
    for idx, size, bold, name in signatures:
        # Cluster sizes within 0.5 tolerance
        rounded_size = round(size * 2) / 2  # round to 0.5
        key = (name, rounded_size, bold)
        style_counts.setdefault(key, []).append(idx)

    # Rank styles by prominence: larger size first, then bold first
    ranked_styles = sorted(
        style_counts.keys(),
        key=lambda k: (-k[1], 0 if k[2] else 1, k[0]),
    )

    # Assign depth 1..min(N, 6) to ranked styles
    depth_map: dict[int, int] = {}
    for rank, style_key in enumerate(ranked_styles):
        depth = min(rank + 1, 6)
        for idx in style_counts[style_key]:
            depth_map[idx] = depth

    return depth_map


def _merge_consecutive_headings(
    heading_indices: set[int],
    depth_map: dict[int, int],
    all_flat: list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]],
    all_page_rects: list[
        list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]]
    ],
    filter_indices: set[int],
) -> tuple[
    set[int],
    dict[int, int],
    list[list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]]],
    set[int],
]:
    """Merge consecutive heading blocks that form multi-line headings.

    When a heading spans multiple lines in a PDF, each line becomes a separate
    text rectangle. This pass merges adjacent heading blocks that:
    1. Have the same heading depth
    2. Share the same font properties (name, size, bold)
    3. Are on consecutive lines with no intervening body text or filtered blocks
    4. Have a vertical gap consistent with line spacing (not paragraph spacing)

    Returns updated (heading_indices, depth_map, all_page_rects) where merged
    headings have their text concatenated and surplus blocks are removed.
    """
    if not heading_indices or not all_flat:
        return heading_indices, depth_map, all_page_rects, filter_indices

    # Build sorted list of heading indices
    sorted_headings = sorted(heading_indices)

    # Identify merge groups: runs of consecutive flat indices that are all
    # headings with the same depth and font properties
    merge_groups: list[list[int]] = []
    current_group: list[int] = [sorted_headings[0]]

    for i in range(1, len(sorted_headings)):
        prev_idx = sorted_headings[i - 1]
        curr_idx = sorted_headings[i]

        # Check: consecutive in flat index (no body text between them)
        # Allow skipping filtered blocks between headings
        all_between_filtered = True
        for between_idx in range(prev_idx + 1, curr_idx):
            if between_idx not in filter_indices:
                all_between_filtered = False
                break

        if not all_between_filtered:
            # There's body text between them — start new group
            if len(current_group) > 1:
                merge_groups.append(current_group)
            current_group = [curr_idx]
            continue

        # Check same depth
        if depth_map.get(prev_idx) != depth_map.get(curr_idx):
            if len(current_group) > 1:
                merge_groups.append(current_group)
            current_group = [curr_idx]
            continue

        # Check same font properties
        prev_fi = all_flat[prev_idx][2] or {}
        curr_fi = all_flat[curr_idx][2] or {}
        same_font = (
            prev_fi.get("font_name", "") == curr_fi.get("font_name", "")
            and abs(prev_fi.get("font_size", 0) - curr_fi.get("font_size", 0)) < 0.5
            and prev_fi.get("is_bold", False) == curr_fi.get("is_bold", False)
        )

        if not same_font:
            if len(current_group) > 1:
                merge_groups.append(current_group)
            current_group = [curr_idx]
            continue

        # Check vertical proximity: gap should be consistent with line spacing
        prev_bbox = all_flat[prev_idx][1]
        curr_bbox = all_flat[curr_idx][1]
        prev_top = max(prev_bbox[1], prev_bbox[3])
        prev_bottom = min(prev_bbox[1], prev_bbox[3])
        curr_top = max(curr_bbox[1], curr_bbox[3])
        curr_bottom = min(curr_bbox[1], curr_bbox[3])
        line_height = max(abs(prev_top - prev_bottom), abs(curr_top - curr_bottom), 1.0)
        gap = abs(prev_bottom - curr_top)
        # Gap should be less than 2x line height for continuation
        if gap > line_height * 2.0:
            if len(current_group) > 1:
                merge_groups.append(current_group)
            current_group = [curr_idx]
            continue

        current_group.append(curr_idx)

    if len(current_group) > 1:
        merge_groups.append(current_group)

    if not merge_groups:
        return heading_indices, depth_map, all_page_rects, filter_indices

    # Apply merges: rebuild all_page_rects with merged headings
    # First, build a flat-index-to-(page, rect_index) mapping
    flat_to_page: dict[int, tuple[int, int]] = {}
    flat_idx = 0
    for pi, page_rects in enumerate(all_page_rects):
        for ri in range(len(page_rects)):
            flat_to_page[flat_idx] = (pi, ri)
            flat_idx += 1

    # Track which flat indices should be removed (merged into the first of their group)
    merged_text: dict[int, str] = {}  # first_idx -> merged text
    remove_indices: set[int] = set()

    for group in merge_groups:
        first_idx = group[0]
        texts = []
        merged_bbox = list(all_flat[first_idx][1])
        for idx in group:
            text = all_flat[idx][0].strip()
            texts.append(text)
            bbox = all_flat[idx][1]
            merged_bbox[0] = min(merged_bbox[0], bbox[0])
            merged_bbox[1] = min(merged_bbox[1], bbox[1])
            merged_bbox[2] = max(merged_bbox[2], bbox[2])
            merged_bbox[3] = max(merged_bbox[3], bbox[3])
        merged_text[first_idx] = " ".join(texts)
        for idx in group[1:]:
            remove_indices.add(idx)

    # Rebuild all_page_rects with merges applied
    new_heading_indices: set[int] = set()
    new_depth_map: dict[int, int] = {}
    new_filter_indices: set[int] = set()
    new_all_page_rects: list[
        list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]]
    ] = []

    new_flat_idx = 0
    old_flat_idx = 0
    for _pi, page_rects in enumerate(all_page_rects):
        new_page_rects: list[
            tuple[str, tuple[float, float, float, float], dict[str, Any] | None]
        ] = []
        for _ri, (text, bbox, fi) in enumerate(page_rects):
            if old_flat_idx in remove_indices:
                old_flat_idx += 1
                continue
            if old_flat_idx in merged_text:
                # Replace text and expand bbox
                new_text = merged_text[old_flat_idx]
                # Find merged bbox from all_flat
                group = next(g for g in merge_groups if g[0] == old_flat_idx)
                mbbox = list(bbox)
                for gidx in group:
                    gb = all_flat[gidx][1]
                    mbbox[0] = min(mbbox[0], gb[0])
                    mbbox[1] = min(mbbox[1], gb[1])
                    mbbox[2] = max(mbbox[2], gb[2])
                    mbbox[3] = max(mbbox[3], gb[3])
                new_page_rects.append((new_text, (mbbox[0], mbbox[1], mbbox[2], mbbox[3]), fi))
            else:
                new_page_rects.append((text, bbox, fi))

            if old_flat_idx in heading_indices:
                new_heading_indices.add(new_flat_idx)
                new_depth_map[new_flat_idx] = depth_map.get(old_flat_idx, 2)

            if old_flat_idx in filter_indices:
                new_filter_indices.add(new_flat_idx)

            new_flat_idx += 1
            old_flat_idx += 1

        new_all_page_rects.append(new_page_rects)

    return new_heading_indices, new_depth_map, new_all_page_rects, new_filter_indices


# ---------------------------------------------------------------------------
# Internal: page rendering and classification
# ---------------------------------------------------------------------------


def _render_page_to_image(
    page: pdfium.PdfPage,
    page_idx: int,
    *,
    source_uri: str,
    dpi: int = 300,
    grayscale: bool = False,
) -> KaosImage:
    """Render a PDF page to a KaosImage."""
    scale = dpi / 72.0
    bitmap = page.render(scale=scale, rotation=0, grayscale=grayscale)
    try:
        pil_image = bitmap.to_pil()
    finally:
        bitmap.close()

    prov = Provenance(
        source=SourceRef(uri=source_uri, mime_type="application/pdf"),
        page=page_idx + 1,
        extractor="kaos-pdf/pypdfium2",
    )

    return KaosImage.from_pil(
        pil_image,
        dpi=(dpi, dpi),
        source_format="png",
        provenance=prov,
    )


def _classify_page(page: pdfium.PdfPage) -> str:
    """Classify page type based on object counts."""
    text_count = 0
    image_count = 0

    for obj in page.get_objects():
        t = obj.type
        if t == FPDF_PAGEOBJ_TEXT:
            text_count += 1
        elif t == FPDF_PAGEOBJ_IMAGE:
            image_count += 1

    if text_count == 0 and image_count == 0:
        return "blank"
    if text_count > 0 and image_count == 0:
        return "text"
    if image_count > 0 and text_count == 0:
        return "image"
    return "mixed"


def _make_provenance(
    page_idx: int,
    bbox: tuple[float, float, float, float],
    source_uri: str,
) -> Provenance:
    """Create provenance for a text block."""
    left, bottom, right, top = bbox
    return Provenance(
        source=SourceRef(uri=source_uri, mime_type="application/pdf"),
        page=page_idx + 1,
        bbox=BoundingBox(left=left, top=top, right=right, bottom=bottom),
        extractor="kaos-pdf/pypdfium2",
    )


# ---------------------------------------------------------------------------
# OCR integration (FUND-3)
# ---------------------------------------------------------------------------


def _run_ocr_per_page(
    doc: pdfium.PdfDocument,
    *,
    page_indices: list[int],
    all_page_rects: list[
        list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]]
    ],
    ocr: OCRMode,
    ocr_engine: OCREngine | None,
    ocr_dpi: int,
    ocr_min_confidence: float,
    ocr_quality_threshold: float,
    source_uri: str,
) -> list[OCRResult | None]:
    """Decide per-page whether OCR should run, run it, return results.

    Policy:

    - ``ocr="always"``  → OCR every page in ``page_indices``.
    - ``ocr="auto"``    → OCR pages whose native rects are empty/blank,
      OR structurally-scanned pages (raster image content) whose native
      text layer scores below ``ocr_quality_threshold`` (a garbled
      embedded OCR layer). Born-digital text pages pass through
      untouched.
    - ``ocr="never"``   → should never reach this function.

    Engine construction is lazy: if the caller passed ``ocr_engine=None``
    and at least one page needs OCR, we build the default engine on
    first use. This means the Tesseract dep is only materialized when
    OCR actually runs.
    """
    results: list[OCRResult | None] = [None] * len(page_indices)
    engine = ocr_engine
    n_pages = len(doc)

    # Unused but retained for clarity if we ever want confidence-gated
    # OCR triggers. Currently confidence filtering happens at emit time.
    _ = ocr_min_confidence

    for i, page_idx in enumerate(page_indices):
        if page_idx < 0 or page_idx >= n_pages:
            continue

        page_rects = all_page_rects[i]
        page: pdfium.PdfPage | None = None
        run = _should_run_ocr(ocr, page_rects)
        if not run and ocr == "auto":
            # Native text is present. Re-OCR only when the page is
            # structurally a scan (carries raster image content) AND its
            # native layer looks garbled — recovering present-but-mangled
            # embedded OCR without ever touching born-digital text pages.
            page = doc[page_idx]
            run = _is_garbled_scan_layer(page, page_rects, ocr_quality_threshold)

        if not run:
            continue

        if engine is None:
            from kaos_pdf.ocr import get_default_engine

            engine = get_default_engine()

        if page is None:
            page = doc[page_idx]
        image = _render_page_to_image(
            page,
            page_idx,
            source_uri=source_uri,
            dpi=ocr_dpi,
            grayscale=False,
        )
        try:
            results[i] = engine.extract_sync(image)
        except Exception as exc:
            logger.warning(
                "OCR engine %s failed on page %d: %s",
                type(engine).__name__,
                page_idx + 1,
                exc,
            )
            results[i] = None
    return results


def _should_run_ocr(
    ocr: OCRMode,
    page_rects: list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]],
) -> bool:
    """Return True if OCR should run for a page given the empty-layer policy.

    ``"auto"`` runs OCR iff there is no extractable native text. The
    garbled-but-present-layer case is handled separately by
    :func:`_is_garbled_scan_layer` (it needs the page object to confirm
    the page is structurally a scan before trusting a text-quality
    signal).
    """
    if ocr == "never":
        return False
    if ocr == "always":
        return True
    # ocr == "auto"
    if not page_rects:
        return True
    return not any((text or "").strip() for text, _bbox, _font in page_rects)


def _is_garbled_scan_layer(
    page: pdfium.PdfPage,
    page_rects: list[tuple[str, tuple[float, float, float, float], dict[str, Any] | None]],
    threshold: float,
) -> bool:
    """Return True when ``page`` is a scan with a garbled native text layer.

    Two gates, both required:

    1. **Structural** — :func:`_classify_page` reports ``"image"`` or
       ``"mixed"`` (the page carries raster image content). Born-digital
       ``"text"`` pages are excluded so clean digital text is never
       re-OCR'd, even if a line happens to score low.
    2. **Legibility** — the joined native text scores below ``threshold``
       per :func:`kaos_pdf.quality.is_low_quality_layer` (worst
       substantial line below threshold).

    A non-positive ``threshold`` disables the check entirely, restoring
    the legacy empty-layer-only ``"auto"`` behavior.
    """
    if threshold <= 0.0:
        return False
    if _classify_page(page) not in ("image", "mixed"):
        return False
    page_text = "\n".join(text for text, _bbox, _font in page_rects)
    return is_low_quality_layer(page_text, threshold=threshold)


def _emit_ocr_blocks(
    builder: DocumentBuilder,
    *,
    page_idx: int,
    ocr_result: OCRResult,
    min_confidence: float,
) -> None:
    """Emit one Paragraph per surviving OCR line with full provenance.

    Each paragraph carries page + bbox + confidence on its Provenance.
    Lines below ``min_confidence`` are dropped entirely — the caller
    opted into that filter knowing it removes real signal at high
    thresholds. Lines with empty text (pure whitespace) are also
    dropped as they contribute nothing to downstream NLP.
    """
    extractor = f"kaos-pdf/ocr/{ocr_result.engine_name}"
    for line in ocr_result.lines:
        if line.confidence < min_confidence:
            continue
        text = line.text.strip()
        if not text:
            continue
        builder.paragraph(text)
        builder.with_provenance(
            page=page_idx + 1,
            bbox=line.bbox,
            confidence=line.confidence,
            extractor=extractor,
        )


# ---------------------------------------------------------------------------
# Table extraction integration (FUND-4)
# ---------------------------------------------------------------------------


def _run_table_engine(
    pdf_path: str,
    engine: TableEngine | None,
    *,
    page_indices: list[int] | None,
) -> list[ExtractedTable]:
    """Run the configured table engine or the default (pdfplumber).

    Returns an empty list on engine failure — table detection must
    never crash the overall extraction.
    """
    if engine is None:
        from kaos_pdf.tables import get_default_engine

        engine = get_default_engine()
    try:
        result = engine.extract_sync(pdf_path, page_indices=page_indices)
    except Exception as exc:
        logger.warning(
            "Table engine %s failed on %s: %s",
            type(engine).__name__,
            pdf_path,
            exc,
        )
        return []
    return list(result.tables)


def _emit_engine_table(builder: DocumentBuilder, extracted: ExtractedTable) -> None:
    """Emit an engine-extracted table as a kaos-content Table block."""
    rows = [[(cell if cell is not None else "") for cell in row] for row in extracted.rows]
    if not rows:
        return

    column_count = max((len(r) for r in rows), default=0)
    if column_count == 0:
        return
    # Pad short rows so the builder doesn't raise on uneven widths.
    rows = [r + [""] * (column_count - len(r)) for r in rows]

    if extracted.has_header and len(rows) >= 2:
        headers = rows[0]
        body_rows = rows[1:]
    else:
        # Synthesize opaque column labels so the Table block remains
        # valid (the builder requires a header row).
        headers = [f"col_{i + 1}" for i in range(column_count)]
        body_rows = rows

    builder.table(headers=headers, rows=body_rows)
    builder.with_provenance(
        page=extracted.page,
        bbox=extracted.bbox,
        confidence=extracted.confidence,
        extractor=f"kaos-pdf/tables/{extracted.engine_name}",
    )


def _build_tabular_document(
    pdf_path: str,
    tables: list[ExtractedTable],
) -> TabularDocument:
    """Pivot engine-extracted tables into a typed :class:`TabularDocument`.

    Each :class:`ExtractedTable` becomes one :class:`Table` with typed
    :class:`Column` metadata inferred via
    :func:`~kaos_content.model.tabular.infer_column_type`. Parsed
    values populate the row tuples — consumers get real ``int`` /
    ``float`` / ``datetime.date`` natives, not stringly-typed cells.
    """
    from kaos_content.model.attr import SourceRef as ContentSourceRef
    from kaos_content.model.metadata import DocumentMetadata
    from kaos_content.model.tabular import (
        Column,
        ColumnType,
        Table,
        TabularDocument,
        infer_column_type,
    )

    source_ref = ContentSourceRef(
        uri=Path(pdf_path).resolve().as_uri(),
        mime_type="application/pdf",
    )

    tds: list[Table] = []
    for idx, extracted in enumerate(tables, start=1):
        rows = [[(cell if cell is not None else "") for cell in row] for row in extracted.rows]
        if not rows:
            continue
        column_count = max(len(r) for r in rows)
        rows = [r + [""] * (column_count - len(r)) for r in rows]

        if extracted.has_header and len(rows) >= 2:
            header = rows[0]
            data_rows = rows[1:]
        else:
            header = [f"col_{i + 1}" for i in range(column_count)]
            data_rows = rows

        # Parse each column's cells into Python natives for type
        # inference. Empty strings are treated as None so they don't
        # widen numeric columns back to TEXT.
        parsed_cols: list[list[Any]] = [[] for _ in range(column_count)]
        for row in data_rows:
            for c, cell in enumerate(row):
                parsed_cols[c].append(_parse_scalar(cell))

        columns: list[Column] = []
        for c in range(column_count):
            col_type = (
                infer_column_type(tuple(parsed_cols[c])) if parsed_cols[c] else ColumnType.TEXT
            )
            name = header[c].strip() if c < len(header) and header[c].strip() else f"col_{c + 1}"
            columns.append(Column(name=name, column_type=col_type))

        # Build row tuples with coerced values.
        typed_rows: tuple[tuple[Any, ...], ...] = tuple(
            tuple(parsed_cols[c][r] for c in range(column_count)) for r in range(len(data_rows))
        )
        tds.append(
            Table(
                name=f"page{extracted.page}_table{idx}",
                columns=tuple(columns),
                rows=typed_rows,
                row_count=len(typed_rows),
                metadata={
                    "page": extracted.page,
                    "engine": extracted.engine_name,
                    "has_header": extracted.has_header,
                },
            )
        )

    return TabularDocument(
        metadata=DocumentMetadata(source=source_ref),
        tables=tuple(tds),
    )


def _parse_scalar(cell: str) -> Any:
    """Parse a cell string into the narrowest sensible Python native.

    Order:
        int → float → bool → date (YYYY-MM-DD) → datetime (ISO) → str.

    Empty strings map to ``None`` so they don't force numeric columns
    back to TEXT. Decimal / money / percent parsing is deferred to a
    future pass — FUND-4.3 will plug in the alpha extractors.
    """
    if cell is None:
        return None
    stripped = cell.strip()
    if not stripped:
        return None

    # int (allow thousands separators)
    int_candidate = stripped.replace(",", "").replace("_", "")
    if _looks_int(int_candidate):
        try:
            return int(int_candidate)
        except ValueError:
            pass

    # float (allow thousands separators)
    if _looks_float(int_candidate):
        try:
            return float(int_candidate)
        except ValueError:
            pass

    # bool
    if stripped.lower() in ("true", "yes"):
        return True
    if stripped.lower() in ("false", "no"):
        return False

    # date / datetime
    import datetime as _dt

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return _dt.datetime.strptime(stripped, fmt).date()
        except ValueError:
            continue
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return _dt.datetime.strptime(stripped, fmt)
        except ValueError:
            continue

    return stripped


def _looks_int(s: str) -> bool:
    if not s:
        return False
    body = s[1:] if s[0] in "+-" else s
    return body.isdigit()


def _looks_float(s: str) -> bool:
    if not s:
        return False
    # Scientific notation: let Python's float parser be the oracle.
    if "e" in s.lower():
        try:
            float(s)
        except ValueError:
            return False
        return True
    body = s.lstrip("+-")
    if "." in body:
        left, _, right = body.partition(".")
        left_ok = left.isdigit() or not left
        right_ok = right.isdigit() or not right
        has_digit = bool(left) or bool(right)
        return bool(left_ok and right_ok and has_digit)
    return False

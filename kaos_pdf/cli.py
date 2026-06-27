"""Command-line interface for kaos-pdf.

Every command supports --json for structured output (pipe-friendly).
Without --json, output is human-readable.

Usage:
    kaos-pdf extract FILE [--format markdown|text|json|html] [--pages 1-5] [--output FILE]
    kaos-pdf extract --stdin [--format markdown|text|json|html] [--output FILE]
    kaos-pdf search FILE QUERY [--top-k 10] [--json]
    kaos-pdf info FILE [--json]
    kaos-pdf outline FILE [--json]
    kaos-pdf page FILE PAGE_NUMBER [--json]
    kaos-pdf render FILE PAGE [--dpi 300] [--grayscale] [--output image.png] [--json]
    kaos-pdf classify FILE [--page N] [--json]
    kaos-pdf serve [--http] [--port 8000]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def main(argv: list[str] | None = None) -> None:
    """Entry point for kaos-pdf CLI."""
    parser = argparse.ArgumentParser(prog="kaos-pdf", description="PDF extraction and search")
    sub = parser.add_subparsers(dest="command", required=True)

    # extract — special: --format controls content type, not --json
    p_extract = sub.add_parser("extract", help="Extract PDF to markdown, text, JSON, or HTML")
    p_extract.add_argument("file", nargs="?", type=Path, default=None, help="PDF file path")
    p_extract.add_argument(
        "--format",
        "-f",
        choices=["markdown", "text", "json", "html"],
        default="markdown",
        help="Output format (default: markdown)",
    )
    p_extract.add_argument("--pages", "-p", help="Page range, e.g. '1-5' or '1,3,5-7' (1-based)")
    p_extract.add_argument("--output", "-o", type=Path, help="Output file (default: stdout)")
    p_extract.add_argument("--stdin", action="store_true", help="Read PDF bytes from stdin")
    p_extract.add_argument("--no-headings", action="store_true", help="Disable heading detection")
    p_extract.add_argument("--no-tables", action="store_true", help="Disable table extraction")
    p_extract.add_argument(
        "--ocr",
        choices=["never", "auto", "always"],
        default="never",
        help=(
            "OCR policy (default: never). 'auto' OCRs empty and garbled "
            "scanned text layers; 'always' re-OCRs every page. Requires the "
            "[ocr] extra and the system tesseract binary."
        ),
    )
    p_extract.add_argument(
        "--ocr-dpi",
        type=int,
        default=300,
        help="DPI to render pages at before OCR (default: 300). Only used when --ocr runs.",
    )

    # search
    p_search = sub.add_parser("search", help="Search within a PDF")
    p_search.add_argument("file", type=Path, help="PDF file path")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--top-k", "-k", type=int, default=10, help="Max results (default: 10)")
    p_search.add_argument(
        "--level",
        choices=["paragraph", "sentence"],
        default="paragraph",
        help="Search granularity (default: paragraph). Sentence requires kaos-nlp-core.",
    )
    p_search.add_argument("--json", action="store_true", help="Structured JSON output")

    # info
    p_info = sub.add_parser("info", help="Show PDF metadata")
    p_info.add_argument("file", type=Path, help="PDF file path")
    p_info.add_argument("--json", action="store_true", help="Structured JSON output")

    # outline
    p_outline = sub.add_parser("outline", help="Show PDF outline/bookmarks")
    p_outline.add_argument("file", type=Path, help="PDF file path")
    p_outline.add_argument("--json", action="store_true", help="Structured JSON output")

    # page
    p_page = sub.add_parser("page", help="Extract text from a single page")
    p_page.add_argument("file", type=Path, help="PDF file path")
    p_page.add_argument("page_number", type=int, help="Page number (1-based)")
    p_page.add_argument("--json", action="store_true", help="Structured JSON output")

    # render
    p_render = sub.add_parser("render", help="Render a PDF page as an image")
    p_render.add_argument("file", type=Path, help="PDF file path")
    p_render.add_argument("page_number", type=int, help="Page number (1-based)")
    p_render.add_argument("--dpi", type=int, default=300, help="Render DPI (default: 300)")
    p_render.add_argument("--grayscale", action="store_true", help="Render in grayscale")
    p_render.add_argument(
        "--output", "-o", type=Path, help="Output image path (default: page_N.png)"
    )
    p_render.add_argument("--json", action="store_true", help="Structured JSON output")

    # classify
    p_classify = sub.add_parser("classify", help="Classify document or page type")
    p_classify.add_argument("file", type=Path, help="PDF file path")
    p_classify.add_argument(
        "--page", type=int, default=None, help="Page number (1-based); omit for document-level"
    )
    p_classify.add_argument("--json", action="store_true", help="Structured JSON output")

    # serve
    p_serve = sub.add_parser("serve", help="Start MCP server")
    p_serve.add_argument("--http", action="store_true", help="Use HTTP transport")
    p_serve.add_argument("--host", default="127.0.0.1", help="HTTP host")
    p_serve.add_argument("--port", type=int, default=8000, help="HTTP port")
    p_serve.add_argument("--debug", action="store_true", help="Debug logging")

    args = parser.parse_args(argv)

    handlers = {
        "extract": _cmd_extract,
        "search": _cmd_search,
        "info": _cmd_info,
        "outline": _cmd_outline,
        "page": _cmd_page,
        "render": _cmd_render,
        "classify": _cmd_classify,
        "serve": _cmd_serve,
    }

    try:
        handlers[args.command](args)
    except FileNotFoundError as exc:
        _error(str(exc))
    except Exception as exc:
        _error(str(exc))


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _json_out(data: dict[str, Any]) -> None:
    """Print structured JSON to stdout."""
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _error(msg: str) -> None:
    """Print error to stderr and exit."""
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _cmd_extract(args: argparse.Namespace) -> None:
    """Extract PDF content in the specified format."""
    from kaos_content import serialize_html, serialize_markdown, serialize_text

    from kaos_pdf import parse_pdf, parse_pdf_bytes

    pages = _parse_pages(args.pages) if args.pages else None
    extract_kwargs: dict[str, Any] = {}
    if args.no_headings:
        extract_kwargs["detect_headings"] = False
    if args.no_tables:
        extract_kwargs["extract_tables"] = False
    if args.ocr != "never":
        extract_kwargs["ocr"] = args.ocr
        extract_kwargs["ocr_dpi"] = args.ocr_dpi

    if args.stdin:
        data = sys.stdin.buffer.read()
        if not data:
            _error("No data received on stdin")
        doc = parse_pdf_bytes(data, pages=pages, **extract_kwargs)
    else:
        if args.file is None:
            _error("FILE is required unless --stdin is used")
        path = _validate_file(args.file)
        doc = parse_pdf(path, pages=pages, **extract_kwargs)

    serializers = {
        "markdown": serialize_markdown,
        "text": serialize_text,
        "json": lambda d: d.model_dump_json(indent=2),
        "html": serialize_html,
    }
    output = serializers[args.format](doc)
    _write_output(output, args.output)


def _cmd_search(args: argparse.Namespace) -> None:
    """Search within a PDF by query."""
    from kaos_pdf import parse_pdf, search_document

    path = _validate_file(args.file)
    doc = parse_pdf(path)
    search_results = search_document(doc, args.query, top_k=args.top_k, level=args.level)

    if args.json:
        _json_out(
            {
                "command": "search",
                "file": path.name,
                "query": args.query,
                "level": args.level,
                "total_matches": search_results.total_matches,
                "has_more": search_results.has_more,
                "results": [
                    {
                        "text": r.text,
                        "score": r.score,
                        "page": r.page,
                        "section": r.section_title,
                        "ref": r.block_ref,
                    }
                    for r in search_results.results
                ],
            }
        )
        return

    results = search_results.results
    if not results:
        print(f'No results for "{args.query}"')
        return

    print(f'Results for "{args.query}" ({len(results)} matches):\n')
    for i, r in enumerate(results, 1):
        page_str = f"Page {r.page}" if r.page else "Page ?"
        section_str = f" | {r.section_title}" if r.section_title else ""
        print(f"[{i}] Score: {r.score:.1f} | {page_str}{section_str}")
        for line in r.text.splitlines():
            print(f"    {line}")
        print()


def _cmd_info(args: argparse.Namespace) -> None:
    """Show PDF metadata and summary."""
    from kaos_pdf import classify_document, get_pdf_metadata, get_pdf_outline

    path = _validate_file(args.file)
    meta = get_pdf_metadata(path)
    doc_type = classify_document(path)
    outline = get_pdf_outline(path)

    if args.json:
        _json_out(
            {
                "command": "info",
                "file": path.name,
                "pages": meta.page_count,
                "type": doc_type,
                "outline_entries": len(outline),
                **meta.to_dict(),
            }
        )
        return

    print(f"File:     {path.name}")
    print(f"Pages:    {meta.page_count}")
    print(f"Type:     {doc_type}")
    for value, label in [
        (meta.title, "Title"),
        (meta.author, "Author"),
        (meta.subject, "Subject"),
        (meta.creator, "Creator"),
        (meta.producer, "Producer"),
    ]:
        if value:
            print(f"{label + ':':<10s}{value}")
    if outline:
        print(f"Outline:  {len(outline)} entries")


def _cmd_outline(args: argparse.Namespace) -> None:
    """Show PDF outline (bookmarks or detected headings)."""
    from kaos_pdf import PdfOutlineEntry, get_pdf_outline, parse_pdf

    path = _validate_file(args.file)

    # Try PDF bookmarks first
    outline = get_pdf_outline(path)
    source = "bookmarks"

    if not outline:
        # Fallback: extract headings
        from kaos_content import NodeIndex, extract_text

        doc = parse_pdf(path)
        idx = NodeIndex(doc)
        source = "headings"
        outline = [
            PdfOutlineEntry(
                title=extract_text(h)[:200],
                level=h.depth - 1,
                page=(h.provenance.page - 1) if h.provenance and h.provenance.page else None,
            )
            for h in idx.headings
        ]

    if args.json:
        _json_out(
            {
                "command": "outline",
                "file": path.name,
                "source": source,
                "total": len(outline),
                "entries": [entry.to_dict() for entry in outline],
            }
        )
        return

    if not outline:
        print("No outline or headings found.")
        return

    label = "PDF Bookmarks" if source == "bookmarks" else "Detected Headings"
    print(f"{label}:")
    for entry in outline:
        indent = "  " * entry.level
        page_str = f" (p{entry.page + 1})" if entry.page is not None else ""
        print(f"{indent}{entry.title}{page_str}")


def _cmd_page(args: argparse.Namespace) -> None:
    """Extract text from a single page."""
    from kaos_pdf import extract_page_text, get_page_count

    path = _validate_file(args.file)
    page_idx = args.page_number - 1  # CLI is 1-based, API is 0-based
    if page_idx < 0:
        _error("Page number must be >= 1")

    total_pages = get_page_count(path)
    if page_idx >= total_pages:
        _error(f"Page {args.page_number} out of range (document has {total_pages} pages)")

    text = extract_page_text(path, page_idx)

    if args.json:
        _json_out(
            {
                "command": "page",
                "file": path.name,
                "page": args.page_number,
                "total_pages": total_pages,
                "text": text,
            }
        )
        return

    print(text)


def _cmd_render(args: argparse.Namespace) -> None:
    """Render a PDF page as an image."""
    from kaos_pdf import get_page_count, render_page

    path = _validate_file(args.file)
    page_idx = args.page_number - 1  # CLI is 1-based, API is 0-based
    if page_idx < 0:
        _error("Page number must be >= 1")

    total_pages = get_page_count(path)
    if page_idx >= total_pages:
        _error(f"Page {args.page_number} out of range (document has {total_pages} pages)")

    kaos_image = render_page(path, page_idx, dpi=args.dpi, grayscale=args.grayscale)

    output_path = args.output or Path(f"page_{args.page_number}.png")
    kaos_image.pil.save(str(output_path))

    if args.json:
        _json_out(
            {
                "command": "render",
                "file": path.name,
                "page": args.page_number,
                "output": str(output_path),
                "width": kaos_image.width,
                "height": kaos_image.height,
                "dpi": args.dpi,
            }
        )
        return

    print(
        f"Rendered page {args.page_number} to {output_path}"
        f" ({kaos_image.width}x{kaos_image.height} @ {args.dpi} DPI)"
    )


def _cmd_classify(args: argparse.Namespace) -> None:
    """Classify document or page type."""
    from kaos_pdf import classify_document, classify_page, get_page_count

    path = _validate_file(args.file)

    if args.page is not None:
        page_idx = args.page - 1  # CLI is 1-based, API is 0-based
        if page_idx < 0:
            _error("Page number must be >= 1")

        total_pages = get_page_count(path)
        if page_idx >= total_pages:
            _error(f"Page {args.page} out of range (document has {total_pages} pages)")

        doc_type = classify_page(path, page_idx)
    else:
        doc_type = classify_document(path)

    if args.json:
        _json_out(
            {
                "command": "classify",
                "file": path.name,
                "type": doc_type,
                "page": args.page,
            }
        )
        return

    if args.page is not None:
        print(f"Page {args.page}: {doc_type}")
    else:
        print(f"Document type: {doc_type}")


def _cmd_serve(args: argparse.Namespace) -> None:
    """Start the MCP server."""
    try:
        from kaos_pdf.serve import main as serve_main
    except ImportError:
        _error("MCP server requires the 'mcp' extra.\nInstall with: pip install 'kaos-pdf[mcp]'")

    serve_argv = []
    if args.http:
        serve_argv.extend(["--http", "--host", args.host, "--port", str(args.port)])
    if args.debug:
        serve_argv.append("--debug")
    serve_main(serve_argv)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _validate_file(path: Path) -> Path:
    """Validate that file exists and return resolved path."""
    path = path.resolve()
    if not path.is_file():
        msg = f"File not found: {path}"
        raise FileNotFoundError(msg)
    return path


def _parse_pages(pages_str: str) -> list[int]:
    """Parse page range string into 0-based page indices.

    Accepts: '1-5', '1,3,5', '1-3,7,9-11' (1-based input).
    Returns 0-based indices.
    """
    indices: list[int] = []
    for part in pages_str.split(","):
        part = part.strip()
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start = int(start_s)
            end = int(end_s)
            indices.extend(range(start - 1, end))  # 1-based to 0-based
        else:
            indices.append(int(part) - 1)  # 1-based to 0-based
    return indices


def _write_output(text: str, output_path: Path | None) -> None:
    """Write text to stdout or file."""
    if output_path:
        output_path.write_text(text, encoding="utf-8")
        print(f"Written to {output_path}", file=sys.stderr)
    else:
        print(text)

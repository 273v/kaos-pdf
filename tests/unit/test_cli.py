"""Tests for the kaos-pdf CLI (kaos_pdf.cli)."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from kaos_pdf.cli import _parse_pages, main

FIXTURES = Path(__file__).parent.parent / "fixtures"
TEST1 = str(FIXTURES / "test1.pdf")
TEST4 = str(FIXTURES / "test4.pdf")
COURT = str(FIXTURES / "kl3m_court_woods.pdf")


class TestParsePages:
    def test_single_page(self):
        assert _parse_pages("1") == [0]

    def test_range(self):
        assert _parse_pages("1-3") == [0, 1, 2]

    def test_comma_separated(self):
        assert _parse_pages("1,3,5") == [0, 2, 4]

    def test_mixed(self):
        assert _parse_pages("1-3,7,9-11") == [0, 1, 2, 6, 8, 9, 10]

    def test_single_high_page(self):
        assert _parse_pages("10") == [9]


class TestExtractCommand:
    def test_extract_markdown(self):
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["extract", TEST1])
        output = stdout.getvalue()
        assert len(output) > 0
        assert "#" in output  # markdown headings

    def test_extract_text(self):
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["extract", TEST1, "--format", "text"])
        output = stdout.getvalue()
        assert len(output) > 0

    def test_extract_json(self):
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["extract", TEST1, "--format", "json"])
        output = stdout.getvalue()
        data = json.loads(output)
        assert "body" in data

    def test_extract_html(self):
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["extract", TEST1, "--format", "html"])
        output = stdout.getvalue()
        assert "<" in output  # HTML tags

    def test_extract_with_pages(self):
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["extract", TEST4, "--pages", "1-2"])
        output = stdout.getvalue()
        assert len(output) > 0

    def test_extract_to_file(self, tmp_path):
        outfile = tmp_path / "out.md"
        main(["extract", TEST1, "--output", str(outfile)])
        assert outfile.exists()
        assert len(outfile.read_text()) > 0

    def test_extract_ocr_auto_on_born_digital_is_noop(self):
        # ``--ocr auto`` on a born-digital PDF triggers no OCR (no Tesseract
        # needed) and yields the same output as the default.
        default = StringIO()
        with patch("sys.stdout", default):
            main(["extract", TEST1])
        auto = StringIO()
        with patch("sys.stdout", auto):
            main(["extract", TEST1, "--ocr", "auto"])
        assert auto.getvalue() == default.getvalue()

    def test_extract_ocr_flags_forwarded(self):
        # The CLI flags must reach parse_pdf as the documented kwargs. Mock
        # parse_pdf (so 'always' needs no Tesseract) but return a real doc so
        # serialization still works.
        from kaos_pdf import parse_pdf

        real_doc = parse_pdf(TEST1)
        with (
            patch("kaos_pdf.parse_pdf", return_value=real_doc) as mock_parse,
            patch("sys.stdout", StringIO()),
        ):
            main(["extract", TEST1, "--ocr", "always", "--ocr-dpi", "150"])
        _, kwargs = mock_parse.call_args
        assert kwargs["ocr"] == "always"
        assert kwargs["ocr_dpi"] == 150

    def test_extract_never_omits_ocr_kwargs(self):
        # Default (--ocr never) must not pass ocr kwargs, preserving the
        # legacy no-OCR-dependency contract.
        from kaos_pdf import parse_pdf

        real_doc = parse_pdf(TEST1)
        with (
            patch("kaos_pdf.parse_pdf", return_value=real_doc) as mock_parse,
            patch("sys.stdout", StringIO()),
        ):
            main(["extract", TEST1])
        _, kwargs = mock_parse.call_args
        assert "ocr" not in kwargs

    def test_extract_rejects_invalid_ocr_mode(self):
        with pytest.raises(SystemExit):
            main(["extract", TEST1, "--ocr", "bogus"])


class TestSearchCommand:
    def test_search_found(self):
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["search", TEST4, "Commission"])
        output = stdout.getvalue()
        assert "Commission" in output or "match" in output.lower()

    def test_search_no_results(self):
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["search", TEST1, "xyznonexistent123"])
        output = stdout.getvalue()
        assert "No results" in output


class TestInfoCommand:
    def test_info(self):
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["info", COURT])
        output = stdout.getvalue()
        assert "Pages" in output or "pages" in output


class TestOutlineCommand:
    def test_outline_from_headings(self):
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["outline", COURT])
        output = stdout.getvalue()
        assert len(output) > 0


class TestPageCommand:
    def test_page_one_based(self):
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["page", TEST1, "1"])
        output = stdout.getvalue()
        assert len(output) > 0

    def test_page_json(self):
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["page", TEST1, "1", "--json"])
        data = json.loads(stdout.getvalue())
        assert data["command"] == "page"
        assert data["page"] == 1
        assert "text" in data
        assert "total_pages" in data

    def test_page_zero_exits(self):
        with pytest.raises(SystemExit):
            main(["page", TEST1, "0"])

    def test_page_out_of_range_exits(self):
        with pytest.raises(SystemExit):
            main(["page", TEST1, "9999"])


class TestJsonEnvelope:
    """All --json outputs should have a consistent 'command' and 'file' envelope."""

    def test_info_json_envelope(self):
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["info", COURT, "--json"])
        data = json.loads(stdout.getvalue())
        assert data["command"] == "info"
        assert "file" in data
        assert "pages" in data

    def test_search_json_envelope(self):
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["search", TEST4, "Commission", "--json"])
        data = json.loads(stdout.getvalue())
        assert data["command"] == "search"
        assert "file" in data
        assert "query" in data
        assert "results" in data
        assert isinstance(data["results"], list)

    def test_outline_json_envelope(self):
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["outline", COURT, "--json"])
        data = json.loads(stdout.getvalue())
        assert data["command"] == "outline"
        assert "file" in data
        assert "source" in data
        assert "entries" in data

    def test_page_json_envelope(self):
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["page", TEST1, "1", "--json"])
        data = json.loads(stdout.getvalue())
        assert data["command"] == "page"
        assert data["page"] == 1
        assert "text" in data


class TestRenderCommand:
    def test_render_creates_png(self, tmp_path):
        outfile = tmp_path / "rendered.png"
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["render", TEST1, "1", "--output", str(outfile)])
        assert outfile.exists()
        # Verify it's a valid PNG (starts with PNG magic bytes)
        header = outfile.read_bytes()[:8]
        assert header[:4] == b"\x89PNG"

    def test_render_json_envelope(self, tmp_path):
        outfile = tmp_path / "rendered.png"
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["render", TEST1, "1", "--output", str(outfile), "--json"])
        data = json.loads(stdout.getvalue())
        assert data["command"] == "render"
        assert "file" in data
        assert data["page"] == 1
        assert "output" in data
        assert "width" in data
        assert "height" in data
        assert data["dpi"] == 300

    def test_render_grayscale(self, tmp_path):
        outfile = tmp_path / "gray.png"
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["render", TEST1, "1", "--grayscale", "--output", str(outfile)])
        assert outfile.exists()
        header = outfile.read_bytes()[:8]
        assert header[:4] == b"\x89PNG"

    def test_render_invalid_page_zero(self):
        with pytest.raises(SystemExit):
            main(["render", TEST1, "0"])

    def test_render_invalid_page_out_of_range(self):
        with pytest.raises(SystemExit):
            main(["render", TEST1, "9999"])


class TestClassifyCommand:
    def test_classify_document(self):
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["classify", TEST1])
        output = stdout.getvalue().strip()
        assert "Document type:" in output
        # Should be one of the known types
        known_types = ("text", "image", "mixed", "scanned", "blank")
        assert any(t in output for t in known_types)

    def test_classify_page(self):
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["classify", TEST1, "--page", "1"])
        output = stdout.getvalue().strip()
        assert "Page 1:" in output
        known_types = ("text", "image", "mixed", "blank")
        assert any(t in output for t in known_types)

    def test_classify_json_envelope(self):
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["classify", TEST1, "--json"])
        data = json.loads(stdout.getvalue())
        assert data["command"] == "classify"
        assert "file" in data
        assert "type" in data
        assert "page" in data
        assert data["page"] is None  # document-level

    def test_classify_page_json_envelope(self):
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["classify", TEST1, "--page", "1", "--json"])
        data = json.loads(stdout.getvalue())
        assert data["command"] == "classify"
        assert data["page"] == 1
        assert "type" in data


class TestExtractEnhancements:
    def test_extract_no_headings(self):
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["extract", TEST1, "--no-headings"])
        output = stdout.getvalue()
        assert len(output) > 0

    def test_extract_no_tables(self):
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["extract", TEST4, "--no-tables"])
        output = stdout.getvalue()
        assert len(output) > 0


class TestErrorHandling:
    def test_missing_file(self):
        with pytest.raises(SystemExit):
            main(["extract", "/nonexistent/file.pdf"])

    def test_no_command(self):
        with pytest.raises(SystemExit):
            main([])

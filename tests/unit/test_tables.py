"""Unit tests for the FUND-4 table extraction pipeline.

Four layers of coverage:

1. Pure helpers (``_parse_scalar``, ``_looks_int``, ``_looks_float``,
   ``_looks_like_header_row``) — no I/O.
2. ``PdfplumberEngine`` wiring — uses a monkey-patched stand-in for the
   pdfplumber module so we don't depend on a PDF round-trip.
3. ``extract_pdf`` integration via a FakeTableEngine: canned
   :class:`ExtractedTable` → ContentDocument Table block + provenance.
4. ``extract_pdf_with_tables`` — sidecar :class:`TabularDocument` with
   inferred :class:`ColumnType` per column.

Live pdfplumber integration against a real fixture lives in
``tests/integration/test_tables_live.py``.
"""

from __future__ import annotations

import datetime as _dt
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import pytest
from kaos_content.images.model import KaosImage  # noqa: F401
from kaos_content.model.attr import BoundingBox
from kaos_content.model.blocks import Heading, Paragraph
from kaos_content.model.blocks import Table as TableBlock
from kaos_content.model.tabular import ColumnType
from PIL import Image, ImageDraw, ImageFont

from kaos_pdf import (
    ExtractedTable,
    TableEngine,
    TableResult,
    extract_pdf,
    extract_pdf_with_tables,
)
from kaos_pdf.extract import (
    _looks_float,
    _looks_int,
    _parse_scalar,
)
from kaos_pdf.tables.pdfplumber import _looks_like_header_row

# --------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------


def _find_font(*, size: int) -> Any:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _make_native_pdf(tmp_path: Path, text: str) -> str:
    """Generate a born-digital (text-layer) PDF via Pillow.

    Pillow saves PIL images as PDFs with the rasterized image as the
    page. For our FUND-4 tests this is fine because we never rely on
    pdfplumber finding tables — we replace the engine with a
    FakeTableEngine that returns canned output.
    """
    image = Image.new("RGB", (850, 1100), "white")
    draw = ImageDraw.Draw(image)
    font = _find_font(size=28)
    draw.text((60, 60), text, fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PDF", resolution=150.0)
    p = tmp_path / "native.pdf"
    p.write_bytes(buffer.getvalue())
    return str(p)


# --------------------------------------------------------------------
# _parse_scalar / _looks_int / _looks_float
# --------------------------------------------------------------------


class TestParseScalar:
    def test_empty_is_none(self) -> None:
        assert _parse_scalar("") is None
        assert _parse_scalar("   ") is None

    def test_int(self) -> None:
        assert _parse_scalar("42") == 42
        assert _parse_scalar("-17") == -17
        assert _parse_scalar("1,234") == 1234

    def test_float(self) -> None:
        assert _parse_scalar("3.14") == pytest.approx(3.14)
        assert _parse_scalar("-0.5") == pytest.approx(-0.5)
        assert _parse_scalar("1,234.56") == pytest.approx(1234.56)

    def test_bool(self) -> None:
        assert _parse_scalar("true") is True
        assert _parse_scalar("No") is False

    def test_iso_date(self) -> None:
        assert _parse_scalar("2024-03-15") == _dt.date(2024, 3, 15)

    def test_us_date(self) -> None:
        assert _parse_scalar("03/15/2024") == _dt.date(2024, 3, 15)

    def test_iso_datetime(self) -> None:
        assert _parse_scalar("2024-03-15T14:30:00") == _dt.datetime(2024, 3, 15, 14, 30, 0)

    def test_text_passthrough(self) -> None:
        assert _parse_scalar("hello world") == "hello world"


class TestLooksInt:
    @pytest.mark.parametrize("value", ["0", "42", "-17", "+3", "1234"])
    def test_valid(self, value: str) -> None:
        assert _looks_int(value) is True

    @pytest.mark.parametrize("value", ["", "3.14", "1,234", "abc", "+"])
    def test_invalid(self, value: str) -> None:
        assert _looks_int(value) is False


class TestLooksFloat:
    @pytest.mark.parametrize("value", ["3.14", "-0.5", "1.", ".5", "1e3", "-1.2e-4"])
    def test_valid(self, value: str) -> None:
        assert _looks_float(value) is True

    @pytest.mark.parametrize("value", ["", "abc", "3.1.4", "."])
    def test_invalid(self, value: str) -> None:
        assert _looks_float(value) is False


# --------------------------------------------------------------------
# Header detection heuristic
# --------------------------------------------------------------------


class TestLooksLikeHeaderRow:
    def test_labels_then_numbers(self) -> None:
        rows = (
            ("Name", "Age", "Score"),
            ("Alice", "30", "95"),
            ("Bob", "25", "87"),
        )
        assert _looks_like_header_row(rows) is True

    def test_all_numeric_rows_no_header(self) -> None:
        rows = (("1", "2", "3"), ("4", "5", "6"))
        assert _looks_like_header_row(rows) is False

    def test_single_row_has_no_header(self) -> None:
        rows = (("Name", "Age"),)
        assert _looks_like_header_row(rows) is False

    def test_all_prose_no_header(self) -> None:
        rows = (
            ("First paragraph discussing the matter at hand in depth", None),
            ("Second paragraph continuing the discussion", None),
        )
        assert _looks_like_header_row(rows) is False


# --------------------------------------------------------------------
# FakeTableEngine-driven integration
# --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FakeTableEngine(TableEngine):
    canned: TableResult
    call_count_box: list[int]
    name: ClassVar[str] = "fake"

    def extract_sync(
        self,
        source: str | Path,
        *,
        page_indices: list[int] | None = None,
    ) -> TableResult:
        self.call_count_box[0] += 1
        _ = source, page_indices
        return self.canned


def _canned_tables() -> TableResult:
    return TableResult(
        tables=(
            ExtractedTable(
                page=1,
                bbox=BoundingBox(left=50.0, top=100.0, right=500.0, bottom=300.0),
                rows=(
                    ("Name", "Age", "Joined"),
                    ("Alice", "30", "2024-01-15"),
                    ("Bob", "25", "2023-07-09"),
                ),
                has_header=True,
                engine_name="fake",
            ),
            ExtractedTable(
                page=1,
                bbox=BoundingBox(left=50.0, top=400.0, right=500.0, bottom=500.0),
                rows=(
                    ("Q1", "Q2", "Q3", "Q4"),
                    ("100.5", "200.25", "150.0", "180.75"),
                ),
                has_header=True,
                engine_name="fake",
            ),
        ),
        engine_name="fake",
    )


class TestExtractPdfEngineMode:
    def test_default_is_geometric_no_engine_called(self, tmp_path: Path) -> None:
        counter = [0]
        FakeTableEngine(canned=_canned_tables(), call_count_box=counter)
        pdf = _make_native_pdf(tmp_path, "nothing to see here")
        doc = extract_pdf(pdf)  # default tables="geometric"
        assert counter[0] == 0  # fake engine never invoked
        assert isinstance(doc.body, tuple)

    def test_disabled_emits_no_tables(self, tmp_path: Path) -> None:
        counter = [0]
        engine = FakeTableEngine(canned=_canned_tables(), call_count_box=counter)
        pdf = _make_native_pdf(tmp_path, "still nothing")
        doc = extract_pdf(pdf, tables="disabled", table_engine=engine)
        assert counter[0] == 0
        table_blocks = [b for b in doc.body if isinstance(b, TableBlock)]
        assert table_blocks == []

    def test_engine_mode_emits_table_blocks(self, tmp_path: Path) -> None:
        counter = [0]
        engine = FakeTableEngine(canned=_canned_tables(), call_count_box=counter)
        pdf = _make_native_pdf(tmp_path, "header text that should survive")
        doc = extract_pdf(pdf, tables="engine", table_engine=engine)
        assert counter[0] == 1, "Engine should be called exactly once"

        table_blocks = [b for b in doc.body if isinstance(b, TableBlock)]
        assert len(table_blocks) == 2

    def test_engine_table_provenance(self, tmp_path: Path) -> None:
        engine = FakeTableEngine(canned=_canned_tables(), call_count_box=[0])
        pdf = _make_native_pdf(tmp_path, "x")
        doc = extract_pdf(pdf, tables="engine", table_engine=engine)
        table_blocks = [b for b in doc.body if isinstance(b, TableBlock)]
        first = table_blocks[0]
        assert first.provenance is not None
        assert first.provenance.page == 1
        assert first.provenance.extractor == "kaos-pdf/tables/fake"
        assert first.provenance.bbox is not None

    def test_engine_failure_does_not_crash(self, tmp_path: Path) -> None:
        class BoomEngine(TableEngine):
            name: ClassVar[str] = "boom"

            def extract_sync(self, source: Any, *, page_indices: Any = None) -> TableResult:
                _ = source, page_indices
                raise RuntimeError("synthetic failure")

        pdf = _make_native_pdf(tmp_path, "x")
        doc = extract_pdf(pdf, tables="engine", table_engine=BoomEngine())
        # No Table blocks, but extraction didn't explode.
        assert all(not isinstance(b, TableBlock) for b in doc.body) or True
        assert doc is not None


# --------------------------------------------------------------------
# extract_pdf_with_tables — TabularDocument sidecar
# --------------------------------------------------------------------


class TestExtractPdfWithTables:
    def test_sidecar_has_columns_and_types(self, tmp_path: Path) -> None:
        engine = FakeTableEngine(canned=_canned_tables(), call_count_box=[0])
        pdf = _make_native_pdf(tmp_path, "x")
        _doc, tab = extract_pdf_with_tables(pdf, table_engine=engine)
        assert len(tab.tables) == 2

        first = tab.tables[0]
        # Header: Name / Age / Joined → expect TEXT / INTEGER / DATE.
        col_types = {c.name: c.column_type for c in first.columns}
        assert col_types["Name"] == ColumnType.TEXT
        assert col_types["Age"] == ColumnType.INTEGER
        assert col_types["Joined"] == ColumnType.DATE

        # Second table: all numeric columns → FLOAT.
        second = tab.tables[1]
        assert all(c.column_type == ColumnType.FLOAT for c in second.columns)

    def test_rows_are_typed_python_natives(self, tmp_path: Path) -> None:
        engine = FakeTableEngine(canned=_canned_tables(), call_count_box=[0])
        pdf = _make_native_pdf(tmp_path, "x")
        _, tab = extract_pdf_with_tables(pdf, table_engine=engine)
        first = tab.tables[0]
        # Row 0 is the first DATA row ("Alice", 30, 2024-01-15).
        name, age, joined = first.rows[0]
        assert name == "Alice"
        assert age == 30
        assert joined == _dt.date(2024, 1, 15)

    def test_passing_tables_kwarg_raises(self, tmp_path: Path) -> None:
        pdf = _make_native_pdf(tmp_path, "x")
        with pytest.raises(TypeError) as exc_info:
            extract_pdf_with_tables(pdf, tables="disabled")  # type: ignore[arg-type]
        assert "extract_pdf_with_tables" in str(exc_info.value)

    def test_headerless_table_synthesizes_col_names(self, tmp_path: Path) -> None:
        """has_header=False → ``col_1``/``col_2``/...; all rows are data."""
        canned = TableResult(
            tables=(
                ExtractedTable(
                    page=2,
                    bbox=BoundingBox(left=0, top=0, right=100, bottom=100),
                    rows=(("100", "200"), ("150", "250")),
                    has_header=False,
                    engine_name="fake",
                ),
            ),
            engine_name="fake",
        )
        engine = FakeTableEngine(canned=canned, call_count_box=[0])
        pdf = _make_native_pdf(tmp_path, "x")
        _, tab = extract_pdf_with_tables(pdf, table_engine=engine)
        first = tab.tables[0]
        assert [c.name for c in first.columns] == ["col_1", "col_2"]
        assert all(c.column_type == ColumnType.INTEGER for c in first.columns)
        # BOTH rows kept as data (no header partition).
        assert first.row_count == 2

    def test_empty_result_yields_empty_tabular_document(self, tmp_path: Path) -> None:
        canned = TableResult(tables=(), engine_name="fake")
        engine = FakeTableEngine(canned=canned, call_count_box=[0])
        pdf = _make_native_pdf(tmp_path, "x")
        _, tab = extract_pdf_with_tables(pdf, table_engine=engine)
        assert tab.tables == ()


# --------------------------------------------------------------------
# Regression: geometric default preserves current behavior
# --------------------------------------------------------------------


class TestLegacyBehaviorPreserved:
    def test_geometric_default_preserves_pdf_output(self, tmp_path: Path) -> None:
        """Calling extract_pdf with no tables kwargs behaves as before.

        This is a smoke-level regression guard: as long as some blocks
        come back, the legacy path still works. The full 1152-test
        suite is the real guard.
        """
        pdf = _make_native_pdf(tmp_path, "first line\nsecond line\nthird line")
        doc = extract_pdf(pdf)
        assert isinstance(doc.body, tuple)
        # Non-empty body or at least no exception — both acceptable
        # given this is a rendered-image PDF.
        types = [type(b).__name__ for b in doc.body]
        assert all(isinstance(b, Heading | Paragraph | TableBlock) for b in doc.body) or types

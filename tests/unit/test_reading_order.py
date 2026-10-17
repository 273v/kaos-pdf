"""Tests for char-indexed fragments, PDFium line grouping, and stream reading order."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pytest
from kaos_content import serialize_text

from kaos_pdf.extract import (
    ReadingOrder,
    _combine_rects,
    _get_char_fragments,
    _merge_column_paragraphs,
    _page_chars,
    _stream_lines,
    _stream_order_is_plausible,
    extract_pdf,
)

Rect = tuple[str, tuple[float, float, float, float], dict[str, Any] | None]


def _frag(text: str, left: float, top: float, line_id: int | None, width: float = 50.0) -> Rect:
    fi: dict[str, Any] = {"font_size": 10.0}
    if line_id is not None:
        fi["line_id"] = line_id
    return (text, (left, top - 10.0, left + width, top), fi)


class TestCharFragments:
    def test_no_doubled_glyphs_from_overlapping_rects(self, fixtures_dir: Path) -> None:
        # FPDFText_GetBoundedText(rect) returned neighbouring characters of
        # overlapping rects twice ("forward, I I would", "h his p postal").
        pdf = pdfium.PdfDocument(str(fixtures_dir / "kl3m_court_woods.pdf"))
        try:
            tp = pdf[1].get_textpage()
            frags = _get_char_fragments(tp)
            page_text = "".join(t for t, _, _ in frags)
            tp.close()
        finally:
            pdf.close()
        assert "forward, I would" in page_text
        assert "I I " not in page_text
        text = serialize_text(extract_pdf(fixtures_dir / "kl3m_court_woods.pdf"))
        assert "lose his postal-service job" in text
        assert not re.search(r"\bh his\b|\bp postal\b", text)

    def test_fragments_carry_line_ids_in_char_order(self, fixtures_dir: Path) -> None:
        pdf = pdfium.PdfDocument(str(fixtures_dir / "test4.pdf"))
        try:
            tp = pdf[0].get_textpage()
            frags = _get_char_fragments(tp)
            tp.close()
        finally:
            pdf.close()
        ids = [fi["line_id"] for _, _, fi in frags]
        assert ids == sorted(ids)
        # A hyphen marker ends its PDFium line even though PDFium's page text
        # continues the word without a break.
        marked = [t for t, _, _ in frags if t.rstrip().endswith("\x02")]
        assert marked

    def test_page_chars_surrogate_pairs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import kaos_pdf.extract as ex

        cps = [0x41, 0xD835, 0xDC00, 0xD800, 0x42]
        monkeypatch.setattr(ex.pdfium_raw, "FPDFText_GetUnicode", lambda _tp, i: cps[i])
        assert _page_chars(object(), len(cps)) == ["A", "\U0001d400", "", "\ufffd", "B"]


class TestLineGrouping:
    def test_pdfium_line_kept_together_across_baseline_jitter(self) -> None:
        # OCR layers place words of one line a few points apart vertically.
        frags = [
            _frag("Whereas it ", 72, 700, 1, 60),
            _frag("appears ", 132, 696.5, 1, 40),
            _frag("to this meeting", 172, 700, 1, 80),
            _frag("object attempted", 72, 686, 2, 90),
        ]
        lines = _combine_rects(frags, y_tolerance=2.0)
        assert [t for t, _, _ in lines] == [
            "Whereas it appears to this meeting",
            "object attempted",
        ]

    def test_without_line_ids_y_tolerance_rule_applies(self) -> None:
        frags = [
            _frag("left ", 72, 700, None),
            _frag("right", 300, 699, None),
            _frag("next", 72, 680, None),
        ]
        assert [t for t, _, _ in _combine_rects(frags)] == ["left right", "next"]


class TestStreamOrder:
    def _two_column_page(self) -> list[Rect]:
        # Column 1 then column 2, as a content stream draws them.
        frags = []
        lid = 0
        for col_left in (72.0, 320.0):
            for i in range(5):
                frags.append(_frag(f"c{int(col_left)} line {i}", col_left, 700 - 14 * i, lid, 200))
                lid += 1
        return frags

    def test_stream_lines_keep_column_order(self) -> None:
        lines = _stream_lines(self._two_column_page())
        assert [t for t, _, _ in lines][:6] == [
            "c72 line 0",
            "c72 line 1",
            "c72 line 2",
            "c72 line 3",
            "c72 line 4",
            "c320 line 0",
        ]
        assert _stream_order_is_plausible(lines)

    def test_geometric_combine_interleaves_same_page(self) -> None:
        # The legacy sort puts same-baseline lines of both columns together;
        # stream order exists to avoid exactly this.
        lines = _combine_rects(
            [
                (t, b, {k: v for k, v in (f or {}).items() if k != "line_id"})
                for t, b, f in self._two_column_page()
            ]
        )
        assert lines[0][0] == "c72 line 0c320 line 0"

    def test_implausible_stream_order_detected(self) -> None:
        # Lines drawn bottom-up in one column: every step goes back up.
        frags = [_frag(f"line {i}", 72, 500 + 14 * i, i, 300) for i in range(12)]
        assert not _stream_order_is_plausible(_stream_lines(frags))

    def test_few_backward_steps_tolerated(self) -> None:
        frags = [_frag(f"line {i}", 72, 700 - 14 * i, i, 300) for i in range(20)]
        frags.append(_frag("Running header", 72, 760, 99, 300))  # drawn last
        assert _stream_order_is_plausible(_stream_lines(frags))

    def test_keep_order_merge_does_not_cross_columns(self) -> None:
        lines = _stream_lines(self._two_column_page())
        merged = _merge_column_paragraphs(lines, keep_order=True)
        texts = [t for t, _, _ in merged]
        assert len(texts) == 2
        assert texts[0].startswith("c72 line 0") and texts[0].endswith("c72 line 4")
        assert texts[1].startswith("c320 line 0")


class TestReadingOrderOption:
    @pytest.mark.parametrize("order", ["auto", "stream", "geometric"])
    def test_modes_extract_same_content(self, fixtures_dir: Path, order: ReadingOrder) -> None:
        text = serialize_text(
            extract_pdf(fixtures_dir / "kl3m_fda_guidance.pdf", reading_order=order)
        )
        assert "Food and Drug Administration" in text

    def test_three_column_page_not_interleaved(self, fixtures_dir: Path) -> None:
        # Federal Register, 3 columns: in stream order each sentence stays in
        # its own column; the geometric sort joins lines across columns.
        doc = extract_pdf(fixtures_dir / "kl3m_fda_guidance.pdf", merge_column_paragraphs=True)
        text = serialize_text(doc)
        assert (
            "County Business Patterns show that 11.5 percent of all grocery stores are in "
            "jurisdictions that have relevant menu labeling regulations."
        ) in text

"""Pleading-paper line numbers, court e-filing stamps and running page counters.

Synthetic PDFs (reportlab) isolate one layout each; ``recap_casd_pleading_order.pdf``
is a real CM/ECF filing on pleading paper (see ``tests/fixtures/README.md``).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from kaos_content import serialize_text

from kaos_pdf import parse_pdf
from kaos_pdf.extract import (
    _is_ecf_stamp,
    _line_number_gutter,
    _running_page_stamps,
)

Rect = tuple[str, tuple[float, float, float, float], dict[str, Any] | None]
Canvas = Any
PageDrawer = Callable[[Canvas, int, int], None]

TOP = 720.0
PITCH = 24.0
STAMP = "Case 3:13-cv-00133-WHO Document 244 Filed 01/08/14 Page {page} of {pages}"


def _write_pdf(path: Path, n_pages: int, draw: PageDrawer) -> Path:
    pytest.importorskip("reportlab")
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=LETTER)
    for page in range(1, n_pages + 1):
        draw(c, page, n_pages)
        c.showPage()
    c.save()
    return path


SHEETS = ("first", "second", "third")


def _body_line(page: int, row: int) -> str:
    # Body lines carry no page number: a number that advances with the page
    # at a fixed place is exactly what a running page counter looks like.
    return f"Row {row} of the {SHEETS[page - 1]} sheet finds the motion well taken."


def _pleading_page(*, numbers: bool = True, split_digits: bool = False) -> PageDrawer:
    def draw(c: Canvas, page: int, pages: int) -> None:
        c.setFont("Helvetica", 9)
        c.drawString(110, 770, STAMP.format(page=page, pages=pages))
        for row in range(1, 29):
            y = TOP - (row - 1) * PITCH
            c.setFont("Times-Roman", 12)
            if numbers and split_digits and row >= 10:
                tens, ones = divmod(row, 10)
                c.drawString(68, y, str(tens))  # one text object per digit
                c.drawString(74, y, str(ones))
            elif numbers:
                c.drawRightString(80, y, str(row))
            c.drawString(100, y, _body_line(page, row))
        c.drawCentredString(306, 30, str(page))

    return draw


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.split("\n") if line.strip()]


class TestPleadingPaper:
    def test_line_numbers_and_stamps_removed(self, tmp_path: Path) -> None:
        pdf = _write_pdf(tmp_path / "pleading.pdf", 3, _pleading_page())
        doc = parse_pdf(pdf)
        lines = _lines(serialize_text(doc))

        assert not [line for line in lines if line.isdigit()]
        assert not [line for line in lines if "Document 244" in line]
        for page in (1, 2, 3):
            for row in range(1, 29):
                # Each body line survives on its own, with no number glued on.
                assert _body_line(page, row) in lines

        removed = (doc.metadata.extra or {})["furniture_removed"]
        assert removed["line_number_pages"] == 3
        assert removed["line_number_fragments"] == 84
        assert removed["page_stamps"] >= 6  # the CM/ECF band and the page number

    def test_flag_off_keeps_line_numbers(self, tmp_path: Path) -> None:
        pdf = _write_pdf(tmp_path / "pleading.pdf", 3, _pleading_page())
        doc = parse_pdf(pdf, strip_line_numbers=False)
        text = serialize_text(doc)
        assert "28" in text
        assert "27" in text
        assert "line_number_fragments" not in (doc.metadata.extra or {}).get(
            "furniture_removed", {}
        )
        # Stamps are page furniture regardless of the flag.
        assert "Document 244" not in text

    def test_two_digit_numbers_drawn_per_digit(self, tmp_path: Path) -> None:
        pdf = _write_pdf(tmp_path / "split.pdf", 2, _pleading_page(split_digits=True))
        doc = parse_pdf(pdf)
        lines = _lines(serialize_text(doc))
        assert not [line for line in lines if line.isdigit()]
        assert _body_line(2, 15) in lines
        assert (doc.metadata.extra or {})["furniture_removed"]["line_number_pages"] == 2

    def test_single_page_filing_stamp_removed(self, tmp_path: Path) -> None:
        pdf = _write_pdf(tmp_path / "one.pdf", 1, _pleading_page(numbers=False))
        text = serialize_text(parse_pdf(pdf))
        assert "Document 244" not in text
        assert _body_line(1, 1) in _lines(text)

    def test_real_recap_filing(self, fixtures_dir: Path) -> None:
        doc = parse_pdf(fixtures_dir / "recap_casd_pleading_order.pdf")
        lines = _lines(serialize_text(doc))
        assert not [line for line in lines if line.isdigit()]
        assert not [line for line in lines if "Filed 11/02/07" in line]
        # The order's own text is intact and no line number is glued onto it.
        assert "IT IS SO ORDERED." in lines
        assert any(line.startswith("Accordingly, the Court") for line in lines)
        assert any("Civil No. 07cv1990" in line for line in lines)
        removed = (doc.metadata.extra or {})["furniture_removed"]
        assert removed["line_number_pages"] == 2
        assert removed["line_number_fragments"] == 56


class TestNoFalsePositives:
    def test_two_column_document_unchanged(self, tmp_path: Path) -> None:
        def draw(c: Canvas, page: int, _pages: int) -> None:
            c.setFont("Times-Roman", 10)
            for row in range(40):
                y = 740 - row * 16
                c.drawString(60, y, f"Left {SHEETS[page - 1]} row {row} reports findings.")
                c.drawString(320, y, f"Right {SHEETS[page - 1]} row {row} continues on.")

        pdf = _write_pdf(tmp_path / "columns.pdf", 2, draw)
        on = parse_pdf(pdf)
        off = parse_pdf(pdf, strip_line_numbers=False)
        assert serialize_text(on) == serialize_text(off)
        assert "furniture_removed" not in (on.metadata.extra or {})
        text = serialize_text(on)
        for page in (1, 2):
            for row in range(40):
                assert f"Left {SHEETS[page - 1]} row {row} reports findings." in text
                assert f"Right {SHEETS[page - 1]} row {row} continues on." in text

    def test_numbered_statute_lines_kept(self, tmp_path: Path) -> None:
        def draw(c: Canvas, _page: int, _pages: int) -> None:
            c.setFont("Times-Roman", 12)
            c.drawString(
                72, 740, "SEC. 2. DEFINITIONS AND APPLICATION OF THIS ACT TO COVERED PERSONS."
            )
            c.drawString(
                72, 724, "In this Act the following terms apply to each covered person and agency."
            )
            # One-line numbered items: the label is its own text object at the
            # body's left edge, the item text follows on the same line.
            for k in range(1, 13):
                y = 700 - (k - 1) * 18
                c.drawString(72, y, str(k))
                c.drawString(90, y, f"The term in item {k} has the meaning given in section 101.")
            c.drawString(
                72, 470, "Nothing in this section limits the authority of the Secretary under"
            )
            c.drawString(
                72, 454, "any other provision of law, including the provisions of subsection (b)."
            )
            c.drawString(
                72, 438, "The definitions above apply to every part of this title as amended."
            )

        pdf = _write_pdf(tmp_path / "statute.pdf", 1, draw)
        doc = parse_pdf(pdf)
        assert "line_number_fragments" not in (doc.metadata.extra or {}).get(
            "furniture_removed", {}
        )
        lines = _lines(serialize_text(doc))
        for k in range(1, 13):
            item = f"The term in item {k} has the meaning given in section 101."
            # The label stays with its item, on the same line.
            assert any(line.startswith(str(k)) and line.endswith(item) for line in lines)

    def test_footnote_numbers_kept(self, tmp_path: Path) -> None:
        def draw(c: Canvas, _page: int, _pages: int) -> None:
            c.setFont("Times-Roman", 12)
            for row in range(20):
                c.drawString(
                    72, 740 - row * 24, f"Body sentence {row} cites authority for the point."
                )
            c.setFont("Times-Roman", 9)
            for k in range(4, 13):  # one-line footnotes 4..12 at a constant pitch
                y = 230 - (k - 4) * 14
                c.drawString(108, y, str(k))
                c.drawString(116, y, f"See Doc. No. {k + 40} at {k}.")

        pdf = _write_pdf(tmp_path / "footnotes.pdf", 1, draw)
        doc = parse_pdf(pdf)
        assert "line_number_fragments" not in (doc.metadata.extra or {}).get(
            "furniture_removed", {}
        )
        text = serialize_text(doc)
        for k in range(4, 13):
            assert f"See Doc. No. {k + 40} at {k}." in text
            assert str(k) in text

    def test_table_row_numbers_kept(self, tmp_path: Path) -> None:
        def draw(c: Canvas, _page: int, _pages: int) -> None:
            c.setFont("Times-Roman", 11)
            c.drawString(
                72, 740, "Table 1 lists each claim with the amount allowed by the trustee."
            )
            c.drawString(72, 724, "No.")
            c.drawString(110, 724, "Claimant")
            c.drawString(400, 724, "Amount")
            for k in range(1, 21):
                y = 706 - (k - 1) * 16
                c.drawString(72, y, str(k))
                c.drawString(110, y, f"Creditor number {k}")
                c.drawString(400, y, f"{k * 125}.00")
            c.drawString(
                72, 360, "The trustee may amend the schedule of allowed claims at any time."
            )

        pdf = _write_pdf(tmp_path / "table.pdf", 1, draw)
        doc = parse_pdf(pdf)
        assert "line_number_fragments" not in (doc.metadata.extra or {}).get(
            "furniture_removed", {}
        )


def _frag(text: str, left: float, y: float, width: float = 6.0) -> Rect:
    return (text, (left, y, left + width, y + 8.0), {})


class TestLineNumberGutter:
    def test_left_margin_column_detected(self) -> None:
        frags = [_frag(str(k), 70, TOP - (k - 1) * PITCH) for k in range(1, 29)]
        frags += [_frag(f"body {k}", 100, TOP - (k - 1) * PITCH, 400) for k in range(1, 29)]
        assert _line_number_gutter(frags) == set(range(28))

    def test_body_text_crossing_column_rejects(self) -> None:
        frags = [_frag(str(k), 70, TOP - (k - 1) * PITCH) for k in range(1, 29)]
        frags.append(_frag("A paragraph that starts in the margin", 60, TOP - 5 * PITCH + 2, 400))
        assert _line_number_gutter(frags) == set()

    def test_irregular_pitch_rejects(self) -> None:
        ys = [TOP - sum((24, 40, 18, 55)[j % 4] for j in range(k)) for k in range(12)]
        frags = [_frag(str(k + 1), 70, y) for k, y in enumerate(ys)]
        frags += [_frag("item text", 100, y, 300) for y in ys]
        assert _line_number_gutter(frags) == set()


class TestEcfStamp:
    @pytest.mark.parametrize(
        "text",
        [
            "Case 3:13-cv-00133-WHO Document 244 Filed 01/08/14 Page 2 of 141",
            "Case 2:15-cv-09640-TJH-KES Document 17 Filed 03/11/16 Page 1 of 1 Page ID #:132",
            "Case 20-10250-btb Doc 13 Entered 02/05/20 16:58:54 Page 2 of 90",
            "Case: 18-17274, 12/07/2018, ID: 11114605, DktEntry: 10-1, Page 2 of 65",
            "Case: 17-1331 Document: 003112567206 Page: 57 Date Filed: 03/20/2017",
            "Case 05-41406 Doc 8-1 Filed 10/30/05 Entered 10/30/05 10:27:08 Desc Schedule",
        ],
    )
    def test_stamp_variants(self, text: str) -> None:
        assert _is_ecf_stamp(text)

    @pytest.mark.parametrize(
        "text",
        [
            "In that case the document was filed late.",
            "Case No. 3:13-cv-00133-WHO",
            "The Court filed its order in Case 1:12-cv-1 on the docket.",
        ],
    )
    def test_ordinary_text(self, text: str) -> None:
        assert not _is_ecf_stamp(text)


class TestRunningPageStamps:
    @staticmethod
    def _pages(top: list[str], body: str = "body text line") -> list[list[Rect]]:
        pages = []
        for header in top:
            rects: list[Rect] = [(header, (50.0, 760.0, 500.0, 770.0), None)]
            rects += [
                (f"{body} {j}", (50.0, 700.0 - 20 * j, 500.0, 710.0 - 20 * j), None)
                for j in range(30)
            ]
            pages.append(rects)
        return pages

    def test_page_counter_header_excluded_on_every_page(self) -> None:
        heads = [f"Federal Register / Vol. 79 / Notices {38901 + i}" for i in range(4)]
        pages = self._pages(heads)
        excluded = _running_page_stamps(pages)
        firsts = [sum(len(p) for p in pages[:i]) for i in range(4)]
        assert set(firsts) <= excluded

    def test_numbers_that_do_not_follow_pages_are_kept(self) -> None:
        heads = ["Table 3 continued", "Table 3 continued", "Table 7 continued", "Table 2 continued"]
        pages = self._pages(heads)
        assert _running_page_stamps(pages) == set()

    def test_two_counters_sharing_a_shape(self) -> None:
        pages = []
        for i in range(4):
            pages.append(
                [
                    (f"{i + 1} of 76", (270.0, 10.0, 320.0, 16.0), None),
                    (f"{i + 3} of 74", (270.0, 40.0, 320.0, 46.0), None),
                ]
                + [
                    (f"text {j}", (50.0, 700.0 - 20 * j, 500.0, 710.0 - 20 * j), None)
                    for j in range(30)
                ]
            )
        excluded = _running_page_stamps(pages, page_heights=[792.0] * 4)
        flat = [t for p in pages for t, _b, _f in p]
        assert {flat[i] for i in excluded} == {f"{i + 1} of 76" for i in range(4)} | {
            f"{i + 3} of 74" for i in range(4)
        }

    def test_footnote_numbers_matching_their_pages_kept(self) -> None:
        # Footnote 2 at the foot of page 2 and footnote 4 at the foot of page 4.
        pages = []
        for i in range(6):
            rects: list[Rect] = [
                (f"text {j}", (50.0, 700.0 - 20 * j, 500.0, 710.0 - 20 * j), None)
                for j in range(30)
            ]
            if i in (1, 3):
                rects.append((str(i + 1), (60.0, 60.0, 66.0, 68.0), None))
            pages.append(rects)
        assert _running_page_stamps(pages, page_heights=[792.0] * 6) == set()

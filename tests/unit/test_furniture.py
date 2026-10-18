"""Unit tests for running header/footer and production-slug filtering.

Covers ``_filter_headers_footers``, ``_repeated_page_furniture`` and
``_is_watermark`` on synthetic page rectangles — no I/O.
"""

from __future__ import annotations

from typing import Any

from kaos_pdf.extract import _filter_headers_footers, _is_watermark, _repeated_page_furniture

Rect = tuple[str, tuple[float, float, float, float], dict[str, Any] | None]


def _r(text: str, y: float, h: float = 10.0) -> Rect:
    """A rect spanning x 50..500 with its low edge at ``y``."""
    return (text, (50.0, y, 500.0, y + h), None)


def _page(
    header: str, body: list[str], footer: str | None = None, *, header_y: float = 740.0
) -> list[Rect]:
    rects = [_r(header, header_y)]
    y = 700.0
    for line in body:
        rects.append(_r(line, y))
        y -= 20.0
    rects.append(_r(footer if footer is not None else "", 40.0))
    return rects


def _flat(pages: list[list[Rect]]) -> list[str]:
    return [t for page in pages for t, _b, _f in page]


class TestRepeatedPageFurniture:
    def test_running_header_kept_once(self) -> None:
        pages = [
            _page("Park et al. BMC Plant Biology 2010", [f"body {i} line {j}" for j in range(30)])
            for i in range(4)
        ]
        excluded = _repeated_page_furniture(pages)
        flat = _flat(pages)
        kept = [i for i, t in enumerate(flat) if t.startswith("Park") and i not in excluded]
        assert len(kept) == 1
        assert kept[0] == 0  # the first page's occurrence survives

    def test_repeated_table_header_survives_on_first_page(self) -> None:
        header = "Occupation Wages fall at or below the following percentiles 10 25 50 75 90"
        pages = [
            _page(header, [f"Worker class {i}-{j} ..... 7.50 8.00 9.24" for j in range(30)])
            for i in range(3)
        ]
        excluded = _repeated_page_furniture(pages)
        flat = _flat(pages)
        survivors = [i for i, t in enumerate(flat) if t == header and i not in excluded]
        assert survivors == [0]

    def test_same_text_at_different_heights_is_not_furniture(self) -> None:
        # A heading prefix that recurs in the zone but not at one position.
        pages = []
        for i, y in enumerate((740.0, 715.0, 690.0)):
            pages.append(
                _page("How are hemorrhoids", [f"text {i} {j}" for j in range(30)], header_y=y)
            )
        # Pad the extent so all three headings stay inside the top zone.
        for page in pages:
            page.append(_r("top rule", 760.0))
        excluded = _repeated_page_furniture(pages)
        flat = _flat(pages)
        assert not [i for i, t in enumerate(flat) if t == "How are hemorrhoids" and i in excluded]

    def test_two_of_five_pages_meets_min_occurrence(self) -> None:
        pages = [_page(f"Unique header {i}", [f"b {i} {j}" for j in range(30)]) for i in range(5)]
        pages[0][0] = _r("Shared header", 740.0)
        pages[1][0] = _r("Shared header", 740.0)
        # 2 of 5 pages = 40%, min_pages = max(2, int(5 * 0.4)) = 2 -> excluded on page 2 only.
        excluded = _repeated_page_furniture(pages)
        flat = _flat(pages)
        hits = [i for i, t in enumerate(flat) if t == "Shared header"]
        assert hits[0] not in excluded
        assert hits[1] in excluded

    def test_numeric_row_labels_are_not_furniture(self) -> None:
        pages = [
            _page("1-2", [f"Participants {i}-{j} ..... 1,404" for j in range(30)]) for i in range(3)
        ]
        excluded = _repeated_page_furniture(pages)
        flat = _flat(pages)
        assert not [i for i, t in enumerate(flat) if t == "1-2" and i in excluded]

    def test_single_page_returns_nothing_via_filter(self) -> None:
        pages = [_page("Header", ["body"])]
        assert _filter_headers_footers(pages) == set()


class TestIsWatermark:
    def test_gpo_slugs(self) -> None:
        assert _is_watermark(
            "VerDate Mar<15>2010 19:36 Feb 07, 2011 Jkt 223001 PO 00000 Frm 00001 "
            "Fmt 4717 Sfmt 4717"
        )
        assert _is_watermark("emcdonald on DSK2BSOYB1PROD with NOTICES3")
        assert _is_watermark("BILLING CODE 4810-33-P")
        assert _is_watermark("E:\\FR\\FM\\08FEN3.SGM 08FEN3")

    def test_ordinary_words_are_not_slugs(self) -> None:
        assert not _is_watermark(
            "Landscaping and groundskeeping workers ............ 7.50 8.00 9.24"
        )
        assert not _is_watermark("Grounds maintenance: desktop publishers and dskilled trades")
        assert not _is_watermark("The format of the billing code section is described below.")
        assert not _is_watermark("Use the fmt module to format output.")
        assert not _is_watermark(
            "Agencies may prod with reminders, but the statute does not require "
            "any response within 30 days."
        )

    def test_filter_drops_slug_and_keeps_data_row(self) -> None:
        pages = [
            [
                _r("Landscaping and groundskeeping workers ..... 7.50", 700.0),
                _r("ctelli on DSK5SPH9LS1PROD with REPORTS", 40.0),
            ],
            [_r("Grounds maintenance workers ..... 8.00", 700.0), _r("Body text", 400.0)],
        ]
        excluded = _filter_headers_footers(pages)
        assert 0 not in excluded
        assert 1 in excluded

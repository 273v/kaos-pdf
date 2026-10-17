"""Tests for PDFium end-of-line hyphen markers, leader glyphs, and control characters."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import pytest
from kaos_content import serialize_text

from kaos_pdf._text_markers import (
    clean_line_text,
    clean_page_text,
    hyphen_join_decision,
    join_hyphenated_lines,
)
from kaos_pdf.extract import extract_page_text, extract_pdf

Rect = tuple[str, tuple[float, float, float, float], dict[str, Any] | None]

# PDF-space boxes (left, bottom, right, top) for two consecutive lines.
_LINE1 = (72.0, 700.0, 540.0, 712.0)
_LINE2 = (72.0, 686.0, 300.0, 698.0)


def _lines(*texts: str) -> list[Rect]:
    rects: list[Rect] = []
    for i, text in enumerate(texts):
        top = 712.0 - 14.0 * i
        rects.append((text, (72.0, top - 12.0, 540.0, top), {"font_size": 10.0}))
    return rects


def _inline_text(node: Any) -> str:
    """Concatenate the text of a block's inline content (recursing into styling)."""
    value = getattr(node, "value", None)
    if isinstance(value, str):
        return value
    return "".join(_inline_text(child) for child in getattr(node, "children", ()) or ())


def _has_bad_control(text: str) -> bool:
    return any((ord(c) < 0x20 and c not in "\t\n\r") or c in "\ufffe\x7f" for c in text)


# ---------------------------------------------------------------------------
# Fragment-join logic (rect level)
# ---------------------------------------------------------------------------


class TestJoinHyphenatedLines:
    @pytest.mark.parametrize("marker", ["\x02", "\ufffe", "\u00ad"])
    def test_marker_then_lowercase_joins(self, marker: str) -> None:
        rects = [
            (f"the House of Repre{marker}", _LINE1, {"font_size": 10.0}),
            ("sentatives shall", _LINE2, {"font_size": 10.0}),
        ]
        out = join_hyphenated_lines(rects)
        assert len(out) == 1
        text, bbox, fi = out[0]
        assert text == "the House of Representatives shall"
        # Provenance: union of both line boxes, font info of the first line.
        assert bbox == (72.0, 686.0, 540.0, 712.0)
        assert fi == {"font_size": 10.0}

    def test_trailing_space_after_marker_still_joins(self) -> None:
        out = join_hyphenated_lines(_lines("di\x02 ", "  rectly to the CFTC"))
        assert [t for t, _, _ in out] == ["directly to the CFTC"]

    def test_marker_then_uppercase_keeps_hyphen_and_break(self) -> None:
        out = join_hyphenated_lines(_lines("an Anti\x02", "American sentiment"))
        assert [t for t, _, _ in out] == ["an Anti\x02", "American sentiment"]
        cleaned = [clean_line_text(t)[0] for t, _, _ in out]
        assert cleaned == ["an Anti-", "American sentiment"]

    def test_marker_then_digit_keeps_hyphen_and_break(self) -> None:
        out = join_hyphenated_lines(_lines("fiscal years 1990\x02", "95 data"))
        assert [clean_line_text(t)[0] for t, _, _ in out] == ["fiscal years 1990-", "95 data"]

    def test_marker_at_last_line_renders_hyphen(self) -> None:
        out = join_hyphenated_lines(_lines("end of page Repre\x02"))
        assert [clean_line_text(t)[0] for t, _, _ in out] == ["end of page Repre-"]

    def test_no_marker_unchanged(self) -> None:
        rects = _lines("an ordinary line", "and another one")
        assert join_hyphenated_lines(rects) == rects

    def test_real_hyphen_compound_at_line_end_unchanged(self) -> None:
        # A literal "-" (not PDFium's marker) is left exactly as is.
        rects = _lines("a self-", "executing treaty")
        assert join_hyphenated_lines(rects) == rects
        assert [clean_line_text(t)[0] for t, _, _ in rects] == ["a self-", "executing treaty"]

    def test_marked_compound_keeps_hyphen_but_rejoins(self) -> None:
        # PDFium marks typeset compound hyphens at a line end like soft ones.
        out = join_hyphenated_lines(_lines("his postal\x02", "service job"))
        assert [t for t, _, _ in out] == ["his postal-service job"]

    def test_hyphenated_chain(self) -> None:
        out = join_hyphenated_lines(_lines("on a day-to\x02", "day basis"))
        assert [t for t, _, _ in out] == ["on a day-to-day basis"]
        out = join_hyphenated_lines(_lines("designated self-regu\x02", "latory organization"))
        assert [t for t, _, _ in out] == ["designated self-regulatory organization"]

    def test_chained_joins(self) -> None:
        out = join_hyphenated_lines(_lines("a con\x02", "tract and an agree\x02", "ment here"))
        assert [t for t, _, _ in out] == ["a contract and an agreement here"]

    def test_ocr_not_sign_line_end(self) -> None:
        # OCR layers (Serial Set scans) write a line-end hyphen as U+00AC.
        out = join_hyphenated_lines(_lines("the present ex\u00ac", "traordinary session"))
        assert [t for t, _, _ in out] == ["the present extraordinary session"]
        # Not a continuation: the sign is left exactly as written.
        rects = _lines("if \u00ac", "P then Q")
        out = join_hyphenated_lines(rects)
        assert out == rects
        assert clean_line_text("if \u00ac")[0] == "if \u00ac"

    def test_custom_resolver(self) -> None:
        calls: list[tuple[str, str]] = []

        def resolver(left: str, right: str) -> Literal["join", "hyphen"] | None:
            calls.append((left, right))
            return "join"

        out = join_hyphenated_lines(_lines("his postal\x02", "service job"), resolver=resolver)
        assert [t for t, _, _ in out] == ["his postalservice job"]
        assert calls == [("postal", "service")]

        def never(left: str, right: str) -> Literal["join", "hyphen"] | None:
            return None

        out = join_hyphenated_lines(_lines("a con\x02", "tract"), resolver=never)
        assert [clean_line_text(t)[0] for t, _, _ in out] == ["a con-", "tract"]

    def test_leader_run_is_not_a_hyphen(self) -> None:
        rects = _lines("Introduction \x02\x02\x02\x02", "page one")
        assert join_hyphenated_lines(rects) == rects


class TestHyphenJoinDecision:
    @pytest.mark.parametrize(
        ("left", "right", "expected"),
        [
            ("Repre", "sentatives", "join"),
            ("with", "out", "join"),
            ("con", "tracts", "join"),
            ("hydro", "graphs", None),  # unsure -> keep hyphen and break
            ("equa", "of", None),  # interleaved, not a continuation
            ("postal", "service", "hyphen"),
            ("self", "executing", "hyphen"),
            ("well", "being", "hyphen"),
            ("day-to", "day", "hyphen"),
            ("self-regu", "latory", "join"),
            ("DOCU", "MENT", "join"),
            ("Anti", "American", None),
            ("ACME", "Corp", None),
        ],
    )
    def test_decisions(self, left: str, right: str, expected: str | None) -> None:
        assert hyphen_join_decision(left, right) == expected


# ---------------------------------------------------------------------------
# Control characters and leaders
# ---------------------------------------------------------------------------


class TestCleanLineText:
    def test_leader_runs_render_as_dots(self) -> None:
        text, dropped = clean_line_text("Chapter 1\x01\x01\x01\x01 7")
        assert text == "Chapter 1.... 7"
        assert dropped == 0
        text, _ = clean_line_text("Chapter 2 \x02 \x02 \x02 9")
        assert text == "Chapter 2 . . . 9"

    def test_unmapped_controls_dropped_and_counted(self) -> None:
        text, dropped = clean_line_text("a\x00b\x18c\x7fd\x01e")
        assert text == "abcde"
        assert dropped == 4

    def test_whitespace_controls_become_space(self) -> None:
        assert clean_line_text("a\x0bb\x0cc")[0] == "a b c"

    def test_tab_newline_cr_kept(self) -> None:
        assert clean_line_text("a\tb\r\nc") == ("a\tb\r\nc", 0)

    def test_soft_hyphen_inside_word_removed(self) -> None:
        assert clean_line_text("hy\u00adphen")[0] == "hyphen"

    def test_clean_text_unchanged(self) -> None:
        assert clean_line_text("plain text - with a hyphen") == ("plain text - with a hyphen", 0)


class TestCleanPageText:
    def test_inline_marker_resolved(self) -> None:
        # PDFium's page text already drops the break after the marker.
        raw = "letter agreement di\x02rectly to the CFTC\r\nhis postal\x02service job"
        text, _ = clean_page_text(raw)
        assert text == "letter agreement directly to the CFTC\r\nhis postal-service job"

    def test_join_disabled_renders_hyphen(self) -> None:
        text, _ = clean_page_text("di\x02rectly", join_hyphenated_lines=False)
        assert text == "di-rectly"


# ---------------------------------------------------------------------------
# Regression on real PDFs (public-domain fixtures)
# ---------------------------------------------------------------------------


class TestPdfRegression:
    """``test4.pdf`` (17 CFR 31) carries 1,168 PDFium U+0002 hyphen markers."""

    def test_no_control_characters_in_ast_text(self, fixtures_dir: Path) -> None:
        doc = extract_pdf(fixtures_dir / "test4.pdf")
        text = serialize_text(doc)
        assert not _has_bad_control(text)
        assert "fully executed letter agreement directly to the CFTC" in text
        assert "di\n" not in text

    def test_joined_paragraph_provenance_spans_both_lines(self, fixtures_dir: Path) -> None:
        doc = extract_pdf(fixtures_dir / "test4.pdf", pages=[0])
        hits = [b for b in doc.body if "agreement directly to the CFTC" in _inline_text(b)]
        assert hits
        prov = hits[0].provenance
        assert prov is not None and prov.bbox is not None and prov.page == 1
        # One line here is ~6pt tall; the merged box covers both lines (~14pt).
        assert prov.bbox.bottom - prov.bbox.top > 10

    def test_hyphen_resolver_option(self, fixtures_dir: Path) -> None:
        doc = extract_pdf(
            fixtures_dir / "test4.pdf", pages=[0], hyphen_resolver=lambda _l, _r: "hyphen"
        )
        text = serialize_text(doc)
        assert "agreement di-rectly to the CFTC" in text
        page = extract_page_text(
            fixtures_dir / "kl3m_court_woods.pdf", 1, hyphen_resolver=lambda _l, _r: "join"
        )
        assert "postalservice" in page

    def test_join_disabled_keeps_lines_with_visible_hyphen(self, fixtures_dir: Path) -> None:
        doc = extract_pdf(fixtures_dir / "test4.pdf", pages=[0], join_hyphenated_lines=False)
        text = serialize_text(doc)
        assert not _has_bad_control(text)
        assert "letter agreement di-" in text
        assert "agreement directly" not in text

    def test_compound_hyphen_kept(self, fixtures_dir: Path) -> None:
        text = serialize_text(extract_pdf(fixtures_dir / "kl3m_court_woods.pdf"))
        assert not _has_bad_control(text)
        assert "postal-service" in text

    @pytest.mark.parametrize("name", ["test4.pdf", "usgs_sir.pdf", "test5.pdf", "test2.pdf"])
    def test_fixtures_free_of_controls(self, fixtures_dir: Path, name: str) -> None:
        doc = extract_pdf(fixtures_dir / name, merge_column_paragraphs=True)
        assert not _has_bad_control(serialize_text(doc))

    def test_extract_page_text_clean(self, fixtures_dir: Path) -> None:
        text = extract_page_text(fixtures_dir / "test4.pdf", 0)
        assert not _has_bad_control(text)
        assert "agreement directly to the CFTC" in text
        raw = extract_page_text(fixtures_dir / "test4.pdf", 0, join_hyphenated_lines=False)
        assert not _has_bad_control(raw)
        assert "agreement di-rectly" in raw

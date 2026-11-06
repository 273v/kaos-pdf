"""Letters a PDF's own font table leaves out: "addicƟve" reads as "addictive"."""

from __future__ import annotations

import zlib
from pathlib import Path

import pytest
from kaos_content import serialize_text

from kaos_pdf import extract_page_text, parse_pdf, parse_pdf_bytes
from kaos_pdf._tounicode import multi_letter_maps

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NY = FIXTURES / "ny_ag_ligature_page.pdf"
COLORADO = FIXTURES / "co_session_law_overlap_page.pdf"


def _stream(body: bytes, *, flate: bool = True) -> bytes:
    data = zlib.compress(body) if flate else body
    head = b"/Filter /FlateDecode " if flate else b""
    return b"<< " + head + b"/Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def _cmap(*entries: str) -> bytes:
    return (
        b"begincmap %d beginbfchar\n" % len(entries)
        + "\n".join(entries).encode()
        + b"\nendbfchar endcmap"
    )


def _file(*objects: tuple[int, bytes]) -> bytes:
    return b"%PDF-1.6\n" + b"".join(b"%d 0 obj\n" % n + body + b"\nendobj\n" for n, body in objects)


def _font(name: bytes, cmap: int | None) -> bytes:
    uni = b" /ToUnicode %d 0 R" % cmap if cmap else b""
    return b"<< /Type /Font /Subtype /Type0 /BaseFont /" + name + uni + b" >>"


def test_a_subset_missing_a_code_takes_it_from_its_typefaces_sibling() -> None:
    data = _file(
        (1, _font(b"AAAAAA+Serifa", 2)),
        (2, _stream(_cmap("<0041> <0061>"))),
        (3, _font(b"BBBBBB+Serifa-Bold", 4)),
        (4, _stream(_cmap("<0200> <00740069>"))),
    )
    assert multi_letter_maps(data) == {"AAAAAA+Serifa": {0x200: "ti"}}


def test_a_code_a_font_maps_itself_is_never_overridden() -> None:
    data = _file(
        (1, _font(b"AAAAAA+Serifa", 2)),
        (2, _stream(_cmap("<0200> <0078>"))),
        (3, _font(b"BBBBBB+Serifa", 4)),
        (4, _stream(_cmap("<0200> <00740069>"))),
    )
    assert "AAAAAA+Serifa" not in multi_letter_maps(data)


def test_another_typeface_says_nothing_and_siblings_that_disagree_arent_trusted() -> None:
    other = _file(
        (1, _font(b"AAAAAA+Serifa", None)),
        (2, _font(b"BBBBBB+Gothic", 3)),
        (3, _stream(_cmap("<0200> <00740069>"))),
    )
    assert multi_letter_maps(other) == {}
    split = _file(
        (1, _font(b"AAAAAA+Serifa", None)),
        (2, _font(b"BBBBBB+Serifa", 3)),
        (3, _stream(_cmap("<0200> <00740069>"))),
        (4, _font(b"CCCCCC+Serifa", 5)),
        (5, _stream(_cmap("<0200> <00660069>"))),
    )
    assert "AAAAAA+Serifa" not in multi_letter_maps(split)


def test_calibri_with_no_sibling_uses_its_own_ligature_glyphs_and_no_one_elses_font_does() -> None:
    calibri = _file((1, _font(b"AAAAAA+Calibri-Italic", 2)), (2, _stream(_cmap("<012b> <FB00>"))))
    assert multi_letter_maps(calibri)["AAAAAA+Calibri-Italic"][0x19F] == "ti"
    assert multi_letter_maps(_file((1, _font(b"AAAAAA+Serifa", None)))) == {}


def test_ranges_plain_streams_and_object_streams_are_read() -> None:
    cmap = (
        b"begincmap 2 beginbfrange\n<0200> <0201> <00740069>\n"
        b"<0300> <0300> [<00660069>]\nendbfrange"
    )
    packed = b"10 0 " + _font(b"BBBBBB+Serifa", 2)
    first = packed.index(b"<<")
    body = b"10 0 " + packed[first:]
    objstm = b"<< /Type /ObjStm /N 1 /First 5 /Filter /FlateDecode >>\nstream\n"
    data = _file(
        (1, _font(b"AAAAAA+Serifa", None)),
        (2, _stream(cmap, flate=False)),
        (3, objstm + zlib.compress(body) + b"\nendstream"),
    )
    assert multi_letter_maps(data)["AAAAAA+Serifa"] == {0x200: "ti", 0x201: "tj", 0x300: "fi"}


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not a pdf",
        b"%PDF-1.6\n1 0 obj\n<< /ToUnicode 2 0 R /BaseFont /A /Subtype /Type0 >>\nendobj\n",
        b"%PDF-1.6\n1 0 obj\n<< /Filter /FlateDecode >>\nstream\nnot flate\nendstream\nendobj\n",
        b"%PDF-1.6\n1 0 obj\n<< /Type /Font /Subtype /Type0 /BaseFont /A+Calibri >>\n"
        b"endobj\ntrailer << /Encrypt 9 0 R >>\n",
    ],
)
def test_a_file_it_cant_read_yields_nothing_and_never_raises(data: bytes) -> None:
    assert multi_letter_maps(data) == {}


def test_a_real_page_reads_its_ligatures_through_every_entry_point() -> None:
    expected = ("Addictive", "Exploitation")
    from_path = serialize_text(parse_pdf(NY))
    from_bytes = serialize_text(parse_pdf_bytes(NY.read_bytes()))
    page = extract_page_text(NY, 0)
    for text in (from_path, from_bytes, page):
        assert all(word in text for word in expected)
        assert not {"Ɵ", "ƞ", "Ʃ"} & set(text)


def test_a_page_with_no_such_codes_is_unchanged_and_the_overlap_stays_fixed() -> None:
    text = serialize_text(parse_pdf(COLORADO))
    assert "Be it enacted by the General Assembly of the State of Colorado" in text
    assert "ththe" not in text
    assert "StStat" not in text

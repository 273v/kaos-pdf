"""Full-width provisions retain visual order when narrow labels create columns."""

from pathlib import Path

import pytest
from kaos_content import serialize_text
from reportlab.pdfgen import canvas

from kaos_pdf import parse_pdf


@pytest.mark.parametrize("combine_fragments", [True, False])
def test_full_width_provisions_keep_top_to_bottom_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, combine_fragments: bool
) -> None:
    path = tmp_path / "provisions.pdf"
    pdf = canvas.Canvas(str(path), pagesize=(612, 792))
    pdf.setFont("Helvetica", 12)
    pdf.drawString(72, 720, "Section Alpha. First provision with full width text across the page.")
    pdf.drawString(72, 650, "Section Beta. Second provision with full width text across the page.")
    pdf.drawString(72, 580, "Section Gamma. Third provision with full width text across the page.")
    pdf.drawString(72, 540, "(a)")
    pdf.drawString(400, 540, "(b)")
    pdf.save()
    # Isolate interleaving once column detection has selected two columns.
    monkeypatch.setattr(
        "kaos_pdf.extract._detect_char_level_columns",
        lambda *args, **kwargs: [(72.0, 300.0), (320.0, 550.0)],
    )
    text = serialize_text(
        parse_pdf(
            path,
            extract_tables=False,
            detect_headings=False,
            combine_fragments=combine_fragments,
        )
    )
    assert text.index("Section Alpha") < text.index("Section Beta") < text.index("Section Gamma")
    assert text.index("Section Gamma") < text.index("(a)")


def test_split_word_stays_on_its_full_width_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "split-word.pdf"
    pdf = canvas.Canvas(str(path), pagesize=(612, 792))
    pdf.setFont("Helvetica", 12)
    prefix = "A business must maintain reasonable security practices in maintaining these re"
    pdf.drawString(72, 720, prefix)
    pdf.drawString(72 + pdf.stringWidth(prefix, "Helvetica", 12) + 1, 720, "cords.")
    pdf.drawString(72, 650, "The following provision is a separate full width line on this page.")
    pdf.drawString(72, 540, "(a)")
    pdf.drawString(400, 540, "(b)")
    pdf.save()
    monkeypatch.setattr(
        "kaos_pdf.extract._detect_char_level_columns",
        lambda *args, **kwargs: [(72.0, 300.0), (320.0, 600.0)],
    )
    text = serialize_text(parse_pdf(path, extract_tables=False, detect_headings=False))
    assert "maintaining these records." in text
    assert text.index("records.") < text.index("following provision")


def test_separate_columns_are_not_joined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "columns.pdf"
    pdf = canvas.Canvas(str(path), pagesize=(612, 792))
    pdf.setFont("Helvetica", 12)
    pdf.drawString(72, 720, "Left first paragraph")
    pdf.drawString(72, 650, "Left second paragraph")
    pdf.drawString(400, 720, "Right first paragraph")
    pdf.drawString(400, 650, "Right second paragraph")
    pdf.save()
    monkeypatch.setattr(
        "kaos_pdf.extract._detect_char_level_columns",
        lambda *args, **kwargs: [(72.0, 300.0), (320.0, 600.0)],
    )
    text = serialize_text(parse_pdf(path, extract_tables=False, detect_headings=False))
    assert text.index("Left first") < text.index("Left second") < text.index("Right first")
    assert text.index("Right first") < text.index("Right second")


def test_multiple_short_fragments_form_one_full_width_provision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "fragmented-line.pdf"
    pdf = canvas.Canvas(str(path), pagesize=(612, 792))
    pdf.setFont("Helvetica", 12)
    left = "Information maintained for record-"
    right = "keeping shall not be used for another purpose."
    pdf.drawString(72, 720, left)
    pdf.drawString(72 + pdf.stringWidth(left, "Helvetica", 12) + 1, 720, right)
    pdf.drawString(
        72, 650, "The next provision has a full width line across the page for comparison."
    )
    pdf.drawString(72, 540, "(a)")
    pdf.drawString(400, 540, "(b)")
    pdf.save()
    monkeypatch.setattr(
        "kaos_pdf.extract._detect_char_level_columns",
        lambda *args, **kwargs: [(72.0, 300.0), (320.0, 600.0)],
    )
    text = serialize_text(parse_pdf(path, extract_tables=False, detect_headings=False))
    assert (
        "Information maintained for record-keeping shall not be used for another purpose." in text
    )


def test_low_punctuation_fragment_stays_with_its_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "punctuation.pdf"
    pdf = canvas.Canvas(str(path), pagesize=(612, 792))
    pdf.setFont("Helvetica", 12)
    prefix = "A business that knows it collects personal information from consumers"
    pdf.drawString(72, 720, prefix)
    pdf.drawString(72 + pdf.stringWidth(prefix, "Helvetica", 12) + 1, 720, ",")
    pdf.drawString(
        72, 650, "The following provision remains on its own visual line across the page."
    )
    pdf.drawString(72, 540, "(a)")
    pdf.drawString(400, 540, "(b)")
    pdf.save()
    monkeypatch.setattr(
        "kaos_pdf.extract._detect_char_level_columns",
        lambda *args, **kwargs: [(72.0, 300.0), (320.0, 600.0)],
    )
    text = serialize_text(parse_pdf(path, extract_tables=False, detect_headings=False))
    assert "information from consumers," in text


def test_overlapping_short_word_requires_normal_line_tolerance() -> None:
    from kaos_pdf.extract import _shares_visual_line

    body = (72.0, 717.0, 400.0, 728.0)
    lower_fragment = (401.0, 715.0, 405.0, 720.0)
    assert not _shares_visual_line(lower_fragment, body, 2.0)
    assert _shares_visual_line(lower_fragment, body, 2.0, punctuation=True)

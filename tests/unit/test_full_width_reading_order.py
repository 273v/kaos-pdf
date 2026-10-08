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

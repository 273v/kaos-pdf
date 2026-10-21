"""Text-block provenance boxes are in top-left coordinates, as their CoordOrigin label says."""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pypdfium2 as pdfium

from kaos_pdf import parse_pdf


def _boxes(path: Path) -> list[tuple[float, float, float, float]]:
    doc = parse_pdf(str(path), pages=[0])
    out = []
    for node in doc.body:
        pv = node.provenance
        if pv is None or pv.bbox is None or pv.extractor != "kaos-pdf/pypdfium2":
            continue
        out.append((pv.bbox.left, pv.bbox.top, pv.bbox.right, pv.bbox.bottom))
    return out


def test_text_boxes_are_top_left(text_pdf: Path) -> None:
    boxes = _boxes(text_pdf)
    if len(boxes) < 3:
        return
    page_h = pdfium.PdfDocument(str(text_pdf))[0].get_height()
    for _l, top, _r, bottom in boxes:
        assert 0.0 <= top <= bottom <= page_h + 1.0
    # Reading order runs down the page, so in top-left coordinates most steps
    # from one text block to the next go down (larger y). A running footer
    # emitted first (as in test4.pdf) is one step against the flow.
    steps = sorted(b[1] - a[1] for a, b in pairwise(boxes))
    assert steps[len(steps) // 2] >= 0

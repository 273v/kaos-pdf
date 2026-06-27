"""Tests for the RapidOCR (ONNX) engine.

The mapping from RapidOCR's output to :class:`OCRResult` is exercised via the
pure ``_output_to_result`` / ``_box_to_bbox`` helpers, so these unit tests run
without the ``[onnx]`` extra installed. The missing-extra error and a real
end-to-end OCR pass are covered separately (the latter gated on rapidocr being
importable).
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from typing import Any

import pytest

from kaos_pdf.ocr import RapidOcrEngine, RapidOcrNotInstalledError
from kaos_pdf.ocr.rapidocr import _box_to_bbox, _output_to_result


@dataclass
class _FakeOutput:
    """Stand-in for ``rapidocr.RapidOCROutput`` (only the fields we read)."""

    boxes: Any = None
    txts: Any = None
    scores: Any = None


_BOX = [[10.0, 20.0], [110.0, 20.0], [110.0, 50.0], [10.0, 50.0]]


class TestBoxToBbox:
    def test_polygon_to_axis_aligned(self) -> None:
        bbox = _box_to_bbox(_BOX)
        assert bbox is not None
        assert (bbox.left, bbox.top, bbox.right, bbox.bottom) == (10.0, 20.0, 110.0, 50.0)

    def test_bad_input_returns_none(self) -> None:
        assert _box_to_bbox(None) is None
        assert _box_to_bbox([]) is None
        assert _box_to_bbox(["nope"]) is None


class TestOutputToResult:
    def test_maps_lines_boxes_scores(self) -> None:
        out = _FakeOutput(
            boxes=[_BOX, _BOX],
            txts=["In the United States", "Court of Federal Claims"],
            scores=[0.98, 0.91],
        )
        result = _output_to_result(out, "rapidocr")
        assert result.engine_name == "rapidocr"
        assert [line.text for line in result.lines] == [
            "In the United States",
            "Court of Federal Claims",
        ]
        assert result.lines[0].confidence == pytest.approx(0.98)
        assert result.lines[0].bbox is not None
        assert result.lines[0].bbox.right == 110.0

    def test_empty_output_yields_empty_result(self) -> None:
        assert _output_to_result(_FakeOutput(), "rapidocr").lines == []
        assert _output_to_result(_FakeOutput(txts=[]), "rapidocr").lines == []

    def test_missing_boxes_and_scores_default(self) -> None:
        out = _FakeOutput(txts=["text only"])
        result = _output_to_result(out, "rapidocr")
        assert result.lines[0].bbox is None
        assert result.lines[0].confidence == 1.0

    def test_scores_clamped_and_bad_score_defaults(self) -> None:
        out = _FakeOutput(txts=["a", "b", "c"], scores=[1.5, -0.2, "x"])
        result = _output_to_result(out, "rapidocr")
        assert result.lines[0].confidence == 1.0  # clamped from 1.5
        assert result.lines[1].confidence == 0.0  # clamped from -0.2
        assert result.lines[2].confidence == 1.0  # non-numeric → default


class TestMissingExtra:
    def test_construct_without_rapidocr_raises_install_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_import = builtins.__import__

        def shim(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "rapidocr" or name.startswith("rapidocr."):
                raise ImportError("pretend rapidocr is missing")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", shim)
        with pytest.raises(RapidOcrNotInstalledError) as exc_info:
            RapidOcrEngine()
        assert "kaos-pdf[onnx]" in str(exc_info.value)

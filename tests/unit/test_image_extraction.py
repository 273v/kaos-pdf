"""Tests for extract_pdf(..., extract_images=True) — embedded PDF image extraction."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from kaos_content.model.blocks import Figure
from kaos_content.model.inlines import Image
from kaos_content.traversal import find_by_type

from kaos_pdf import extract_pdf
from kaos_pdf.extract import _image_data_uri

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _fixture_with_embedded_images() -> Path | None:
    """Return the first kaos-pdf fixture containing at least one embedded image."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    from kaos_pdf.extract import _PDFIUM_LOCK

    for p in sorted(FIXTURES.rglob("*.pdf")):
        with _PDFIUM_LOCK:
            try:
                doc = pdfium.PdfDocument(str(p))
            except Exception:
                continue
            try:
                for i in range(len(doc)):
                    page = doc[i]
                    imgs = list(page.get_objects(filter=(pdfium_c.FPDF_PAGEOBJ_IMAGE,)))
                    if imgs:
                        return p
            finally:
                doc.close()
    return None


class TestImageDataUri:
    def test_jpeg_data_uri(self) -> None:
        data = b"\xff\xd8\xff" + b"\x00" * 10
        uri = _image_data_uri(data, "jpeg")
        assert uri.startswith("data:image/jpeg;base64,")
        encoded = uri.split(",", 1)[1]
        assert base64.b64decode(encoded) == data

    def test_png_data_uri(self) -> None:
        data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10
        uri = _image_data_uri(data, "png")
        assert uri.startswith("data:image/png;base64,")

    def test_roundtrip(self) -> None:
        raw = b"hello world" * 100
        uri = _image_data_uri(raw, "png")
        encoded = uri.split(",", 1)[1]
        assert base64.b64decode(encoded) == raw


class TestExtractImagesOff:
    """With ``extract_images=False`` (default), no Image / Figure nodes are emitted."""

    def test_default_no_images(self) -> None:
        fixture = _fixture_with_embedded_images()
        if fixture is None:
            pytest.skip("no fixture with embedded images")
        doc = extract_pdf(fixture)
        assert list(find_by_type(doc, Image)) == []

    def test_explicit_false_no_images(self) -> None:
        fixture = _fixture_with_embedded_images()
        if fixture is None:
            pytest.skip("no fixture with embedded images")
        doc = extract_pdf(fixture, extract_images=False)
        assert list(find_by_type(doc, Image)) == []


class TestExtractImagesOn:
    """With ``extract_images=True``, Figure/Image pairs are emitted."""

    def test_emits_figures(self) -> None:
        fixture = _fixture_with_embedded_images()
        if fixture is None:
            pytest.skip("no fixture with embedded images")
        doc = extract_pdf(fixture, extract_images=True)
        figures = list(find_by_type(doc, Figure))
        images = list(find_by_type(doc, Image))
        assert figures, f"expected Figure blocks in {fixture.name}"
        assert len(images) == len(figures)

    def test_image_src_is_data_uri(self) -> None:
        fixture = _fixture_with_embedded_images()
        if fixture is None:
            pytest.skip("no fixture with embedded images")
        doc = extract_pdf(fixture, extract_images=True)
        for img in find_by_type(doc, Image):
            assert img.src.startswith("data:image/"), f"unexpected src: {img.src[:60]}"
            mime, _, b64 = img.src[len("data:image/") :].partition(";base64,")
            assert mime in {"jpeg", "png", "jp2"}
            # base64 payload is valid + non-empty
            decoded = base64.b64decode(b64)
            assert len(decoded) > 0

    def test_image_dimensions_populated(self) -> None:
        fixture = _fixture_with_embedded_images()
        if fixture is None:
            pytest.skip("no fixture with embedded images")
        doc = extract_pdf(fixture, extract_images=True)
        for img in find_by_type(doc, Image):
            assert img.width is not None and img.width > 0
            assert img.height is not None and img.height > 0

    def test_image_has_alt_text(self) -> None:
        fixture = _fixture_with_embedded_images()
        if fixture is None:
            pytest.skip("no fixture with embedded images")
        doc = extract_pdf(fixture, extract_images=True)
        for img in find_by_type(doc, Image):
            assert img.alt, "expected non-empty alt describing page + index"
            assert "PDF page" in img.alt

    def test_figure_carries_page_and_bbox_provenance(self) -> None:
        fixture = _fixture_with_embedded_images()
        if fixture is None:
            pytest.skip("no fixture with embedded images")
        doc = extract_pdf(fixture, extract_images=True)
        figures = list(find_by_type(doc, Figure))
        assert figures
        for fig in figures:
            prov = fig.provenance
            assert prov is not None
            assert prov.page is not None and prov.page >= 1
            assert prov.bbox is not None
            assert prov.extractor == "kaos-pdf/pypdfium2"


class TestImageSrcBuilder:
    """``image_src_builder`` lets callers inject their own URI policy.

    The parameter exists so kaos-pdf doesn't bake any storage decision
    into its output. Callers who want artifact URIs, ``file://`` URIs
    after writing to disk, or bare logical URIs paired with an
    out-of-band byte store pass their own builder. The default
    (``_image_data_uri``) keeps documents self-contained.
    """

    def test_default_builder_emits_data_uris(self) -> None:
        fixture = _fixture_with_embedded_images()
        if fixture is None:
            pytest.skip("no fixture with embedded images")
        doc = extract_pdf(fixture, extract_images=True)
        img = next(iter(find_by_type(doc, Image)), None)
        assert img is not None
        assert img.src.startswith("data:image/")

    def test_custom_builder_overrides_src(self) -> None:
        fixture = _fixture_with_embedded_images()
        if fixture is None:
            pytest.skip("no fixture with embedded images")

        def builder(data: bytes, fmt: str, page: int, index: int) -> str:
            return f"pdf://fixture/page-{page}/image-{index}.{fmt}"

        doc = extract_pdf(fixture, extract_images=True, image_src_builder=builder)
        images = list(find_by_type(doc, Image))
        assert images
        for img in images:
            assert img.src.startswith("pdf://fixture/page-")
            # URI carries page + index + format
            assert "/image-" in img.src
            assert img.src.endswith((".jpeg", ".png", ".jp2"))

    def test_builder_receives_expected_arguments(self) -> None:
        """Builder is called once per image with
        (data, fmt, page, index) matching the Figure's provenance.
        """
        fixture = _fixture_with_embedded_images()
        if fixture is None:
            pytest.skip("no fixture with embedded images")

        calls: list[tuple[int, str, int, int]] = []  # (len(data), fmt, page, index)

        def builder(data: bytes, fmt: str, page: int, index: int) -> str:
            calls.append((len(data), fmt, page, index))
            return f"pdf://test/{page}-{index}.{fmt}"

        doc = extract_pdf(fixture, extract_images=True, image_src_builder=builder)
        images = list(find_by_type(doc, Image))
        assert len(calls) == len(images)
        # Every call has non-empty data, a known format, a positive page,
        # and a positive index (1-based).
        for data_len, fmt, page, index in calls:
            assert data_len > 0
            assert fmt in {"jpeg", "png", "jp2"}
            assert page >= 1
            assert index >= 1

    def test_builder_not_called_when_images_off(self) -> None:
        fixture = _fixture_with_embedded_images()
        if fixture is None:
            pytest.skip("no fixture with embedded images")

        def builder(data: bytes, fmt: str, page: int, index: int) -> str:
            raise AssertionError("builder must not be called when extract_images=False")

        # extract_images=False (default) bypasses image enumeration
        # entirely, so the builder is never invoked.
        extract_pdf(fixture, extract_images=False, image_src_builder=builder)

    def test_builder_enables_byte_collection_side_channel(self) -> None:
        """The common use case for a custom builder: collect bytes in a
        dict keyed by the returned URI so the document stays lightweight.
        """
        fixture = _fixture_with_embedded_images()
        if fixture is None:
            pytest.skip("no fixture with embedded images")

        collected: dict[str, bytes] = {}

        def builder(data: bytes, fmt: str, page: int, index: int) -> str:
            uri = f"pdf://bytes/{page}-{index}.{fmt}"
            collected[uri] = data
            return uri

        doc = extract_pdf(fixture, extract_images=True, image_src_builder=builder)
        images = list(find_by_type(doc, Image))
        assert collected
        assert len(collected) == len(images)
        # Every Image.src is a URI we collected bytes for
        for img in images:
            assert img.src in collected
            assert len(collected[img.src]) > 0

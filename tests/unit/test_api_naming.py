"""Tests for the canonical ``parse_*`` API naming + deprecation wrappers.

Per ``docs/guides/python-api-naming.md``, ``parse_pdf`` / ``parse_pdf_bytes``
are the canonical names; ``extract_pdf`` / ``extract_pdf_bytes`` are kept as
deprecation wrappers so existing call sites keep working with a warning.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from kaos_content.model.document import ContentDocument

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


def _pick_fixture() -> Path:
    """Return any small PDF fixture in tests/fixtures/."""
    candidates = sorted(_FIXTURE_DIR.glob("**/*.pdf"))
    if not candidates:
        pytest.skip("No PDF fixture available under tests/fixtures/")
    # Prefer the smallest one to keep the test cheap.
    candidates.sort(key=lambda p: p.stat().st_size)
    return candidates[0]


def test_parse_pdf_is_importable_from_top_level() -> None:
    """``from kaos_pdf import parse_pdf`` resolves to the canonical function."""
    from kaos_pdf import parse_pdf

    assert callable(parse_pdf)


def test_parse_pdf_bytes_is_importable_from_top_level() -> None:
    from kaos_pdf import parse_pdf_bytes

    assert callable(parse_pdf_bytes)


def test_parse_pdf_returns_content_document() -> None:
    from kaos_pdf import parse_pdf

    fixture = _pick_fixture()
    doc = parse_pdf(fixture)
    assert isinstance(doc, ContentDocument)


def test_extract_pdf_emits_deprecation_warning() -> None:
    """The legacy ``extract_pdf`` name still works but warns."""
    from kaos_pdf import extract_pdf

    fixture = _pick_fixture()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        doc = extract_pdf(fixture)
    assert isinstance(doc, ContentDocument)
    dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert dep_warnings, "expected a DeprecationWarning from extract_pdf()"
    assert "parse_pdf" in str(dep_warnings[0].message)


def test_extract_pdf_bytes_emits_deprecation_warning() -> None:
    from kaos_pdf import extract_pdf_bytes

    fixture = _pick_fixture()
    data = fixture.read_bytes()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        doc = extract_pdf_bytes(data)
    assert isinstance(doc, ContentDocument)
    dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert dep_warnings, "expected a DeprecationWarning from extract_pdf_bytes()"
    assert "parse_pdf_bytes" in str(dep_warnings[0].message)


def test_parse_pdf_does_not_warn() -> None:
    """The canonical name must not emit a DeprecationWarning."""
    from kaos_pdf import parse_pdf

    fixture = _pick_fixture()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        parse_pdf(fixture)
    dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert not dep_warnings, f"unexpected DeprecationWarning from parse_pdf(): {dep_warnings}"

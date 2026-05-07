"""Live FUND-4 integration tests — real pdfplumber on real fixtures.

Runs only when pdfplumber is importable. Asserts:

1. The default engine finds at least one table on the
   ``kl3m_court_burns.pdf`` fixture (confirmed in smoke testing).
2. ``extract_pdf_with_tables`` returns a ``TabularDocument`` with
   at least one :class:`Table` whose columns carry
   :class:`~kaos_content.model.tabular.ColumnType` metadata.
3. Engine-mode swap produces ``Table`` blocks with the correct
   provenance extractor string.

These are the acceptance gate for FUND-4 per CLAUDE.md's testing
policy — unit-only runs are partial evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kaos_content.model.blocks import Table as TableBlock

from kaos_pdf import extract_pdf, extract_pdf_with_tables

try:
    import pdfplumber  # type: ignore[import-not-found]  # noqa: F401

    _HAS_PDFPLUMBER = True
except ImportError:
    _HAS_PDFPLUMBER = False


_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


@pytest.mark.integration
def test_live_pdfplumber_finds_tables() -> None:
    """pdfplumber on a legal fixture finds ≥1 table."""
    if not _HAS_PDFPLUMBER:
        pytest.skip("pdfplumber not installed")
    fixture = _FIXTURE_DIR / "kl3m_court_burns.pdf"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")

    _doc, tab = extract_pdf_with_tables(str(fixture))
    # Smoke-verified that this fixture has multiple table regions.
    assert len(tab.tables) >= 1, (
        f"Expected ≥1 table on {fixture.name}; got {len(tab.tables)}. "
        "If pdfplumber's detector improved, tighten this bound."
    )

    # Every Table has a name and columns.
    for t in tab.tables:
        assert t.name.startswith("page")
        assert len(t.columns) >= 1
        # Every column has a ColumnType set (even if TEXT).
        assert all(c.column_type is not None for c in t.columns)


@pytest.mark.integration
def test_live_engine_mode_emits_table_blocks() -> None:
    """tables='engine' populates ContentDocument with Table blocks."""
    if not _HAS_PDFPLUMBER:
        pytest.skip("pdfplumber not installed")
    fixture = _FIXTURE_DIR / "kl3m_court_burns.pdf"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")

    doc = extract_pdf(str(fixture), tables="engine")
    table_blocks = [b for b in doc.body if isinstance(b, TableBlock)]
    assert table_blocks, "Expected at least one Table block in engine mode"

    # Provenance tags the block with the engine name.
    first = table_blocks[0]
    assert first.provenance is not None
    assert first.provenance.extractor == "kaos-pdf/tables/pdfplumber"


@pytest.mark.integration
def test_live_geometric_default_unchanged() -> None:
    """Regression guard: the legacy default still runs without the engine."""
    if not _HAS_PDFPLUMBER:
        pytest.skip("pdfplumber not installed")
    fixture = _FIXTURE_DIR / "kl3m_court_burns.pdf"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")

    # Default tables="geometric". The legacy detector may or may not
    # find tables; we only assert that extraction returns a non-None
    # document without raising.
    doc = extract_pdf(str(fixture))
    assert doc is not None
    # None of the blocks should carry the pdfplumber extractor.
    pdfplumber_blocks = [
        b
        for b in doc.body
        if b.provenance is not None
        and (b.provenance.extractor or "").startswith("kaos-pdf/tables/pdfplumber")
    ]
    assert pdfplumber_blocks == []

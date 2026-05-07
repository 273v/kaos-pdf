"""Shared fixtures for kaos-pdf tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from kaos_core import ArtifactStore, KaosRuntime, KaosSettings, VFSConfig, VirtualFileSystem
from kaos_core.types.enums import StorageBackend

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    """Path to the test fixtures directory."""
    return FIXTURES_DIR


@pytest.fixture
def runtime(tmp_path: Path) -> KaosRuntime:
    """Create a KaosRuntime with VFS for artifact storage."""
    settings = KaosSettings(
        artifact_inline_read_max_bytes=262_144,
        artifact_chunk_size_bytes=65_536,
    )
    runtime = KaosRuntime(config=settings)
    runtime.vfs = VirtualFileSystem(
        VFSConfig(default_backend=StorageBackend.DISK, disk_base_path=tmp_path / "vfs")
    )
    runtime.artifacts = ArtifactStore(
        runtime.vfs,
        manifest_context_id=settings.artifact_manifest_context_id,
        manifest_prefix=settings.artifact_manifest_prefix,
        max_inline_read_bytes=settings.artifact_inline_read_max_bytes,
        default_chunk_size=settings.artifact_chunk_size_bytes,
        temporary_ttl_seconds=settings.artifact_temporary_ttl_seconds,
    )
    return runtime


# ---------------------------------------------------------------------------
# Fixture file paths — each represents a different document type
# ---------------------------------------------------------------------------


@pytest.fixture
def federal_register_pdf(fixtures_dir: Path) -> Path:
    """Federal Register notice (1 page, text-based)."""
    return fixtures_dir / "test1.pdf"


@pytest.fixture
def public_law_pdf(fixtures_dir: Path) -> Path:
    """Public Law statute (1 page, text-based, dense)."""
    return fixtures_dir / "test2.pdf"


@pytest.fixture
def scanned_pdf(fixtures_dir: Path) -> Path:
    """Scanned/image PDF (32 pages, no extractable text)."""
    return fixtures_dir / "test3.pdf"


@pytest.fixture
def cftc_regulations_pdf(fixtures_dir: Path) -> Path:
    """CFTC regulations (34 pages, text-based, structured)."""
    return fixtures_dir / "test4.pdf"


@pytest.fixture
def scanned_20page_pdf(fixtures_dir: Path) -> Path:
    """Scanned document (20 pages, image-based)."""
    return fixtures_dir / "test5.pdf"


@pytest.fixture
def short_federal_register_pdf(fixtures_dir: Path) -> Path:
    """Short Federal Register notice (3 pages)."""
    return fixtures_dir / "test6.pdf"


@pytest.fixture
def fda_guidance_pdf(fixtures_dir: Path) -> Path:
    """FDA Federal Register guidance (2 pages)."""
    return fixtures_dir / "kl3m_fda_guidance.pdf"


@pytest.fixture
def court_filing_short_pdf(fixtures_dir: Path) -> Path:
    """TX Court of Criminal Appeals filing (8 pages)."""
    return fixtures_dir / "kl3m_court_woods.pdf"


@pytest.fixture
def court_filing_long_pdf(fixtures_dir: Path) -> Path:
    """PA Superior Court filing (31 pages)."""
    return fixtures_dir / "kl3m_court_burns.pdf"


@pytest.fixture
def city_council_pdf(fixtures_dir: Path) -> Path:
    """City council document (1 page)."""
    return fixtures_dir / "kl3m_dot_attachment.pdf"


@pytest.fixture
def bad_pdf(fixtures_dir: Path) -> Path:
    """Corrupt/invalid PDF for error handling tests."""
    return fixtures_dir / "bad_test1.pdf"


@pytest.fixture(
    params=[
        "test1.pdf",
        "test2.pdf",
        "test4.pdf",
        "test6.pdf",
        "kl3m_fda_guidance.pdf",
        "kl3m_court_woods.pdf",
        "kl3m_court_burns.pdf",
        "kl3m_dot_attachment.pdf",
    ]
)
def text_pdf(request: pytest.FixtureRequest, fixtures_dir: Path) -> Path:
    """Parametrized fixture: all text-based PDFs."""
    return fixtures_dir / request.param


@pytest.fixture(
    params=[
        "test3.pdf",
        "test5.pdf",
    ]
)
def image_pdf(request: pytest.FixtureRequest, fixtures_dir: Path) -> Path:
    """Parametrized fixture: all image/scanned PDFs."""
    return fixtures_dir / request.param


@pytest.fixture(
    params=[
        "test1.pdf",
        "test2.pdf",
        "test3.pdf",
        "test4.pdf",
        "test5.pdf",
        "test6.pdf",
        "kl3m_fda_guidance.pdf",
        "kl3m_court_woods.pdf",
        "kl3m_court_burns.pdf",
        "kl3m_dot_attachment.pdf",
    ]
)
def any_valid_pdf(request: pytest.FixtureRequest, fixtures_dir: Path) -> Path:
    """Parametrized fixture: all valid PDFs."""
    return fixtures_dir / request.param

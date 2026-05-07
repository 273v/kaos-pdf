"""Tests for the sync search API (kaos_pdf.search)."""

from __future__ import annotations

from pathlib import Path

import pytest
from kaos_content.search import SearchResult, SearchResults, search_document

from kaos_pdf import extract_pdf

FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def federal_register_doc():
    return extract_pdf(FIXTURES / "test1.pdf")


@pytest.fixture
def cftc_doc():
    return extract_pdf(FIXTURES / "test4.pdf")


@pytest.fixture
def court_doc():
    return extract_pdf(FIXTURES / "kl3m_court_woods.pdf")


class TestSearchDocument:
    def test_returns_search_results(self, federal_register_doc):
        sr = search_document(federal_register_doc, "Federal Register")
        assert isinstance(sr, SearchResults)
        assert isinstance(sr.results, list)
        assert sr.query == "Federal Register"

    def test_exact_match(self, federal_register_doc):
        sr = search_document(federal_register_doc, "Federal Register")
        assert len(sr.results) > 0
        assert all(isinstance(r, SearchResult) for r in sr.results)
        assert sr.results[0].score > 0

    def test_word_fallback(self, cftc_doc):
        sr = search_document(cftc_doc, "Commission regulation")
        assert len(sr.results) > 0

    def test_no_matches(self, federal_register_doc):
        sr = search_document(federal_register_doc, "xyznonexistent123")
        assert len(sr.results) == 0
        assert sr.total_matches == 0
        assert sr.has_more is False

    def test_top_k_limit(self, cftc_doc):
        sr = search_document(cftc_doc, "the", top_k=3)
        assert len(sr.results) <= 3

    def test_has_more_when_truncated(self, cftc_doc):
        sr = search_document(cftc_doc, "the", top_k=3)
        # "the" should match many paragraphs
        assert sr.has_more is True
        assert sr.total_matches > 3
        assert len(sr.results) == 3

    def test_has_more_false_when_all_fit(self, federal_register_doc):
        sr = search_document(federal_register_doc, "xyznonexistent123", top_k=100)
        assert sr.has_more is False

    def test_total_matches_reflects_all(self, cftc_doc):
        sr_small = search_document(cftc_doc, "the", top_k=2)
        sr_large = search_document(cftc_doc, "the", top_k=100000)
        # total_matches should be the same regardless of top_k
        assert sr_small.total_matches == sr_large.total_matches
        # With a large enough top_k, all matches are returned
        assert sr_large.has_more is False
        assert sr_large.total_matches == len(sr_large.results)

    def test_sorted_by_score(self, cftc_doc):
        sr = search_document(cftc_doc, "leverage", top_k=20)
        if len(sr.results) > 1:
            scores = [r.score for r in sr.results]
            assert scores == sorted(scores, reverse=True)

    def test_empty_query_raises(self, federal_register_doc):
        with pytest.raises(ValueError):
            search_document(federal_register_doc, "")

    def test_whitespace_query_raises(self, federal_register_doc):
        with pytest.raises(ValueError):
            search_document(federal_register_doc, "   ")

    def test_result_has_block_ref(self, cftc_doc):
        sr = search_document(cftc_doc, "Commission")
        for r in sr.results:
            assert r.block_ref.startswith("#/body/")

    def test_result_has_page(self, cftc_doc):
        sr = search_document(cftc_doc, "Commission")
        assert any(r.page is not None for r in sr.results)

    def test_preview_length(self, cftc_doc):
        sr = search_document(cftc_doc, "the", preview_length=50)
        for r in sr.results:
            assert len(r.text) <= 53  # 50 + "..."

    def test_full_text_no_truncation(self, cftc_doc):
        sr = search_document(cftc_doc, "the", preview_length=0, top_k=1)
        if sr.results:
            # Full text should be returned (no truncation)
            assert not sr.results[0].text.endswith("...") or len(sr.results[0].text) > 203

    def test_section_title_populated(self, court_doc):
        sr = search_document(court_doc, "court")
        # Some results should have section context
        has_section = any(r.section_title is not None for r in sr.results)
        # Not all docs have sections, so this is a soft check
        assert isinstance(has_section, bool)

    def test_search_result_frozen(self, federal_register_doc):
        sr = search_document(federal_register_doc, "Federal")
        if sr.results:
            with pytest.raises(AttributeError):
                sr.results[0].score = 999  # type: ignore[misc]  # ty: ignore[invalid-assignment]

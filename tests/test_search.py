"""Tests for search ranking."""

from __future__ import annotations

from pathlib import Path

import pytest

from hdhub4u.database import add_media_batch, initialize_database
from hdhub4u.search import (
    NO_TITLE,
    calculate_score,
    get_search_title,
    normalize_text,
    search_media,
    similarity,
)


def test_normalize_strips_punctuation() -> None:
    assert normalize_text(
        "Big Boss: Season 1!"
    ) == "big boss season 1"


def test_normalize_collapses_space() -> None:
    assert normalize_text("a---b") == "a b"


def test_similarity_bounds() -> None:
    assert similarity("abc", "abc") == 1.0
    assert similarity("", "") == 1.0
    assert 0.0 <= similarity("abc", "xyz") <= 1.0


class TestCalculateScore:
    def test_exact_match_wins(self) -> None:
        exact = calculate_score(
            "Big Boss",
            "Big Boss",
        )

        partial = calculate_score(
            "Big Boss",
            "Big Boss Full Series",
        )

        assert exact > partial

    def test_empty_query_scores_zero(self) -> None:
        assert calculate_score("", "Anything") == 0

    def test_no_match_scores_zero(self) -> None:
        assert calculate_score(
            "zzzz",
            "Big Boss",
        ) == 0

    def test_prefix_counts(self) -> None:
        assert calculate_score(
            "big",
            "Bigg Boss",
        ) > 0

    def test_longer_query_scores_higher(self) -> None:
        single = calculate_score(
            "boss",
            "Big Boss",
        )

        both = calculate_score(
            "big boss",
            "Big Boss",
        )

        assert both > single


class TestGetSearchTitle:
    def test_uses_stored_title(self) -> None:
        assert get_search_title(
            "Real Title",
            "https://x.test/slug",
        ) == "Real Title"

    def test_falls_back_for_placeholder(self) -> None:
        title = get_search_title(
            NO_TITLE,
            "https://x.test/big-boss-full-series/",
        )

        assert "Big Boss" in title

    def test_falls_back_for_blank(self) -> None:
        assert (
            get_search_title(
                "   ",
                "https://x.test/fir-london/",
            )
            == "Fir London"
        )

    def test_falls_back_to_url_without_slug(self) -> None:
        assert (
            get_search_title("", "https://x.test/")
            == "https://x.test/"
        )


class TestSearchMedia:
    def test_finds_seeded_titles(
        self,
        isolated_home: Path,
    ) -> None:
        initialize_database()

        add_media_batch(
            [
                (
                    "Big Boss Full Series",
                    "https://site.test/big-boss/",
                    "webseries",
                    "",
                ),
                (
                    "Batman & Robin Full Movie",
                    "https://site.test/batman-robin/",
                    "movie",
                    "",
                ),
            ]
        )

        results = search_media("boss")

        assert results
        assert "Big Boss" in results[0]["title"]
        assert results[0]["type"] == "webseries"

    def test_results_carry_score(
        self,
        isolated_home: Path,
    ) -> None:
        initialize_database()

        add_media_batch(
            [
                (
                    "Big Boss",
                    "https://site.test/a",
                    "movie",
                    "",
                ),
            ]
        )

        results = search_media("boss")

        assert results[0]["score"] > 0

    def test_respects_limit(
        self,
        isolated_home: Path,
    ) -> None:
        initialize_database()

        add_media_batch(
            [
                (
                    f"Boss Part {index}",
                    f"https://site.test/{index}",
                    "movie",
                    "",
                )
                for index in range(10)
            ]
        )

        assert len(search_media("boss", limit=3)) == 3

    def test_short_query_returns_nothing(
        self,
        isolated_home: Path,
    ) -> None:
        initialize_database()

        assert search_media("a") == []

    def test_all_tiny_words_returns_nothing(
        self,
        isolated_home: Path,
    ) -> None:
        initialize_database()

        assert search_media("a b") == []

    def test_blank_query_returns_nothing(
        self,
        isolated_home: Path,
    ) -> None:
        initialize_database()

        assert search_media("   ") == []

    def test_no_match_returns_empty(
        self,
        isolated_home: Path,
    ) -> None:
        initialize_database()

        add_media_batch(
            [
                (
                    "Big Boss",
                    "https://site.test/a",
                    "movie",
                    "",
                ),
            ]
        )

        assert search_media("zzzzzz") == []

    def test_empty_index_returns_empty(
        self,
        isolated_home: Path,
    ) -> None:
        initialize_database()

        assert search_media("boss") == []


@pytest.mark.parametrize("query", ["", "a", "  "])
def test_degenerate_queries(
    isolated_home: Path,
    query: str,
) -> None:
    initialize_database()

    assert search_media(query) == []

"""Tests for the crawl budget and category discovery."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from hdhub4u.database import (
    get_media_count,
    initialize_database,
)
from hdhub4u.errors import NetworkError
from hdhub4u.indexer import (
    CrawlBudget,
    build_index,
    extract_category_links,
    fetch_page,
    index_category,
    is_allowed_page,
    is_same_domain,
)
from hdhub4u.parser import is_category_url


class TestIsSameDomain:
    def test_same_host(self) -> None:
        assert is_same_domain(
            "https://a.test/x",
            "https://a.test/y",
        )

    def test_different_host(self) -> None:
        assert not is_same_domain(
            "https://a.test/x",
            "https://b.test/y",
        )

    def test_subdomain_is_a_different_host(self) -> None:
        assert not is_same_domain(
            "https://cdn.a.test/x",
            "https://a.test/y",
        )


class TestIsAllowedPage:
    def test_allows_category(self) -> None:
        assert is_allowed_page(
            "https://a.test/category/movies/",
            "https://a.test/",
        )

    def test_rejects_other_host(self) -> None:
        assert not is_allowed_page(
            "https://b.test/category/movies/",
            "https://a.test/",
        )

    def test_rejects_excluded_path(self) -> None:
        assert not is_allowed_page(
            "https://a.test/how-to-download/",
            "https://a.test/",
        )

    def test_rejects_root(self) -> None:
        assert not is_allowed_page(
            "https://a.test/",
            "https://a.test/",
        )


class TestExtractCategoryLinks:
    def test_finds_unique_categories(self) -> None:
        html = """
        <a href="/category/movies/">Movies</a>
        <a href="/category/movies/">Movies again</a>
        <a href="/category/series/">Series</a>
        <a href="https://b.test/category/other/">Other</a>
        """

        links = extract_category_links(
            html,
            "https://a.test/",
        )

        assert links == [
            "https://a.test/category/movies/",
            "https://a.test/category/series/",
        ]

    def test_joins_relative_paths(self) -> None:
        html = '<a href="tag/bollywood/">B</a>'

        assert extract_category_links(
            html,
            "https://a.test/section/page/2/",
        ) == [
            "https://a.test/section/page/2/tag/bollywood/"
        ]

    def test_empty_html(self) -> None:
        assert extract_category_links("", "https://a.test/") == []


class TestFetchPage:
    def test_returns_final_url_html_status(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "hdhub4u.indexer._fetch_page",
            lambda url: (
                "https://a.test/final",
                "<html></html>",
                200,
            ),
        )

        assert fetch_page("https://a.test/page") == (
            "https://a.test/final",
            "<html></html>",
            200,
        )

    def test_retries_transient_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A crawl that gives up on the first hiccup indexes almost
        nothing, so a transient failure must be retried.
        """

        calls = {"n": 0}

        def flaky(url: str):
            calls["n"] += 1

            if calls["n"] < 3:
                raise httpx.ReadTimeout("slow")

            return ("https://a.test/", "<html></html>", 200)

        monkeypatch.setattr(
            "hdhub4u.indexer._fetch_page",
            flaky,
        )

        assert fetch_page("https://a.test/")[2] == 200
        assert calls["n"] == 3


class TestBuildIndex:
    def test_crawls_and_indexes(
        self,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pages = {
            "https://a.test/": (
                "https://a.test/",
                '<a href="/category/movies/">Movies</a>',
                200,
            ),
            "https://a.test/category/movies/": (
                "https://a.test/category/movies/",
                '<a href="/some-movie-full-movie/">'
                "Some Movie</a>",
                200,
            ),
        }

        monkeypatch.setattr(
            "hdhub4u.indexer.fetch_page",
            lambda url, attempts=3, **kwargs: pages.get(
                url,
                ("", "", 0),
            ),
        )

        found = build_index(
            "https://a.test/",
            max_pages=5,
            time_budget=30,
            delay=0,
        )

        assert found == 1
        assert get_media_count() == 1

    def test_unreachable_start_url_returns_zero(
        self,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def failing(url: str, attempts: int = 3):
            raise NetworkError("no route")

        monkeypatch.setattr(
            "hdhub4u.indexer.fetch_page",
            failing,
        )

        assert build_index("https://a.test/") == 0

    def test_page_failure_does_not_stop_the_crawl(
        self,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fetch(url: str, attempts: int = 3, **kwargs):
            if url == "https://a.test/":
                return (
                    url,
                    '<a href="/category/bad/">Bad</a>'
                    '<a href="/category/good/">Good</a>',
                    200,
                )

            if url.endswith("bad/"):
                raise NetworkError("gone")

            return (
                url,
                '<a href="/ok-movie-full-movie/">OK</a>',
                200,
            )

        monkeypatch.setattr(
            "hdhub4u.indexer.fetch_page",
            fetch,
        )

        found = build_index(
            "https://a.test/",
            max_pages=5,
            time_budget=30,
            delay=0,
        )

        assert found == 1


class TestIndexCategory:
    def test_returns_counts_and_html(
        self,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        initialize_database()

        monkeypatch.setattr(
            "hdhub4u.indexer.fetch_page",
            lambda url, attempts=3, **kwargs: (
                url,
                '<a href="/a-movie-full-movie/">A</a>',
                200,
            ),
        )

        count, html, final = index_category(
            "https://a.test/category/x/"
        )

        assert count == 1
        assert html
        assert final == "https://a.test/category/x/"

    def test_failure_returns_empty_html(
        self,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def failing(url: str, attempts: int = 3):
            raise NetworkError("gone")

        monkeypatch.setattr(
            "hdhub4u.indexer.fetch_page",
            failing,
        )

        count, html, final = index_category(
            "https://a.test/category/x/"
        )

        assert count == 0
        assert html == ""


class TestCrawlBudget:
    def test_reports_elapsed(self) -> None:
        budget = CrawlBudget()

        assert budget.elapsed >= 0.0

    def test_expires_after_budget(self) -> None:
        budget = CrawlBudget(
            max_pages=10,
            seconds=0.0,
        )

        assert budget.expired()

    def test_within_budget(self) -> None:
        budget = CrawlBudget(
            max_pages=10,
            seconds=600.0,
        )

        assert not budget.expired()


def test_category_keywords_are_recognised() -> None:
    assert is_category_url("https://a.test/category/x/")
    assert is_category_url("https://a.test/genre/x/")
    assert not is_category_url("https://a.test/movie-x/")


def test_network_error_is_importable() -> None:
    assert issubclass(NetworkError, Exception)

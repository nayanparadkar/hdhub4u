"""Tests for hdhub4u.parser."""

from __future__ import annotations

import pytest

from hdhub4u.parser import (
    classify_media_content,
    extract_content_links,
    extract_download_options,
    extract_title_from_url,
    is_category_url,
    is_excluded_path,
)


class TestIsCategoryUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://x.com/category/movies/",
            "https://x.com/genre/action/",
            "https://x.com/tag/hindi/",
            "https://x.com/page/2/",
        ],
    )
    def test_matches(
        self,
        url: str,
    ) -> None:
        assert is_category_url(url)

    def test_rejects_content(self) -> None:
        assert not is_category_url(
            "https://x.com/batman-full-movie/"
        )


class TestIsExcludedPath:
    def test_known_path(self) -> None:
        assert is_excluded_path(
            "https://x.com/how-to-download/"
        )

    def test_normalizes_missing_slash(self) -> None:
        assert is_excluded_path(
            "https://x.com/how-to-download"
        )

    def test_allows_normal_content(self) -> None:
        assert not is_excluded_path(
            "https://x.com/batman/"
        )


class TestClassifyMediaContent:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Batman Full Movie", "movie"),
            ("Bigg Boss Season 20", "webseries"),
            ("F.I.R. Episode 1", "episode"),
            ("Watch Online", "watch"),
            ("Something Else", "other"),
        ],
    )
    def test_classification(
        self,
        text: str,
        expected: str,
    ) -> None:
        # A neutral URL keeps the category check from firing.
        assert (
            classify_media_content(
                text,
                "https://x.com/page-1/",
            )
            == expected
        )

    def test_excluded_path_wins(self) -> None:
        assert (
            classify_media_content(
                "Movie",
                "https://x.com/how-to-download/",
            )
            == "other"
        )


class TestExtractTitleFromUrl:
    def test_basic_slug(self) -> None:
        assert (
            extract_title_from_url(
                "https://x.com/the-dark-knight/"
            )
            == "The Dark Knight"
        )

    def test_strips_metadata(self) -> None:
        result = extract_title_from_url(
            "https://x.com/movie-2020-hindi-webrip-1080p/"
        )

        assert "webrip" not in result.lower()
        assert "1080p" not in result.lower()
        assert "hindi" not in result.lower()

    def test_adds_full_movie_suffix(self) -> None:
        assert (
            extract_title_from_url(
                "https://x.com/batman-full-movie/"
            )
            == "Batman Full Movie"
        )

    def test_empty_path(self) -> None:
        assert extract_title_from_url("https://x.com/") == ""

    def test_uses_last_segment(self) -> None:
        assert (
            extract_title_from_url(
                "https://x.com/category/movies/film-name/"
            )
            == "Film Name"
        )


class TestExtractContentLinks:
    def test_finds_media_links(
        self,
        media_html: str,
    ) -> None:
        results = extract_content_links(
            media_html,
            "https://x.com/",
        )

        urls = [r["url"] for r in results]

        assert any(
            "batman-robin" in u for u in urls
        )

    def test_skips_excluded(
        self,
        media_html: str,
    ) -> None:
        results = extract_content_links(
            media_html,
            "https://x.com/",
        )

        assert not any(
            "how-to-download" in r["url"]
            for r in results
        )

    def test_falls_back_to_image_alt(
        self,
        media_html: str,
    ) -> None:
        results = extract_content_links(
            media_html,
            "https://x.com/",
        )

        titles = [r["title"] for r in results]

        assert any("F.I.R." in t for t in titles)

    def test_deduplicates(
        self,
        media_html: str,
    ) -> None:
        results = extract_content_links(
            media_html,
            "https://x.com/",
        )

        urls = [r["url"] for r in results]

        assert len(urls) == len(set(urls))


class TestExtractDownloadOptions:
    def test_finds_external_links(
        self,
        media_html: str,
    ) -> None:
        results = extract_download_options(
            media_html,
            "https://x.com/",
        )

        assert len(results) == 2

    def test_all_have_titles(
        self,
        media_html: str,
    ) -> None:
        results = extract_download_options(
            media_html,
            "https://x.com/",
        )

        assert all(
            r["title"] for r in results
        )

    def test_skips_same_domain_links(self) -> None:
        # Options must live on an external host, so a relative
        # href (which resolves to the page's own host) is skipped.
        results = extract_download_options(
            '<a href="/dl/1080p-x264-1gb/">'
            "1080p x264 [1GB]</a>",
            "https://x.com/page/",
        )

        assert results == []

    def test_keeps_external_options(self) -> None:
        results = extract_download_options(
            '<a href="https://cdn.example.com/dl/">'
            "1080p x264 [1GB]</a>",
            "https://x.com/page/",
        )

        assert results == [
            {
                "title": "1080p x264 [1GB]",
                "url": (
                    "https://cdn.example.com/dl/"
                ),
                "type": "Download",
            }
        ]

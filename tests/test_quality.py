"""Tests for hdhub4u.quality."""

from __future__ import annotations

import pytest

from hdhub4u.quality import (
    MINIMUM_QUALITY,
    QualityOption,
    extract_resolution,
    extract_size_bytes,
    find_quality_options,
    get_quality,
    get_quality_options,
    matches_quality,
    select_smallest,
    sort_by_size,
)


class TestExtractResolution:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Movie [2160p]", 2160),
            ("Movie [1440p]", 1440),
            ("Movie 1080p", 1080),
            ("720p WEBrip", 720),
            ("480p x264", 480),
            ("360p", 360),
            ("Movie", None),
            ("", None),
        ],
    )
    def test_values(
        self,
        title: str,
        expected: int | None,
    ) -> None:
        assert extract_resolution(title) == expected

    def test_ignores_non_adjacent_digits(self) -> None:
        assert extract_resolution("12080p") is None


class TestGetQuality:
    def test_known_key(self) -> None:
        assert get_quality("1") is not None

    def test_unknown_key(self) -> None:
        assert get_quality("99") is None

    def test_back_key(self) -> None:
        assert get_quality("6") is None

    def test_strips_whitespace(self) -> None:
        assert get_quality("  2  ") is not None


class TestMatchesQuality:
    def test_best_matches_everything(self) -> None:
        best = QualityOption(
            key="1",
            label="Best available",
            value="best",
        )

        assert matches_quality("anything", best)

    def test_exact_match(self) -> None:
        option = QualityOption(
            key="2",
            label="1080p",
            value="1080p",
        )

        assert matches_quality("Movie 1080p", option)
        assert not matches_quality(
            "Movie 720p",
            option,
        )

    def test_no_resolution_fails(self) -> None:
        option = QualityOption(
            key="2",
            label="1080p",
            value="1080p",
        )

        assert not matches_quality(
            "Movie",
            option,
        )


class TestFindQualityOptions:
    def test_filters_by_quality(self) -> None:
        options = [
            {"title": "A 1080p [1GB]", "url": "a"},
            {"title": "B 720p [700MB]", "url": "b"},
            {"title": "C 1080p [2GB]", "url": "c"},
        ]

        result = find_quality_options(
            options,
            QualityOption(
                key="2",
                label="1080p",
                value="1080p",
            ),
        )

        assert len(result) == 2

    def test_best_returns_all(self) -> None:
        options = [
            {"title": "A 1080p", "url": "a"},
            {"title": "B 720p", "url": "b"},
        ]

        result = find_quality_options(
            options,
            QualityOption(
                key="1",
                label="Best",
                value="best",
            ),
        )

        assert len(result) == 2

    def test_no_match_returns_empty(self) -> None:
        options = [{"title": "A 360p", "url": "a"}]

        result = find_quality_options(
            options,
            QualityOption(
                key="2",
                label="1080p",
                value="1080p",
            ),
        )

        assert result == []


def test_quality_options_are_unique() -> None:
    options = get_quality_options()

    keys = [o.key for o in options]
    values = [o.value for o in options]

    assert len(keys) == len(set(keys))
    assert len(values) == len(set(values))


class TestExtractSize:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Movie [700MB]", 700 * 1024**2),
            ("Movie [1GB]", 1024**3),
            ("Movie [1.1GB]", int(1.1 * 1024**3)),
            ("Movie [ 480 MB ]", 480 * 1024**2),
            ("Movie 720p [2gb]", 2 * 1024**3),
            ("Movie", None),
            ("", None),
        ],
    )
    def test_values(
        self,
        title: str,
        expected: int | None,
    ) -> None:
        assert extract_size_bytes(title) == expected

    def test_requires_brackets(self) -> None:
        assert extract_size_bytes("700MB") is None


class TestMinimumQuality:
    def test_selects_lowest_resolution(
        self,
        download_options: list[dict[str, str]],
    ) -> None:
        chosen = select_smallest(download_options)

        assert chosen is not None
        assert "360p" in chosen["title"]

    def test_picks_smaller_of_equal_resolution(self) -> None:
        options = [
            {"title": "A 720p [900MB]", "url": "a"},
            {"title": "B 720p [400MB]", "url": "b"},
        ]

        chosen = select_smallest(options)

        assert chosen is not None
        assert chosen["url"] == "b"

    def test_skips_options_without_resolution(self) -> None:
        options = [
            {"title": "Download", "url": "a"},
            {"title": "B 480p", "url": "b"},
        ]

        chosen = select_smallest(options)

        assert chosen is not None
        assert chosen["url"] == "b"

    def test_returns_none_when_nothing_qualifies(self) -> None:
        options = [
            {"title": "Download", "url": "a"},
            {"title": "Watch online", "url": "b"},
        ]

        assert select_smallest(options) is None

    def test_returns_none_for_empty(self) -> None:
        assert select_smallest([]) is None

    def test_minimum_quality_matches_everything(self) -> None:
        assert matches_quality("anything", MINIMUM_QUALITY)

    def test_minimum_filter_keeps_all(
        self,
        download_options: list[dict[str, str]],
    ) -> None:
        assert len(
            find_quality_options(
                download_options,
                MINIMUM_QUALITY,
            )
        ) == len(download_options)

    def test_minimum_is_lookupable_by_key(self) -> None:
        assert get_quality("a") is MINIMUM_QUALITY

    def test_minimum_appears_in_the_menu(self) -> None:
        assert MINIMUM_QUALITY in get_quality_options()


class TestSortBySize:
    def test_orders_ascending(self) -> None:
        options = [
            {"title": "1080p", "url": "a"},
            {"title": "360p", "url": "b"},
            {"title": "720p", "url": "c"},
        ]

        ordered = sort_by_size(options)

        assert [o["url"] for o in ordered] == [
            "b",
            "c",
            "a",
        ]

    def test_unknown_resolution_sorts_last(self) -> None:
        options = [
            {"title": "Download", "url": "a"},
            {"title": "480p", "url": "b"},
        ]

        ordered = sort_by_size(options)

        assert ordered[-1]["url"] == "a"

    def test_does_not_mutate_input(self) -> None:
        options = [
            {"title": "1080p", "url": "a"},
            {"title": "360p", "url": "b"},
        ]

        original = list(options)

        sort_by_size(options)

        assert options == original

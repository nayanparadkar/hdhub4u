"""Tests for hdhub4u.http_types."""

from __future__ import annotations

import pytest

from hdhub4u.http_types import (
    is_media_content_type,
    normalize_content_type,
)


class TestNormalizeContentType:
    def test_strips_parameters(self) -> None:
        assert (
            normalize_content_type(
                "video/mp4; charset=utf-8"
            )
            == "video/mp4"
        )

    def test_lowercases(self) -> None:
        assert (
            normalize_content_type("VIDEO/MP4")
            == "video/mp4"
        )

    def test_trims_whitespace(self) -> None:
        assert (
            normalize_content_type("  video/mp4  ")
            == "video/mp4"
        )


class TestIsMediaContentType:
    @pytest.mark.parametrize(
        "content_type",
        [
            "video/mp4",
            "video/x-matroska",
            "audio/mpeg",
            "application/octet-stream",
            "video/mp4; charset=utf-8",
            "APPLICATION/OCTET-STREAM",
        ],
    )
    def test_media(
        self,
        content_type: str,
    ) -> None:
        assert is_media_content_type(content_type)

    @pytest.mark.parametrize(
        "content_type",
        [
            "text/html",
            "text/html; charset=UTF-8",
            "application/json",
            "text/plain",
            "",
        ],
    )
    def test_not_media(
        self,
        content_type: str,
    ) -> None:
        assert not is_media_content_type(
            content_type
        )

"""Tests for URL inspection."""

from __future__ import annotations

import httpx
import pytest

from hdhub4u.inspector import (
    extract_title,
    get_url_type,
    inspect_url,
)


class TestGetUrlType:
    @pytest.mark.parametrize(
        ("url", "content_type", "expected"),
        [
            ("https://x.test/a.mp4", "", "media"),
            ("https://x.test/a.mkv", "", "media"),
            ("https://x.test/a.mp3", "", "media"),
            ("https://x.test/page", "video/mp4", "media"),
            (
                "https://x.test/page",
                "text/html; charset=UTF-8",
                "webpage",
            ),
            ("https://x.test/f.bin", "other/x", "other"),
        ],
    )
    def test_classification(
        self,
        url: str,
        content_type: str,
        expected: str,
    ) -> None:
        assert get_url_type(url, content_type) == expected

    def test_extension_wins_over_octet_stream(
        self,
    ) -> None:
        assert (
            get_url_type(
                "https://x.test/a.mp4",
                "application/octet-stream",
            )
            == "media"
        )


class TestExtractTitle:
    def test_reads_title(self) -> None:
        assert extract_title(
            "<html><head><title> Movie </title></head></html>"
        ) == "Movie"

    def test_missing_title_is_empty(self) -> None:
        assert extract_title("<html></html>") == ""


class TestInspectUrl:
    def test_html_result_carries_body(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fake_get(self, url):
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<html><head><title>T</title></head></html>",
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(
            httpx.Client,
            "get",
            fake_get,
        )

        result = inspect_url("https://x.test/page")

        assert result["type"] == "webpage"
        assert result["title"] == "T"
        assert result["html"]
        assert result["error"] == ""

    def test_media_result_omits_body(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fake_get(self, url):
            return httpx.Response(
                200,
                headers={"content-type": "video/mp4"},
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(
            httpx.Client,
            "get",
            fake_get,
        )

        result = inspect_url("https://x.test/a.mp4")

        assert result["type"] == "media"
        assert result["html"] == ""

    def test_timeout_is_reported_not_raised(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fake_get(self, url):
            raise httpx.ReadTimeout("slow")

        monkeypatch.setattr(
            httpx.Client,
            "get",
            fake_get,
        )

        result = inspect_url("https://x.test/a")

        assert result["type"] == "error"
        assert "timed out" in str(result["error"])

    def test_request_error_is_reported_not_raised(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fake_get(self, url):
            raise httpx.ConnectError("no route")

        monkeypatch.setattr(
            httpx.Client,
            "get",
            fake_get,
        )

        result = inspect_url("https://x.test/a")

        assert result["type"] == "error"
        assert result["error"]

"""Tests for hdhub4u.probe."""

from __future__ import annotations

import pytest

from hdhub4u.http_types import (
    extension_from_content_type,
    extension_from_url,
    resolve_extension,
)
from hdhub4u.probe import (
    OptionProbe,
    ProbeResult,
    classify_failure,
    group_by_downloadability,
    summarize_gating,
)
from hdhub4u.resolver import (
    HostAdapterRegistry,
    ResolveResult,
)


class StubResolver:
    """A resolver returning a fixed result."""

    name = "stub"

    def __init__(
        self,
        result: ResolveResult | Exception,
    ) -> None:
        self.result = result

    def resolve(
        self,
        url: str,
    ) -> ResolveResult:
        if isinstance(
            self.result,
            Exception,
        ):
            raise self.result

        return self.result


def media_result(
    url: str,
    content_type: str = "video/mp4",
) -> ResolveResult:
    return ResolveResult(
        original_url=url,
        final_url=url,
        status_code=200,
        content_type=content_type,
        is_media=True,
        redirects=0,
    )


def html_result(
    url: str,
) -> ResolveResult:
    return ResolveResult(
        original_url=url,
        final_url=url,
        status_code=200,
        content_type="text/html; charset=UTF-8",
        is_media=False,
        redirects=0,
    )


class TestClassifyFailure:
    def test_html_means_needs_a_browser(self) -> None:
        result = classify_failure(
            200,
            "text/html; charset=UTF-8",
        )

        assert "browser" in result
        assert "gated" in result

    def test_403_is_blocked(self) -> None:
        assert "blocked" in classify_failure(
            403,
            "video/mp4",
        )

    def test_404_is_missing(self) -> None:
        assert "missing" in classify_failure(
            404,
            "video/mp4",
        )

    def test_429_is_throttled(self) -> None:
        assert "throttled" in classify_failure(
            429,
            "video/mp4",
        )

    def test_500_is_server_error(self) -> None:
        assert "server error" in classify_failure(
            503,
            "video/mp4",
        )

    def test_no_content_type_is_unclear(
        self,
    ) -> None:
        assert "unclear" in classify_failure(
            200,
            "",
        )

    def test_other_type_is_named(self) -> None:
        result = classify_failure(
            200,
            "application/zip",
        )

        assert "application/zip" in result


class TestOptionProbe:
    def test_media_url_is_downloadable(
        self,
    ) -> None:
        url = "https://cdn.test/film.mp4"

        probe = OptionProbe(
            StubResolver(media_result(url))
        )

        result = probe.probe(url)

        assert result.is_media
        assert result.extension == ".mp4"
        assert result.host == "cdn.test"
        assert result.label == "downloadable"

    def test_html_url_is_reported_as_gated(
        self,
    ) -> None:
        url = "https://gated.test/x"

        probe = OptionProbe(
            StubResolver(html_result(url))
        )

        result = probe.probe(url)

        assert not result.is_media
        assert "gated" in result.reason
        assert result.label != "downloadable"

    def test_network_failure_never_raises(
        self,
    ) -> None:
        probe = OptionProbe(
            StubResolver(
                RuntimeError("connection reset")
            )
        )

        result = probe.probe(
            "https://down.test/x"
        )

        assert not result.is_media
        assert "unreachable" in result.reason

    def test_octet_stream_uses_url_suffix(
        self,
    ) -> None:
        url = "https://cdn.test/film.mkv"

        probe = OptionProbe(
            StubResolver(
                media_result(
                    url,
                    "application/octet-stream",
                )
            )
        )

        result = probe.probe(url)

        assert result.is_media
        assert result.extension == ".mkv"

    def test_probe_all_preserves_order(self) -> None:
        options = [
            {"url": "https://a.test/1.mp4"},
            {"url": "https://a.test/2.mp4"},
            {"url": "https://a.test/3.mp4"},
        ]

        probe = OptionProbe(
            StubResolver(
                media_result(
                    "https://a.test/1.mp4"
                )
            )
        )

        results = probe.probe_all(options)

        assert [
            result.url
            for result in results
        ] == [
            option["url"]
            for option in options
        ]

    def test_uses_host_adapter_when_registered(
        self,
    ) -> None:
        class MarkerResolver:
            name = "adapter"

            def resolve(
                self,
                url: str,
            ) -> ResolveResult:
                return ResolveResult(
                    original_url=url,
                    final_url=url,
                    status_code=200,
                    content_type="video/x-matroska",
                    is_media=True,
                    redirects=0,
                    strategy="adapter",
                )

        registry = HostAdapterRegistry()
        registry.register(
            "special.test",
            MarkerResolver,
        )

        probe = OptionProbe(
            StubResolver(
                html_result(
                    "https://special.test/x"
                )
            ),
            registry=registry,
        )

        result = probe.probe(
            "https://special.test/x"
        )

        assert result.is_media
        assert result.extension == ".mkv"

    def test_unregistered_host_uses_base_resolver(
        self,
    ) -> None:
        registry = HostAdapterRegistry()

        probe = OptionProbe(
            StubResolver(
                media_result(
                    "https://other.test/x.mp4"
                )
            ),
            registry=registry,
        )

        result = probe.probe(
            "https://other.test/x.mp4"
        )

        assert result.is_media


class TestGroupByDownloadability:
    def test_splits_and_keeps_order(self) -> None:
        results = [
            ProbeResult(
                url="a",
                is_media=True,
                status_code=200,
                content_type="video/mp4",
                extension=".mp4",
                host="a.test",
            ),
            ProbeResult(
                url="b",
                is_media=False,
                status_code=200,
                content_type="text/html",
                extension="",
                host="b.test",
                reason="gated",
            ),
        ]

        usable, blocked = group_by_downloadability(
            results
        )

        assert [
            result.url
            for result in usable
        ] == ["a"]
        assert [
            result.url
            for result in blocked
        ] == ["b"]


class TestSummarizeGating:
    def test_all_downloadable(self) -> None:
        results = [
            ProbeResult(
                url="a",
                is_media=True,
                status_code=200,
                content_type="video/mp4",
                extension=".mp4",
                host="a.test",
            ),
        ]

        message = summarize_gating(results)

        assert "directly downloadable" in message
        assert "Not downloadable" not in message

    def test_reports_blocked_hosts(self) -> None:
        results = [
            ProbeResult(
                url="a",
                is_media=True,
                status_code=200,
                content_type="video/mp4",
                extension=".mp4",
                host="a.test",
            ),
            ProbeResult(
                url="b",
                is_media=False,
                status_code=200,
                content_type="text/html",
                extension="",
                host="gated.test",
                reason="gated",
            ),
        ]

        message = summarize_gating(results)

        assert "1 of 2" in message
        assert "gated.test" in message


class TestExtensionHelpers:
    @pytest.mark.parametrize(
        ("content_type", "expected"),
        [
            ("video/mp4", ".mp4"),
            ("video/x-matroska", ".mkv"),
            ("video/webm", ".webm"),
            ("audio/mpeg", ".mp3"),
            ("audio/flac", ".flac"),
            ("video/mp4; charset=utf-8", ".mp4"),
            ("video/x-newcodec", ".mp4"),
            ("text/html", ""),
            ("", ""),
        ],
    )
    def test_extension_from_content_type(
        self,
        content_type: str,
        expected: str,
    ) -> None:
        assert (
            extension_from_content_type(content_type)
            == expected
        )

    def test_extension_from_url(self) -> None:
        assert (
            extension_from_url(
                "https://cdn.test/a/b.mkv?x=1"
            )
            == ".mkv"
        )

    def test_url_without_media_suffix(self) -> None:
        assert (
            extension_from_url(
                "https://cdn.test/download"
            )
            == ""
        )

    def test_resolve_prefers_url(self) -> None:
        assert resolve_extension(
            "https://cdn.test/a.mkv",
            "application/octet-stream",
        ) == ".mkv"

    def test_resolve_falls_back_to_type(self) -> None:
        assert resolve_extension(
            "https://cdn.test/download",
            "video/x-matroska",
        ) == ".mkv"

    def test_resolve_returns_empty_for_html(
        self,
    ) -> None:
        assert resolve_extension(
            "https://cdn.test/page",
            "text/html",
        ) == ""
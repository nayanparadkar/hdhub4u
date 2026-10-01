"""Tests for link resolution."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import httpx
import pytest

from hdhub4u.resolver import (
    ChainResolver,
    DirectResolver,
    LinkResolver,
    ResolveResult,
    build_resolver,
)


def _result(
    content_type: str,
    *,
    is_media: bool = False,
    strategy: str = "fake",
    final_url: str = "https://x.test/f",
    resolver_name: str | None = None,
) -> ResolveResult:
    return ResolveResult(
        original_url="https://x.test/",
        final_url=final_url,
        status_code=200,
        content_type=content_type,
        is_media=is_media,
        redirects=1,
        strategy=strategy,
    )


class _Stub:
    """A resolver returning a fixed result or raising."""

    def __init__(
        self,
        name: str,
        result: ResolveResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self.result = result
        self.error = error
        self.calls: list[str] = []

    def resolve(self, url: str) -> ResolveResult:
        self.calls.append(url)

        if self.error is not None:
            raise self.error

        assert self.result is not None

        # Real strategies stamp their own name, so the stub does
        # the same. That is how the chain identifies its winner.
        return replace(
            self.result,
            strategy=self.name,
        )


def test_protocol_is_runtime_checkable() -> None:
    assert isinstance(
        DirectResolver(),
        LinkResolver,
    )


def test_chain_requires_a_strategy() -> None:
    with pytest.raises(ValueError):
        ChainResolver([])


def test_chain_returns_first_media_result() -> None:
    html = _Stub("html", _result("text/html"))
    browser = _Stub(
        "browser",
        _result("video/mp4", is_media=True),
    )
    direct = _Stub("direct", _result("video/mp4", is_media=True))

    result = ChainResolver(
        [html, browser, direct]
    ).resolve("https://gate.test/a")

    assert result.is_media
    assert result.strategy == "browser"
    assert direct.calls == []


def test_chain_reports_first_result_when_all_decline() -> None:
    """
    When nothing resolves, the caller must see the server's own
    content type rather than a generic failure.
    """

    html = _Stub(
        "html",
        _result("text/html; charset=UTF-8"),
    )
    direct = _Stub(
        "direct",
        _result("text/html"),
    )

    result = ChainResolver([html, direct]).resolve(
        "https://gate.test/a"
    )

    assert not result.is_media
    assert result.content_type == "text/html; charset=UTF-8"


def test_chain_skips_a_raising_strategy() -> None:
    broken = _Stub("broken", error=RuntimeError("no"))
    working = _Stub(
        "working",
        _result("video/mp4", is_media=True),
    )

    result = ChainResolver([broken, working]).resolve(
        "https://gate.test/a"
    )

    assert result.strategy == "working"


def test_chain_raises_when_nothing_attempted() -> None:
    broken = _Stub("broken", error=RuntimeError("no"))

    with pytest.raises(ValueError):
        ChainResolver([broken]).resolve("https://x.test/")


def test_build_resolver_defaults_to_direct() -> None:
    assert isinstance(
        build_resolver(),
        DirectResolver,
    )


def test_build_resolver_chains_extra_strategies() -> None:
    stub = _Stub("browser")

    resolver = build_resolver(strategies=[stub])

    assert isinstance(resolver, ChainResolver)
    assert resolver.resolvers[0] is stub


def test_describe_is_single_line() -> None:
    text = _result(
        "video/mp4",
        is_media=True,
    ).describe()

    assert "\n" not in text
    assert "video/mp4" in text
    assert "1 redirect" in text


def test_describe_pluralizes_redirects() -> None:
    result = ResolveResult(
        original_url="u",
        final_url="f",
        status_code=200,
        content_type="video/mp4",
        is_media=True,
        redirects=3,
    )

    assert "3 redirects" in result.describe()


def test_direct_resolver_returns_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_head(self: Any, url: str) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "video/mp4"},
            request=httpx.Request("HEAD", url),
        )

    monkeypatch.setattr(
        httpx.Client,
        "head",
        fake_head,
    )

    result = DirectResolver().resolve(
        "https://cdn.test/movie.mp4"
    )

    assert result.is_media
    assert result.strategy == "direct"


def test_direct_resolver_rejects_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_head(self: Any, url: str) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            request=httpx.Request("HEAD", url),
        )

    monkeypatch.setattr(
        httpx.Client,
        "head",
        fake_head,
    )

    result = DirectResolver().resolve(
        "https://gate.test/a"
    )

    assert not result.is_media


def test_direct_resolver_falls_back_to_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Some servers reject HEAD outright, so a one-byte ranged GET
    is the fallback.
    """

    def fake_head(self: Any, url: str) -> httpx.Response:
        return httpx.Response(
            405,
            request=httpx.Request("HEAD", url),
        )

    seen: dict[str, str] = {}

    def fake_get(
        self: Any,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        seen.update(headers or {})

        return httpx.Response(
            200,
            headers={"content-type": "video/mp4"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(
        httpx.Client,
        "head",
        fake_head,
    )
    monkeypatch.setattr(
        httpx.Client,
        "get",
        fake_get,
    )

    result = DirectResolver().resolve(
        "https://cdn.test/movie.mp4"
    )

    assert result.is_media
    assert seen["Range"] == "bytes=0-0"

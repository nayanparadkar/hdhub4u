"""Tests for the streaming download path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from hdhub4u.downloader import DownloadJob, download_file
from hdhub4u.errors import DownloadError


class _Stream:
    """
    Minimal stand-in for httpx.stream.

    httpx.stream is used as a context manager, so a fake has to
    support both the call and the response context protocol.
    """

    def __init__(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> None:
        self.method = method
        self.url = url
        self.kwargs = kwargs
        self.headers_sent = kwargs.get("headers", {})

        self._response = (
            self.response_factory(method, url)
        )

    @staticmethod
    def response_factory(
        method: str,
        url: str,
    ) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "video/mp4",
                "content-length": "6",
            },
            content=b"abcdef",
            request=httpx.Request(method, url),
        )

    def __enter__(self) -> httpx.Response:
        return self._response

    def __exit__(self, *args: object) -> None:
        return None


def _job(
    root: Path,
    name: str = "file.mp4",
    url: str = "https://cdn.test/file.mp4",
) -> DownloadJob:
    return DownloadJob(
        title="Movie",
        quality="auto",
        option_title="video",
        url=url,
        output_directory=root,
        output_filename=name,
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    stream: Any,
) -> None:
    monkeypatch.setattr(httpx, "stream", stream)


def test_writes_media_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _Stream)

    path = download_file(_job(tmp_path))

    assert path.read_bytes() == b"abcdef"
    assert not _job(tmp_path).partial_path.exists()


def test_rejects_html_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def html(
        method: str,
        url: str,
        **kwargs: Any,
    ) -> _Stream:
        class _S(_Stream):
            @staticmethod
            def response_factory(
                method: str,
                url: str,
            ) -> httpx.Response:
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    content=b"<html>",
                    request=httpx.Request(method, url),
                )

        return _S(method, url, **kwargs)

    _install(monkeypatch, html)

    with pytest.raises(DownloadError) as info:
        download_file(_job(tmp_path))

    assert "media" in str(info.value)


def test_reports_http_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing(
        method: str,
        url: str,
        **kwargs: Any,
    ) -> _Stream:
        class _S(_Stream):
            @staticmethod
            def response_factory(
                method: str,
                url: str,
            ) -> httpx.Response:
                return httpx.Response(
                    404,
                    request=httpx.Request(method, url),
                )

        return _S(method, url, **kwargs)

    _install(monkeypatch, failing)

    with pytest.raises(DownloadError) as info:
        download_file(_job(tmp_path))

    assert "404" in str(info.value)


def test_reports_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow(
        method: str,
        url: str,
        **kwargs: Any,
    ) -> None:
        raise httpx.ReadTimeout("slow")

    _install(monkeypatch, slow)

    with pytest.raises(DownloadError) as info:
        download_file(_job(tmp_path))

    assert "timed out" in str(info.value)


def test_reports_request_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(
        method: str,
        url: str,
        **kwargs: Any,
    ) -> None:
        raise httpx.ConnectError("no route")

    _install(monkeypatch, broken)

    with pytest.raises(DownloadError) as info:
        download_file(_job(tmp_path))

    assert "no route" in str(info.value)


def test_resumes_from_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A 206 response with a Range header means the existing bytes are
    kept rather than overwritten.
    """

    job = _job(tmp_path)

    job.partial_path.write_bytes(b"abc")

    sent: dict[str, str] = {}

    class _S(_Stream):
        @staticmethod
        def response_factory(
            method: str,
            url: str,
        ) -> httpx.Response:
            return httpx.Response(
                206,
                headers={
                    "content-type": "video/mp4",
                    "content-length": "3",
                },
                content=b"def",
                request=httpx.Request(method, url),
            )

    def stream(
        method: str,
        url: str,
        **kwargs: Any,
    ) -> _S:
        sent.update(kwargs.get("headers", {}))

        return _S(method, url, **kwargs)

    _install(monkeypatch, stream)

    path = download_file(job)

    assert sent["Range"] == "bytes=3-"
    assert path.read_bytes() == b"abcdef"


def test_restarts_when_range_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A 200 in reply to a Range request means the server sent the
    whole file, so the partial must be discarded, not appended.
    """

    job = _job(tmp_path)

    job.partial_path.write_bytes(b"abc")

    def stream(
        method: str,
        url: str,
        **kwargs: Any,
    ) -> _Stream:
        return _Stream(method, url, **kwargs)

    _install(monkeypatch, stream)

    path = download_file(job)

    assert path.read_bytes() == b"abcdef"


def test_refuses_to_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _Stream)

    job = _job(tmp_path)

    job.output_path.write_bytes(b"existing")

    with pytest.raises(DownloadError) as info:
        download_file(job)

    assert "Already downloaded" in str(info.value)
    assert job.output_path.read_bytes() == b"existing"


def test_detects_short_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _S(_Stream):
        @staticmethod
        def response_factory(
            method: str,
            url: str,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                headers={
                    "content-type": "video/mp4",
                    "content-length": "100",
                },
                content=b"abcdef",
                request=httpx.Request(method, url),
            )

    def stream(
        method: str,
        url: str,
        **kwargs: Any,
    ) -> _S:
        return _S(method, url, **kwargs)

    _install(monkeypatch, stream)

    job = _job(tmp_path)

    with pytest.raises(DownloadError) as info:
        download_file(job)

    assert "Incomplete" in str(info.value)

    # The partial is kept so a rerun can resume.
    assert job.partial_path.exists()
    assert not job.output_path.exists()


def test_progress_callback_reports_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[int, int | None]] = []

    def stream(
        method: str,
        url: str,
        **kwargs: Any,
    ) -> _Stream:
        return _Stream(method, url, **kwargs)

    _install(monkeypatch, stream)

    download_file(
        _job(tmp_path),
        progress_callback=lambda done, total: seen.append(
            (done, total)
        ),
    )

    assert seen[0][0] == 0
    assert seen[-1][0] == 6
    assert seen[-1][1] == 6


def test_uses_the_original_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The download must request the URL the site handed out. Following
    a redirect instead would drop the signed parameters that
    authorize the transfer.
    """

    requested: list[str] = []

    def stream(
        method: str,
        url: str,
        **kwargs: Any,
    ) -> _Stream:
        requested.append(url)

        return _Stream(method, url, **kwargs)

    _install(monkeypatch, stream)

    download_file(
        _job(
            tmp_path,
            url="https://gate.test/a?token=signed",
        )
    )

    assert requested == [
        "https://gate.test/a?token=signed"
    ]


def test_creates_missing_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _Stream)

    target = tmp_path / "new" / "nested"

    path = download_file(_job(target))

    assert path.exists()

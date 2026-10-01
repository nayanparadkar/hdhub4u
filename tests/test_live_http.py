"""Live HTTP tests against a local server.

The rest of the suite stubs httpx, which is why a host answering
with ``text/html`` instead of media went unnoticed: no test ever
saw a real response. These tests run a real server over a real
socket, so the content-type gate and the extension logic are
exercised end to end.

They are self-contained and need no internet access.
"""

from __future__ import annotations

import threading
from http.server import (
    BaseHTTPRequestHandler,
    ThreadingHTTPServer,
)

import pytest

from hdhub4u.downloader import (
    create_download_job,
    download_file,
)
from hdhub4u.errors import DownloadError
from hdhub4u.http_types import (
    is_media_content_type,
    resolve_extension,
)
from hdhub4u.probe import (
    OptionProbe,
    classify_failure,
    group_by_downloadability,
    summarize_gating,
)
from hdhub4u.resolver import DirectResolver

#: A short but structurally valid MP4 header, so the saved file is
#: recognisable as media by a type detector.
FAKE_MP4 = (
    b"\x00\x00\x00\x20ftypisom"
    b"\x00\x00\x02\x00isomiso2"
    + b"\x00" * 4096
)


class Handler(BaseHTTPRequestHandler):
    """Serves a fixed set of paths, each with a chosen response."""

    protocol_version = "HTTP/1.1"

    routes: dict[
        str,
        tuple[int, str, bytes],
    ] = {}

    def log_message(
        self,
        *args,
    ) -> None:
        """Stay quiet during tests."""

    def _send(
        self,
        body: bytes | None = None,
    ) -> None:
        status, content_type, payload = self.routes.get(
            self.path.split("?")[0],
            (404, "text/html", b"not found"),
        )

        if body is None:
            body = payload

        self.send_response(status)

        self.send_header(
            "Content-Type",
            content_type,
        )

        self.send_header(
            "Content-Length",
            str(len(body)),
        )

        if self.path.endswith(".part"):
            self.send_header(
                "Accept-Ranges",
                "bytes",
            )

        self.end_headers()

        self.wfile.write(body)

    def do_HEAD(self) -> None:
        status, content_type, payload = self.routes.get(
            self.path.split("?")[0],
            (404, "text/html", b"not found"),
        )

        self.send_response(status)

        self.send_header(
            "Content-Type",
            content_type,
        )

        self.send_header(
            "Content-Length",
            str(len(payload)),
        )

        self.end_headers()

    def do_GET(self) -> None:
        # Honour a Range request so the resume path is real.
        range_header = self.headers.get("Range")

        if range_header and self.path.endswith(
            ".part"
        ):
            status, content_type, payload = (
                self.routes[
                    self.path.split("?")[0]
                ]
            )

            start = int(
                range_header.split("=")[1].split("-")[0]
            )

            chunk = payload[start:]

            self.send_response(206)

            self.send_header(
                "Content-Type",
                content_type,
            )

            self.send_header(
                "Content-Range",
                (
                    f"bytes {start}-"
                    f"{len(payload) - 1}"
                    f"/{len(payload)}"
                ),
            )

            self.send_header(
                "Content-Length",
                str(len(chunk)),
            )

            self.end_headers()

            self.wfile.write(chunk)

            return

        self._send()


@pytest.fixture(scope="module")
def live_server():
    """Run a real HTTP server for the duration of this module."""

    Handler.routes = {
        "/film.mp4": (
            200,
            "video/mp4",
            FAKE_MP4,
        ),
        "/film.mkv": (
            200,
            "application/octet-stream",
            FAKE_MP4,
        ),
        "/no-suffix": (
            200,
            "video/x-matroska",
            FAKE_MP4,
        ),
        "/gated": (
            200,
            "text/html; charset=UTF-8",
            b"<html><body>Redirecting</body></html>",
        ),
        "/forbidden": (
            403,
            "text/html",
            b"<html>nope</html>",
        ),
    }

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        Handler,
    )

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )

    thread.start()

    yield (
        f"http://127.0.0.1:{server.server_port}"
    )

    server.shutdown()
    server.server_close()


class TestLiveMediaValidation:
    def test_real_mp4_passes_the_gate(
        self,
        live_server: str,
    ) -> None:
        response = DirectResolver().resolve(
            f"{live_server}/film.mp4"
        )

        assert response.is_media
        assert response.content_type == "video/mp4"

    def test_real_html_page_fails_the_gate(
        self,
        live_server: str,
    ) -> None:
        response = DirectResolver().resolve(
            f"{live_server}/gated"
        )

        assert not response.is_media

        reason = classify_failure(
            response.status_code,
            response.content_type,
        )

        assert "browser" in reason

    def test_octet_stream_with_mkv_path_is_media(
        self,
        live_server: str,
    ) -> None:
        # This is the real-world case: a host serving
        # application/octet-stream for every file.
        response = DirectResolver().resolve(
            f"{live_server}/film.mkv"
        )

        assert response.is_media
        assert (
            resolve_extension(
                response.final_url,
                response.content_type,
            )
            == ".mkv"
        )


class TestLiveDownload:
    def test_download_gains_a_playable_extension(
        self,
        live_server: str,
        tmp_path,
    ) -> None:
        job = create_download_job(
            "Test Film",
            "480p",
            {
                "title": "480p",
                "url": f"{live_server}/film.mp4",
            },
            root=tmp_path,
            extension=".mp4",
        )

        path = download_file(job)

        assert path.name.endswith(".mp4")
        assert path.stat().st_size == len(FAKE_MP4)
        assert path.read_bytes().startswith(
            b"\x00\x00\x00\x20ftypisom"
        )

    def test_extension_inferred_when_job_omits_it(
        self,
        live_server: str,
        tmp_path,
    ) -> None:
        # No extension passed in: the response has to supply it,
        # otherwise the saved file is unplayable.
        job = create_download_job(
            "Test Film",
            "480p",
            {
                "title": "480p",
                "url": f"{live_server}/film.mkv",
            },
            root=tmp_path,
        )

        path = download_file(job)

        assert path.suffix == ".mkv"
        assert path.read_bytes() == FAKE_MP4

    def test_extension_from_type_when_url_has_none(
        self,
        live_server: str,
        tmp_path,
    ) -> None:
        job = create_download_job(
            "Test Film",
            "480p",
            {
                "title": "480p",
                "url": f"{live_server}/no-suffix",
            },
            root=tmp_path,
        )

        path = download_file(job)

        assert path.suffix == ".mkv"

    def test_gated_page_is_refused(
        self,
        live_server: str,
        tmp_path,
    ) -> None:
        job = create_download_job(
            "Gated Film",
            "480p",
            {
                "title": "480p",
                "url": f"{live_server}/gated",
            },
            root=tmp_path,
        )

        with pytest.raises(DownloadError) as info:
            download_file(job)

        assert "media" in str(info.value)

        # Nothing is promoted to a final name.
        assert not list(
            tmp_path.rglob("*.mp4")
        )

    def test_resume_over_a_real_socket(
        self,
        live_server: str,
        tmp_path,
    ) -> None:
        job = create_download_job(
            "Resumed Film",
            "480p",
            {
                "title": "480p",
                "url": f"{live_server}/film.mp4",
            },
            root=tmp_path,
            extension=".mp4",
        )

        job.output_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        # Simulate an interrupted transfer.
        job.partial_path.write_bytes(
            FAKE_MP4[:1000]
        )

        path = download_file(job)

        assert path.stat().st_size == len(FAKE_MP4)
        assert path.read_bytes() == FAKE_MP4
        assert not job.partial_path.exists()


class TestLiveProbe:
    def test_probe_reports_real_responses(
        self,
        live_server: str,
    ) -> None:
        options = [
            {
                "title": "480p",
                "url": f"{live_server}/film.mp4",
            },
            {
                "title": "720p",
                "url": f"{live_server}/gated",
            },
            {
                "title": "1080p",
                "url": f"{live_server}/forbidden",
            },
        ]

        results = OptionProbe(
            DirectResolver(timeout=10.0)
        ).probe_all(options)

        by_title = dict(
            zip(
                [
                    option["title"]
                    for option in options
                ],
                results,
                strict=True,
            )
        )

        assert by_title["480p"].is_media
        assert (
            by_title["480p"].extension == ".mp4"
        )

        assert not by_title["720p"].is_media
        assert "browser" in by_title["720p"].reason

        assert not by_title["1080p"].is_media
        assert "blocked" in by_title["1080p"].reason

    def test_summary_reports_mixed_outcome(
        self,
        live_server: str,
    ) -> None:
        results = OptionProbe(
            DirectResolver(timeout=10.0)
        ).probe_all([
            {
                "title": "480p",
                "url": f"{live_server}/film.mp4",
            },
            {
                "title": "720p",
                "url": f"{live_server}/gated",
            },
        ])

        message = summarize_gating(results)

        assert "1 of 2" in message

        usable, blocked = group_by_downloadability(
            results
        )

        assert len(usable) == 1
        assert len(blocked) == 1

    def test_every_option_gated_is_stated(
        self,
        live_server: str,
    ) -> None:
        results = OptionProbe(
            DirectResolver(timeout=10.0)
        ).probe_all([
            {
                "title": "720p",
                "url": f"{live_server}/gated",
            },
        ])

        assert not any(
            result.is_media
            for result in results
        )

        assert "0 of 1" in summarize_gating(
            results
        )


class TestLiveContentTypes:
    @pytest.mark.parametrize(
        "content_type",
        [
            "video/mp4",
            "video/x-matroska",
            "application/octet-stream",
            "audio/mpeg",
        ],
    )
    def test_media_types_accepted(
        self,
        content_type: str,
    ) -> None:
        assert is_media_content_type(
            content_type
        )

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
    def test_non_media_types_rejected(
        self,
        content_type: str,
    ) -> None:
        assert not is_media_content_type(
            content_type
        )

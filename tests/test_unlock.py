"""Tests for hdhub4u.unlock."""

from __future__ import annotations

import base64
import logging
from urllib.parse import quote

import httpx
import pytest

from hdhub4u import unlock as unlock_module
from hdhub4u.catalog import (
    DOWNLOAD_KIND,
    STREAMING_KIND,
    MediaOption,
)
from hdhub4u.errors import NetworkError, ResolutionError
from hdhub4u.unlock import (
    UnlockedLink,
    extension_for,
    filename_from_disposition,
    format_size,
    host_suffix,
    sniff_container,
    unlock,
    unlock_hubcdn,
    unlock_hubdrive,
    unlock_many,
    verify_media,
)

MATROSKA_PREFIX = b"\x1a\x45\xdf\xa3" + b"\x00" * 200
MP4_PREFIX = (
    b"\x00\x00\x00\x20ftypisom"
    + b"\x00\x00\x02\x00isomiso2avc1mp41"
)
MPEG_TS_PACKET_SIZE = 188
MPEG_TS_QUORUM = unlock_module._MPEG_TS_QUORUM


def _mpeg_ts_prefix(packets: int = MPEG_TS_QUORUM) -> bytes:
    """
    Build a plausible transport stream prefix.

    Every packet opens with the sync byte and the packets follow
    one another at a fixed stride, which is the only part of a
    stream that can be told from a body of text.

    The default is the quorum the module demands, so a prefix built
    here is one it has to accept; pass fewer to build one it must
    decline.
    """

    packet = b"\x47" + b"\x00" * (MPEG_TS_PACKET_SIZE - 1)

    return packet * packets


def _riff_prefix(form: bytes) -> bytes:
    """Build a RIFF header naming the form it wraps."""

    return (
        b"RIFF"
        + b"\x20\x00\x00\x00"
        + form
        + b"fmt " + b"\x00" * 16
    )


class _Dribble(httpx.SyncByteStream):
    """
    A body delivered in small pieces, the way a socket fills.

    Records how much of it a reader actually pulled, so a test can
    show that a prefix read costs a prefix and no more.
    """

    def __init__(
        self,
        data: bytes,
        piece: int = 8,
    ) -> None:
        self.data = data
        self.piece = piece
        self.served = 0

    def __iter__(self):
        for start in range(0, len(self.data), self.piece):
            chunk = self.data[start:start + self.piece]
            self.served += len(chunk)
            yield chunk


def _client(handler) -> httpx.Client:
    """Return a client whose requests are answered by a handler."""

    return httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )


def _option(url: str, kind: str = DOWNLOAD_KIND) -> MediaOption:
    """Build an option pointing at a gate."""

    from hdhub4u.catalog import registrable_host

    return MediaOption(
        label="480p",
        url=url,
        kind=kind,
        host=registrable_host(url),
    )


def _media_response(
    request: httpx.Request,
    name: str,
    size: int,
) -> httpx.Response:
    """Answer with a partial-content media response."""

    return httpx.Response(
        206,
        headers={
            "content-range": f"bytes 0-4095/{size}",
            "content-disposition": (
                f"attachment; filename*=UTF-8''{name}"
            ),
        },
        content=MATROSKA_PREFIX,
        request=request,
    )


class TestSniffContainer:
    def test_detects_matroska(self) -> None:
        assert sniff_container(MATROSKA_PREFIX) == "matroska"

    def test_detects_mp4(self) -> None:
        """
        A real MP4 opens with a four-byte box size, then ``ftyp``,
        then the major brand. Reading the brand at offset four would
        find ``ftyp`` and miss the file.
        """

        assert sniff_container(MP4_PREFIX) == "mp4"

    def test_detects_an_ftyp_prefix_with_no_size_box(self) -> None:
        assert sniff_container(b"ftypisom" + b"\x00" * 20) == "mp4"

    def test_ignores_html(self) -> None:
        assert (
            sniff_container(b"<!DOCTYPE html><html>not a movie")
            == ""
        )

    def test_ignores_a_short_prefix(self) -> None:
        assert sniff_container(b"\x1a\x45\xdf") == ""

    def test_text_beginning_with_the_sync_byte_is_not_a_stream(
        self,
    ) -> None:
        """
        The sync byte is also the letter ``G``, so a one-byte
        signature would call a GIF and any page of prose a
        transport stream.
        """

        assert sniff_container(b"GIF89a\x01\x00\x01\x00") == ""
        assert sniff_container(b"GraphQL query { viewer }") == ""
        assert sniff_container(b"Goodbye world") == ""
        assert sniff_container(b"G" + b"x" * 500) == ""

    def test_detects_a_real_transport_stream(self) -> None:
        """
        A quorum of sync bytes at the packet stride is what separates
        a stream from anything else that starts with ``G``.
        """

        assert sniff_container(_mpeg_ts_prefix()) == "mpeg-ts"

    def test_a_prefix_short_of_the_quorum_is_not_a_stream(self) -> None:
        """
        The old two-sync-byte test still lets prose through: the byte
        is also the letter ``G``, so any 188 bytes of English have a
        one-in-fifty chance of carrying a second one. Requiring a
        quorum of eight means eight independent chances instead,
        which is a million to one rather than fifty to one.
        """

        aligned_but_short = _mpeg_ts_prefix()[:MPEG_TS_PACKET_SIZE + 1]

        assert sniff_container(aligned_but_short) == ""

    def test_prose_is_not_a_stream(self) -> None:
        """
        The advertisement this module exists to reject. Pad it to a
        probe's worth so that the length requirement cannot be what
        turns it away, and assert it is the bytes that do.
        """

        prose = (
            b"Get unlimited premium access now. No thanks, not today."
        ) * 12
        padded = prose[:unlock_module.PROBE_BYTES]

        assert sniff_container(padded) == ""

    def test_a_gif_is_not_a_stream(self) -> None:
        """
        ``GIF89a`` opens with the sync byte and then the letter of
        the format, but a GIF is not a packet of 188 bytes.
        """

        head = b"GIF89a" + b"\x00" * (unlock_module.PROBE_BYTES - 6)

        assert sniff_container(head) == ""

    def test_a_prefix_without_a_stride_of_sync_bytes_is_not_a_stream(
        self,
    ) -> None:
        """
        Long enough to hold the quorum, but the packets do not
        start where packets have to start.
        """

        head = bytearray(_mpeg_ts_prefix())
        for index in range(1, MPEG_TS_QUORUM):
            head[index * MPEG_TS_PACKET_SIZE] = 0x48

        assert sniff_container(bytes(head)) == ""

    def test_a_truncated_stream_prefix_is_declined(self) -> None:
        """
        Fewer bytes than one packet leaves nothing to compare the
        leading byte against, so the answer is "not a file".
        """

        assert sniff_container(b"\x47" + b"\x00" * 10) == ""

    def test_tells_avi_from_wave(self) -> None:
        """
        RIFF names its inner format four bytes in. Reading only
        ``RIFF`` would save every audio file as video.
        """

        assert sniff_container(_riff_prefix(b"AVI ")) == "avi"
        assert sniff_container(_riff_prefix(b"WAVE")) == "wav"

    def test_an_unknown_riff_form_stays_generic(self) -> None:
        assert sniff_container(_riff_prefix(b"WEBP")) == "riff"

    def test_an_empty_prefix_is_unknown(self) -> None:
        assert sniff_container(b"") == ""


class TestFilenameFromDisposition:
    def test_prefers_the_rfc5987_form(self) -> None:
        header = (
            "attachment; filename=download; "
            "filename*=UTF-8''Death.of.a.Unicorn.2025.mkv"
        )

        assert filename_from_disposition(header) == (
            "Death.of.a.Unicorn.2025.mkv"
        )

    def test_falls_back_to_the_plain_form(self) -> None:
        header = 'attachment; filename="My Movie.mkv"'

        assert filename_from_disposition(header) == "My Movie.mkv"

    def test_decodes_a_non_ascii_name(self) -> None:
        header = (
            "attachment; "
            "filename*=UTF-8''%E0%A4%A8%E0%A4%AE%E0%A4%B8.mkv"
        )

        assert filename_from_disposition(header) == "नमस.mkv"

    def test_a_generic_name_still_counts(self) -> None:
        assert filename_from_disposition(
            "attachment; filename=download"
        ) == "download"

    def test_an_empty_header_yields_nothing(self) -> None:
        assert filename_from_disposition("") == ""


class TestExtensionFor:
    def test_maps_known_containers(self) -> None:
        assert extension_for("matroska") == ".mkv"
        assert extension_for("mp4") == ".mp4"

    def test_keeps_a_wav_file_out_of_an_avi_container(self) -> None:
        assert extension_for("wav") == ".wav"
        assert extension_for("avi") == ".avi"

    def test_falls_back_to_matroska(self) -> None:
        assert extension_for("something-else") == ".mkv"


class TestFormatSize:
    def test_formats_binary_units(self) -> None:
        assert format_size(1536) == "1.5 KB"
        assert format_size(461_628_928) == "440.2 MB"

    def test_a_missing_size_reads_as_unknown(self) -> None:
        assert format_size(None) == "unknown size"

    def test_zero_is_still_a_size(self) -> None:
        assert format_size(0) == "0 B"

    def test_drops_a_pointless_decimal(self) -> None:
        assert format_size(2 * 1024**3) == "2 GB"


class TestHostSuffix:
    def test_matches_a_bare_host(self) -> None:
        assert host_suffix("hubcdn.club", "hubcdn.club")

    def test_matches_a_subdomain(self) -> None:
        assert host_suffix("dl.hubcdn.wiki", "hubcdn.wiki")

    def test_rejects_a_lookalike(self) -> None:
        assert not host_suffix(
            "hubcdn.club.evil.test",
            "hubcdn.club",
        )


class TestVerifyMedia:
    def test_accepts_a_real_file(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _media_response(
                request,
                "Real.Movie.mkv",
                50_000_000,
            )

        link = verify_media(
            _client(handler),
            "https://cdn.test/Real.Movie.mkv",
        )

        assert link is not None
        assert link.filename == "Real.Movie.mkv"
        assert link.size_bytes == 50_000_000
        assert link.container == "matroska"

    def test_rejects_an_html_mirror(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="<html><body>click here</body></html>",
                request=request,
            )

        assert (
            verify_media(
                _client(handler),
                "https://mirror.test/x",
            )
            is None
        )

    def test_the_range_header_asks_for_exactly_the_probe_size(
        self,
    ) -> None:
        """
        A prefix read keeps probing cheap: without the range the
        server would stream the whole object. The end of a range is
        inclusive, so the header asks for one byte fewer than
        :data:`PROBE_BYTES`, not one more.
        """

        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return _media_response(request, "a.mkv", 900)

        verify_media(_client(handler), "https://cdn.test/a.mkv")

        assert unlock_module.PROBE_BYTES == 4096
        assert seen["range"] == (
            f"bytes=0-{unlock_module.PROBE_BYTES - 1}"
        )
        assert seen["range"] == "bytes=0-4095"

    def test_sends_a_referer_when_given_one(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return _media_response(request, "a.mkv", 900)

        verify_media(
            _client(handler),
            "https://cdn.test/a.mkv",
            referer="https://gamerxyt.com/hubcloud.php",
        )

        assert "gamerxyt.com" in seen["referer"]

    def test_invents_a_filename_when_the_server_omits_one(
        self,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                206,
                headers={"content-range": "bytes 0-4095/900"},
                content=MATROSKA_PREFIX,
                request=request,
            )

        link = verify_media(
            _client(handler),
            "https://cdn.test/path/Total.Silence.2025.mkv",
        )

        assert link is not None
        assert link.filename == "Total.Silence.2025.mkv"

    def test_uses_a_generic_name_for_an_opaque_path(
        self,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                206,
                headers={"content-range": "bytes 0-4095/900"},
                content=MATROSKA_PREFIX,
                request=request,
            )

        link = verify_media(
            _client(handler),
            "https://cdn.test/dl/2f9a1c",
        )

        assert link is not None
        assert link.filename == "video.mkv"

    def test_an_error_response_is_not_a_file(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404,
                text="gone",
                request=request,
            )

        assert (
            verify_media(
                _client(handler),
                "https://mirror.test/missing",
            )
            is None
        )

    def test_ignores_a_content_length_that_only_covers_the_prefix(
        self,
    ) -> None:
        """
        A ``206`` carrying no ``Content-Range`` has told us the size
        of the body it sent, not of the object. Reporting that as the
        file size would be a lie.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                206,
                headers={"content-length": "4097"},
                content=MATROSKA_PREFIX,
                request=request,
            )

        link = verify_media(
            _client(handler),
            "https://cdn.test/a.mkv",
        )

        assert link is not None
        assert link.size_bytes is None

    def test_reports_a_small_content_length_with_no_content_range(
        self,
    ) -> None:
        """
        A length too short to be a prefix can only be the whole
        object, so it is a real size and worth reporting.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                206,
                headers={"content-length": "900"},
                content=MATROSKA_PREFIX,
                request=request,
            )

        link = verify_media(
            _client(handler),
            "https://cdn.test/a.mkv",
        )

        assert link is not None
        assert link.size_bytes == 900

    def test_the_content_range_total_wins_over_the_length(
        self,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                206,
                headers={
                    "content-range": "bytes 0-4095/123456789",
                    "content-length": "4096",
                },
                content=MATROSKA_PREFIX,
                request=request,
            )

        link = verify_media(
            _client(handler),
            "https://cdn.test/a.mkv",
        )

        assert link is not None
        assert link.size_bytes == 123_456_789

    def test_reads_the_whole_prefix_not_just_the_first_chunk(
        self,
    ) -> None:
        """
        ``iter_bytes`` makes no promise about chunk sizes, so a
        transport handing over a few bytes at a time must not be
        allowed to hide the MP4 brand sitting at offset eight.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                206,
                headers={"content-range": "bytes 0-4095/7000"},
                stream=_Dribble(MP4_PREFIX),
                request=request,
            )

        link = verify_media(
            _client(handler),
            "https://cdn.test/a.mp4",
        )

        assert link is not None
        assert link.container == "mp4"

    def test_a_body_that_stops_early_is_not_a_file(self) -> None:
        """
        A transfer that breaks part-way through the prefix leaves
        the question open. Answering "yes" on half the evidence is
        the one mistake this module cannot make.
        """

        class Truncated(httpx.SyncByteStream):
            def __iter__(self):
                yield MP4_PREFIX[:8]
                raise httpx.ReadError("connection reset")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                206,
                headers={"content-range": "bytes 0-4095/7000"},
                stream=Truncated(),
                request=request,
            )

        assert (
            verify_media(
                _client(handler),
                "https://mirror.test/a.mp4",
            )
            is None
        )

    def test_a_prefix_longer_than_the_probe_is_truncated(
        self,
    ) -> None:
        """
        A server that ignored the range may start the whole file.
        Only the probe is read, so a two-megabyte answer to a 4 KB
        request costs 4 KB.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=(
                    MATROSKA_PREFIX
                    + b"\x00"
                    * (4 * unlock_module.PROBE_BYTES)
                ),
                request=request,
            )

        link = verify_media(
            _client(handler),
            "https://cdn.test/a.mkv",
        )

        assert link is not None
        assert link.container == "matroska"
        assert link.size_bytes is None

    def test_a_wav_probe_is_saved_as_audio(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                206,
                headers={
                    "content-range": "bytes 0-4095/88000",
                    "content-disposition": "attachment; filename=x",
                },
                content=_riff_prefix(b"WAVE"),
                request=request,
            )

        link = verify_media(
            _client(handler),
            "https://cdn.test/track",
        )

        assert link is not None
        assert link.container == "wav"
        assert link.filename == "video.wav"

    def test_a_probe_failure_names_its_reason(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        Callers depend on the None, but a None alone leaves them
        telling the user the host returned web pages even when the
        host never answered at all. The reason is recorded and
        logged, so the diagnosis survives.

        The retries wrap three timeouts into one ``NetworkError``
        before this sees it, so the type named here is that one, not
        the ``ConnectTimeout`` underneath it.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("connection timed out")

        reasons: list[str] = []

        with caplog.at_level(
            logging.DEBUG,
            logger="hdhub4u.unlock",
        ):
            link = verify_media(
                _client(handler),
                "https://cdn.test/a.mkv",
                reasons=reasons,
            )

        assert link is None
        assert len(reasons) == 1
        assert "timed out" in reasons[0]
        assert "cdn.test/a.mkv" in caplog.text

    def test_a_web_page_is_logged_as_content_not_transport(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        The same None, a genuinely different reason: the bytes
        arrived and they were not a container.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="<html>buy something</html>",
                request=request,
            )

        with caplog.at_level(
            logging.DEBUG,
            logger="hdhub4u.unlock",
        ):
            link = verify_media(
                _client(handler),
                "https://mirror.test/a.mkv",
            )

        assert link is None
        assert "container" in caplog.text
        assert "ConnectTimeout" not in caplog.text


class TestReadProbePrefix:
    def test_assembles_a_body_delivered_in_pieces(self) -> None:
        """
        A range-limited response holds the whole prefix, and a
        transport may hand it over in any number of pieces. Every
        one of them is needed.
        """

        body = _Dribble(MATROSKA_PREFIX)

        response = httpx.Response(206, stream=body)

        prefix, complete = unlock_module._read_probe_prefix(response)

        assert prefix == MATROSKA_PREFIX
        assert complete is True

    def test_reads_no_further_than_the_probe_size(self) -> None:
        body = _Dribble(MATROSKA_PREFIX + b"\x00" * 8_192)

        response = httpx.Response(200, stream=body)

        prefix, complete = unlock_module._read_probe_prefix(response)

        assert len(prefix) == unlock_module.PROBE_BYTES
        assert body.served <= unlock_module.PROBE_BYTES + 64
        assert complete is True

    def test_a_range_ignoring_server_costs_one_probe(self) -> None:
        """
        The regression that made the byte cap inert. Probing used
        ``client.get``, which buffers the whole body before this
        function saw a byte, so a mirror serving a two megabyte
        object transferred all of it -- and a mirror serving four
        gigabytes under a video content type would have done the
        same, into memory.

        This drives the real client so that the count is of bytes
        off the wire, not bytes this function chose to keep.
        """

        class Endless(httpx.SyncByteStream):
            def __init__(self) -> None:
                self.served = 0

            def __iter__(self):
                block = b"\x00" * 65_536
                while self.served < 2 * 1024 * 1024:
                    self.served += len(block)
                    yield block

        for status, headers in (
            (206, {"content-range": "bytes 0-4095/2097152"}),
            (200, {"content-length": "2097152"}),
        ):
            body = Endless()

            def handler(
                request: httpx.Request,
                body: Endless = body,
                status: int = status,
                headers: dict[str, str] = headers,
            ) -> httpx.Response:
                return httpx.Response(
                    status,
                    headers=headers,
                    stream=body,
                    request=request,
                )

            client = httpx.Client(
                transport=httpx.MockTransport(handler),
                follow_redirects=True,
            )

            verify_media(client, "https://cdn.test/big.mkv")

            # One chunk of slack: the reader is allowed the piece it
            # is in the middle of, not a whole body.
            assert body.served <= 65_536, status
            assert body.served < 2 * 1024 * 1024, status

    def test_a_short_body_reads_cleanly(self) -> None:
        """
        A body that ends inside the probe is not a failure. The flag
        reports whether the transfer broke, not whether the object
        filled the window, and a small file legitimately does not.
        """

        body = _Dribble(MATROSKA_PREFIX)

        response = httpx.Response(200, stream=body)

        prefix, complete = unlock_module._read_probe_prefix(response)

        assert prefix == MATROSKA_PREFIX
        assert complete is True

    def test_a_broken_body_reads_as_nothing(self) -> None:
        class Broken(httpx.SyncByteStream):
            def __iter__(self):
                yield MATROSKA_PREFIX
                raise httpx.ReadError("connection reset")

        response = httpx.Response(206, stream=Broken())

        prefix, complete = unlock_module._read_probe_prefix(response)

        assert prefix == b""
        assert complete is False


def _wrapped(file_url: str) -> str:
    """
    Build the base64 wrapper the hubcdn gate uses.

    One decode only. The decoded text is a URL whose ``link``
    parameter is already the plain file address.
    """

    return base64.b64encode(
        f"https://hubcdn.club/dl/?link={file_url}".encode()
    ).decode()


HUB_CDN_HTML = (
    '<html><script>var reurl = '
    '"https://inventoryidea.com/?r='
    + _wrapped(
        "https://pub-634337561459459daa49b2489f10d39e.r2.dev/"
        "Death.of.a.Unicorn.2025.mkv"
    )
    + '";</script></html>'
)


def _hubcdn_handler(request: httpx.Request) -> httpx.Response:
    """Answer the hubcdn gate and the object it points at."""

    if request.headers.get("range"):
        return _media_response(
            request,
            "Death.of.a.Unicorn.2025.mkv",
            1234,
        )

    return httpx.Response(
        200,
        text=HUB_CDN_HTML,
        request=request,
    )


#: A real host: the base64 wrapping it holds both ``+`` and ``/``,
#: which is exactly what standard base64 emits and what
#: urlsafe base64 would have rewritten.
_TRICKY_FILE_URL = (
    "https://pub-634337561459459daa49b2489f10d39e.r2.dev/"
    "Ba+Dha-Dhoom2a~2025.1080p.BluRay.mkv"
)


class TestDecodeWrappedUrl:
    def test_unwraps_a_plain_payload(self) -> None:
        wrapped = (
            f"https://inventoryidea.com/?r={_wrapped('https://x.test/a.mkv')}"
        )

        assert (
            unlock_module._decode_wrapped_url(wrapped)
            == "https://x.test/a.mkv"
        )

    def test_keeps_a_plus_and_a_slash_intact(self) -> None:
        """
        ``parse_qs`` runs ``unquote_plus``, so a raw ``+`` in the
        value becomes a space. That keeps the payload's length, so
        ``b64decode`` goes on to accept it and returns shifted
        rubbish instead of failing — and the real address is lost.
        """

        blob = _wrapped(_TRICKY_FILE_URL)

        assert "+" in blob, "the fixture must exercise the bug"
        assert "/" in blob, "the fixture must exercise the bug"

        wrapped = f"https://inventoryidea.com/?r={blob}"

        assert (
            unlock_module._decode_wrapped_url(wrapped)
            == _TRICKY_FILE_URL
        )

    def test_keeps_an_encoded_plus_intact(self) -> None:
        """The live gate percent-encodes ``+``; that must work too."""

        blob = quote(_wrapped(_TRICKY_FILE_URL), safe="")

        wrapped = f"https://inventoryidea.com/?r={blob}"

        assert (
            unlock_module._decode_wrapped_url(wrapped)
            == _TRICKY_FILE_URL
        )

    def test_reads_the_parameter_before_another_key(self) -> None:
        blob = _wrapped("https://x.test/a.mkv")

        wrapped = f"https://inventoryidea.com/?id=7&r={blob}"

        assert (
            unlock_module._decode_wrapped_url(wrapped)
            == "https://x.test/a.mkv"
        )

    def test_a_missing_parameter_raises(self) -> None:
        with pytest.raises(ResolutionError):
            unlock_module._decode_wrapped_url(
                "https://inventoryidea.com/?id=7"
            )

    def test_a_payload_that_is_not_base64_raises(self) -> None:
        with pytest.raises(ResolutionError):
            unlock_module._decode_wrapped_url(
                "https://inventoryidea.com/?r=@@@@"
            )

    def test_a_payload_with_no_link_raises(self) -> None:
        blob = base64.b64encode(b"nothing to see").decode()

        with pytest.raises(ResolutionError):
            unlock_module._decode_wrapped_url(
                f"https://inventoryidea.com/?r={blob}"
            )


HUBDRIVE_FILE_PAGE = """
<html><body>
  <a href="https://hubcloud.ist/drive/abc123">Download</a>
</body></html>
"""

HUBDRIVE_DRIVE_PAGE = """
<html><body>
  <a href="https://gamerxyt.com/hubcloud.php?data=xyz&amp;token=t1">
      click here</a>
</body></html>
"""

HUBDRIVE_HTML = """
<html><body>
  <a href="https://cdn.valentine.guru/Dead.Mirror.mkv">
      Download Mirror 1</a>
  <a href="https://gpdl.example.test/file/12">Download Mirror 2</a>
  <a href="https://pixeldrain.example.test/u/abc">Mirror 3</a>
</body></html>
"""

#: A listing whose mirrors are relative. The live site writes them
#: out in full, but a listing can be served from anywhere and
#: nothing guarantees it.
HUBDRIVE_RELATIVE_HTML = """
<html><body>
  <a href="/files/Real.One.mkv">Download Mirror 1</a>
  <a href="?alt=1">Download Mirror 2</a>
</body></html>
"""

#: A drive page whose listing link carries an escaped ampersand, and
#: a value that must survive the round trip untouched.
HUBDRIVE_ESCAPED_DRIVE_PAGE = (
    '<html><body><a href="https://gamerxyt.com/hubcloud.php'
    '?data=a%2Bb%2Bc&amp;token=t9">click here</a></body></html>'
)


def _hubdrive_chain_handler(
    drive_page: str = HUBDRIVE_DRIVE_PAGE,
    listing: str = HUBDRIVE_HTML,
    seen: list[str] | None = None,
):
    """Answer the hubdrive chain with the pages given."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)

        if seen is not None:
            seen.append(url)

        if "hubdrive.pics" in url:
            return httpx.Response(
                200,
                text=HUBDRIVE_FILE_PAGE,
                request=request,
            )

        if "hubcloud.ist" in url:
            return httpx.Response(
                200,
                text=drive_page,
                request=request,
            )

        if "hubcloud.php" in url:
            return httpx.Response(
                200,
                text=listing,
                request=request,
            )

        if "valentine.guru" in url:
            return httpx.Response(
                200,
                text="<html>mirror is an ad page</html>",
                request=request,
            )

        return _media_response(request, "Real.One.mkv", 7777)

    return handler


def _hubdrive_handler(request: httpx.Request) -> httpx.Response:
    """Walk the three-page hubdrive chain to a mirror listing."""

    url = str(request.url)

    if "hubdrive.pics" in url:
        return httpx.Response(
            200,
            text=HUBDRIVE_FILE_PAGE,
            request=request,
        )

    if "hubcloud.ist" in url:
        return httpx.Response(
            200,
            text=HUBDRIVE_DRIVE_PAGE,
            request=request,
        )

    if "gamerxyt.com" in url:
        return httpx.Response(
            200,
            text=HUBDRIVE_HTML,
            request=request,
        )

    if "valentine.guru" in url:
        return httpx.Response(
            200,
            text="<html>mirror is an ad page</html>",
            request=request,
        )

    return _media_response(request, "Real.One.mkv", 7777)


class TestUnlockHubCdn:
    def test_resolves_to_the_object_behind_the_gates(
        self,
    ) -> None:
        """
        The gate nests a base64 link two levels deep. Only the
        innermost object is a downloadable file.
        """

        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            seen.append(url)

            if request.headers.get("range"):
                return _media_response(
                    request,
                    "Death.of.a.Unicorn.2025.mkv",
                    1234,
                )

            return httpx.Response(
                200,
                text=HUB_CDN_HTML,
                request=request,
            )

        link = unlock_hubcdn(
            _client(handler),
            _option("https://hubcdn.club/file/ABC"),
        )

        assert link.strategy == "hubcdn"
        assert link.filename == "Death.of.a.Unicorn.2025.mkv"
        assert "r2.dev" in link.url
        assert seen[0] == "https://hubcdn.club/file/ABC"

    def test_a_broken_gate_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="<html>no reurl here</html>",
                request=request,
            )

        with pytest.raises(ResolutionError) as info:
            unlock_hubcdn(
                _client(handler),
                _option("https://hubcdn.club/file/ABC"),
            )

        assert info.value.url == "https://hubcdn.club/file/ABC"

    def test_a_deleted_file_is_named_as_such(self) -> None:
        """
        The site keeps the listing up after the file behind it is
        gone. Telling the user to retry that link would be wrong, so
        the two cases must not share a message.
        """

        dead = (
            "No URL found.Failed to get data from gserver"
            "<html><head><title>Something went wrong</title>"
            "</head><body><h4>File is Deleted or UnAvailable...."
            "Re.Trying to Reload Page</h4></body></html>"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=dead,
                request=request,
            )

        with pytest.raises(ResolutionError) as info:
            unlock_hubcdn(
                _client(handler),
                _option("https://hubcdn.wiki/file/ABC"),
            )

        message = str(info.value).lower()

        assert "gone" in message
        assert "layout" not in message

    def test_an_object_that_is_not_media_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.headers.get("range"):
                return httpx.Response(
                    200,
                    text="<html>not media</html>",
                    request=request,
                )

            return httpx.Response(
                200,
                text=HUB_CDN_HTML,
                request=request,
            )

        with pytest.raises(ResolutionError):
            unlock_hubcdn(
                _client(handler),
                _option("https://hubcdn.club/file/ABC"),
            )


class TestUnlockHubDrive:
    def test_keeps_the_first_mirror_that_really_serves_bytes(
        self,
    ) -> None:
        """
        The listing offers several mirrors and the first is
        routinely an HTML page, so the bytes decide.
        """

        link = unlock_hubdrive(
            _client(_hubdrive_handler),
            _option("https://hubdrive.pics/file/BBB"),
        )

        assert link.strategy == "hubdrive"
        assert link.filename == "Real.One.mkv"

    def test_a_chain_that_never_reaches_a_mirror_raises(
        self,
    ) -> None:
        """
        The pages load but no listed mirror serves bytes. That is a
        dead link, not a transport failure, and must be reported as
        such rather than retried.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)

            if "hubdrive.pics" in url:
                return httpx.Response(
                    200,
                    text=HUBDRIVE_FILE_PAGE,
                    request=request,
                )

            if "hubcloud.ist" in url:
                return httpx.Response(
                    200,
                    text=HUBDRIVE_DRIVE_PAGE,
                    request=request,
                )

            if "gamerxyt.com" in url:
                return httpx.Response(
                    200,
                    text=HUBDRIVE_HTML,
                    request=request,
                )

            return httpx.Response(
                200,
                text="<html>every mirror is an ad page</html>",
                request=request,
            )

        with pytest.raises(ResolutionError) as info:
            unlock_hubdrive(
                _client(handler),
                _option("https://hubdrive.pics/file/BBB"),
            )

        assert "mirror" in str(info.value).lower()

    def test_a_file_page_without_a_drive_link_raises(
        self,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="<html>nothing here anymore</html>",
                request=request,
            )

        with pytest.raises(ResolutionError):
            unlock_hubdrive(
                _client(handler),
                _option("https://hubdrive.pics/file/BBB"),
            )

    def test_resolves_a_relative_mirror_against_the_listing(
        self,
    ) -> None:
        """
        A relative href is only meaningful against the page it was
        read from. Probing it as-is raises a protocol error, which
        reads exactly like a mirror serving a web page — so a
        perfectly good mirror gets written off as an ad page.
        """

        seen: list[str] = []

        link = unlock_hubdrive(
            _client(
                _hubdrive_chain_handler(
                    listing=HUBDRIVE_RELATIVE_HTML,
                    seen=seen,
                )
            ),
            _option("https://hubdrive.pics/file/BBB"),
        )

        assert link.url == "https://gamerxyt.com/files/Real.One.mkv"
        assert link.filename == "Real.One.mkv"
        assert "https://gamerxyt.com/files/Real.One.mkv" in seen

    def test_a_query_only_mirror_stays_on_the_listing_host(
        self,
    ) -> None:
        mirrors = unlock_module._download_mirrors(
            HUBDRIVE_RELATIVE_HTML,
            "https://gamerxyt.com/hubcloud.php?data=xyz&token=t1",
        )

        assert [url for _, url in mirrors] == [
            "https://gamerxyt.com/files/Real.One.mkv",
            "https://gamerxyt.com/hubcloud.php?alt=1",
        ]

    def test_unescapes_an_escaped_listing_link(self) -> None:
        """
        The link is matched out of raw markup, where ``&`` is
        written ``&amp;``. Undoing that is an HTML step and has to
        happen before the value is read as a URL.
        """

        seen: list[str] = []

        handler = _hubdrive_chain_handler(
            drive_page=HUBDRIVE_ESCAPED_DRIVE_PAGE,
            seen=seen,
        )

        link = unlock_hubdrive(
            _client(handler),
            _option("https://hubdrive.pics/file/BBB"),
        )

        listing = [
            url
            for url in seen
            if "hubcloud.php" in url and "gamerxyt" in url
        ]

        assert listing, "the listing page was never fetched"
        assert "&amp;" not in listing[0]
        assert "data=a+b+c&token=t9" in listing[0]
        assert link.strategy == "hubdrive"


class TestUnlockMany:
    def test_pairs_each_option_with_its_outcome(self) -> None:
        """
        One dead mirror must not hide the rest of the menu, so a
        failure is reported beside its option instead of raised.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)

            if "hubdrive.pics" in url:
                return httpx.Response(
                    200,
                    text=HUBDRIVE_FILE_PAGE,
                    request=request,
                )

            if "hubcloud.ist" in url:
                return httpx.Response(
                    200,
                    text=HUBDRIVE_DRIVE_PAGE,
                    request=request,
                )

            if "gamerxyt.com" in url:
                return httpx.Response(
                    200,
                    text=HUBDRIVE_HTML,
                    request=request,
                )

            if (
                "valentine.guru" in url
                or "gpdl.example.test" in url
                or "pixeldrain.example.test" in url
            ):
                return httpx.Response(
                    200,
                    text="<html>every mirror is an ad page</html>",
                    request=request,
                )

            if "r2.dev" in url:
                return _media_response(
                    request,
                    "Death.of.a.Unicorn.2025.mkv",
                    1234,
                )

            return httpx.Response(
                200,
                text=HUB_CDN_HTML,
                request=request,
            )

        results = unlock_many(
            [
                _option("https://hubdrive.pics/file/B"),
                _option("https://hubcdn.club/file/A"),
            ],
            client=_client(handler),
        )

        assert len(results) == 2

        first_option, first = results[0]
        second_option, second = results[1]

        assert first_option.url.endswith("/file/B")
        assert isinstance(first, ResolutionError)

        assert second_option.url.endswith("/file/A")
        assert isinstance(second, UnlockedLink)
        assert second.filename == "Death.of.a.Unicorn.2025.mkv"

    def test_an_empty_list_needs_no_requests(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not reach the network")

        assert unlock_many([], client=_client(handler)) == []

    def test_an_unexpected_failure_does_not_hide_later_options(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The contract is one bad option, one reported failure. Only
        the two expected error types were caught, so anything else
        — a bug, a timeout that escaped as itself — took the rest of
        the menu down with it.
        """

        options = [
            _option("https://hubcdn.club/file/A"),
            _option("https://hubcdn.club/file/B"),
            _option("https://hubcdn.club/file/C"),
        ]

        def resolve(option, *, client=None):
            if option.url.endswith("/file/A"):
                raise ValueError("the gate did not look like this")

            return UnlockedLink(
                url=f"https://r2.dev/{option.url[-1]}.mkv",
                filename=f"{option.url[-1]}.mkv",
                host="r2.dev",
                strategy="direct",
            )

        monkeypatch.setattr(unlock_module, "unlock", resolve)

        results = unlock_many(
            options,
            client=_client(_nothing_to_see),
        )

        assert len(results) == 3

        _, first = results[0]
        _, second = results[1]
        _, third = results[2]

        assert isinstance(first, ResolutionError)
        assert isinstance(second, UnlockedLink)
        assert isinstance(third, UnlockedLink)

    def test_a_reported_failure_keeps_its_cause(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Reporting the failure must not lose what it was: the
        replacement is raised from the original, so the traceback
        still explains itself.
        """

        def resolve(option, *, client=None):
            raise NetworkError("the gate timed out")

        monkeypatch.setattr(unlock_module, "unlock", resolve)

        results = unlock_many(
            [_option("https://hubcdn.club/file/A")],
            client=_client(_nothing_to_see),
        )

        _, outcome = results[0]

        assert isinstance(outcome, ResolutionError)
        assert "timed out" in str(outcome)
        assert isinstance(outcome.__cause__, NetworkError)

    def test_an_interrupt_is_not_reported_as_a_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Asking to stop is not a per-option failure. Reporting it as
        one would turn a Ctrl-C into a list of dead links.
        """

        def resolve(option, *, client=None):
            raise KeyboardInterrupt

        monkeypatch.setattr(unlock_module, "unlock", resolve)

        with pytest.raises(KeyboardInterrupt):
            unlock_many(
                [_option("https://hubcdn.club/file/A")],
                client=_client(_nothing_to_see),
            )


class TestLinkCacheIsUsed:
    """
    The cache is an optimisation, so every one of these tests is also a
    test that nothing about the answer depends on it being there.
    """

    def _cache(self, tmp_path):
        from hdhub4u.link_cache import LinkCache

        return LinkCache(tmp_path / "links.json")

    def test_a_resolution_is_stored(
        self,
        tmp_path,
    ) -> None:
        option = _option("https://hubcdn.club/file/A")
        cache = self._cache(tmp_path)

        unlock(option, client=_client(_hubcdn_handler), cache=cache)

        entry = cache.get(option.url)

        assert entry is not None
        assert entry.final_url.endswith(".mkv")

    def test_a_second_run_skips_the_gate(
        self,
        tmp_path,
    ) -> None:
        """
        The whole point of the cache: a repeat run does not walk the
        gate again. The handler refuses every request except the probe
        the cache hit still has to make.
        """

        option = _option("https://hubcdn.club/file/A")
        cache = self._cache(tmp_path)

        unlock(option, client=_client(_hubcdn_handler), cache=cache)

        def only_probe(request: httpx.Request) -> httpx.Response:
            if "Range" not in request.headers:
                raise AssertionError(
                    "the gate was walked again despite the cache"
                )

            return _media_response(request, "Dune.mkv", 900)

        link = unlock(
            option,
            client=_client(only_probe),
            cache=cache,
        )

        assert "cached" in link.strategy

    def test_a_dead_cache_entry_is_not_trusted(
        self,
        tmp_path,
    ) -> None:
        """
        These links are short-lived, so an hour-old entry is often
        dead. Using it hands the user a 403 at the download instead of
        the message they needed, so it is probed and, failing that,
        forgotten and resolved for real.
        """

        option = _option("https://hubcdn.club/file/A")
        cache = self._cache(tmp_path)

        cache.put(
            option.url,
            "https://r2.dev/gone.mkv",
            "video/x-matroska",
            "hubcdn",
        )

        def gone_is_gone(request: httpx.Request) -> httpx.Response:
            if "gone.mkv" in str(request.url):
                return httpx.Response(
                    403,
                    request=request,
                )

            return _hubcdn_handler(request)

        link = unlock(
            option,
            client=_client(gone_is_gone),
            cache=cache,
        )

        assert "cached" not in link.strategy
        assert link.url.endswith(".mkv")

    def test_the_cache_is_optional(
        self,
    ) -> None:
        """
        Tests must not share a cache, and neither should a caller who
        has not asked for one.
        """

        link = unlock(
            _option("https://hubcdn.club/file/A"),
            client=_client(_hubcdn_handler),
        )

        assert link.strategy == "hubcdn"

    def test_a_cache_write_failure_does_not_fail_the_download(
        self,
        tmp_path,
    ) -> None:
        """
        The disk being full, read-only or full of nonsense must not
        stop a file that is otherwise ready to arrive.
        """

        class Broken:
            """A cache whose disk has given up."""

            def get(self, url: str) -> None:
                return None

            def put(self, *args: object, **kwargs: object) -> None:
                raise OSError("no space left on device")

        link = unlock(
            _option("https://hubcdn.club/file/A"),
            client=_client(_hubcdn_handler),
            cache=Broken(),  # type: ignore[arg-type]
        )

        assert link.url.endswith(".mkv")


class TestUnlock:
    def test_routes_by_host(self) -> None:
        link = unlock(
            _option("https://hubcdn.club/file/A"),
            client=_client(_hubcdn_handler),
        )

        assert link.strategy == "hubcdn"

    def test_a_stream_option_is_refused(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("streams are not downloadable")

        with pytest.raises(ResolutionError) as info:
            unlock(
                _option(
                    "https://greenmotors.club/?id=x",
                    STREAMING_KIND,
                ),
                client=_client(handler),
            )

        assert "stream" in str(info.value).lower()

    def test_an_unknown_host_is_refused(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no strategy to try")

        with pytest.raises(ResolutionError):
            unlock(
                _option("https://example.com/file/1"),
                client=_client(handler),
            )

    def test_a_network_failure_falls_through_to_the_next_strategy(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        One host names one strategy today, but the loop must not be
        what decides that: a strategy failing says nothing about the
        next. The pair is driven here so the second gets its chance.
        """

        attempts: list[str] = []

        monkeypatch.setattr(
            unlock_module,
            "_iter_strategies",
            lambda host: iter(["hubcdn", "hubdrive"]),
        )

        def failing_gate(client, option):
            attempts.append("hubcdn")
            raise NetworkError("could not open the gate")

        def hubdrive_gate(client, option):
            attempts.append("hubdrive")
            return UnlockedLink(
                url="https://r2.dev/a.mkv",
                filename="a.mkv",
                host="r2.dev",
                strategy="direct",
            )

        # The dispatch table is built once at import, so the seam is
        # the table rather than the module attributes it was made
        # from.
        monkeypatch.setattr(
            unlock_module,
            "_STRATEGIES",
            {
                "hubcdn": failing_gate,
                "hubdrive": hubdrive_gate,
            },
        )

        link = unlock(
            _option("https://multi.host.test/file/A"),
            client=_client(_nothing_to_see),
        )

        assert attempts == ["hubcdn", "hubdrive"]
        assert link.strategy == "direct"

    def test_every_strategy_failing_reports_the_last_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Nothing resolved, so the caller gets one resolution error
        carrying the last strategy's message, which is more use than
        a bare "could not be opened". "Last", not "furthest":
        strategies are ordered by host, not by how far they got.
        """

        monkeypatch.setattr(
            unlock_module,
            "_iter_strategies",
            lambda host: iter(["hubcdn", "hubdrive"]),
        )

        def failing_gate(client, option):
            raise NetworkError("the gate timed out")

        monkeypatch.setattr(
            unlock_module,
            "_STRATEGIES",
            {
                "hubcdn": failing_gate,
                "hubdrive": failing_gate,
            },
        )

        with pytest.raises(ResolutionError) as info:
            unlock(
                _option("https://multi.host.test/file/A"),
                client=_client(_nothing_to_see),
            )

        assert "timed out" in str(info.value)
        assert info.value.url == "https://multi.host.test/file/A"

    def test_an_interrupt_is_not_swallowed(self) -> None:
        """
        Asking to stop is not a per-option failure. The strategy
        loop must let it through rather than report it as a dead
        link.
        """

        def interrupted(request: httpx.Request) -> httpx.Response:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            unlock(
                _option("https://hubcdn.club/file/A"),
                client=_client(interrupted),
            )


def _nothing_to_see(request: httpx.Request) -> httpx.Response:
    """Answer nothing, for a test that makes no request."""

    raise AssertionError("this test makes no request")

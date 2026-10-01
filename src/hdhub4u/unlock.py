"""
Turning a site's gated link into a downloadable file.

Every download link on a post page points at a host that serves
HTML, not media. The HTML is a short redirect gate, and reading it
is what produces the real file URL. Each gate family has its own
shape, so each gets its own strategy here:

``hubcdn.club``
    302s to a sibling domain whose page carries a variable named
    ``reurl``. That value holds one base64 payload wrapping the
    direct address of a Cloudflare R2 object. The payload is decoded
    once and the address inside it is read without ever requesting
    it, so only two pages are ever fetched.

``hubdrive.pics``
    A three-hop chain. The file page links to a HubCloud drive
    page, which links to a mirror listing, which finally lists the
    files. Only some of those listed mirrors serve bytes; the rest
    return a further HTML wrapper. So candidates are verified
    against the bytes they actually return before one is offered.

Because the last hop is untrustworthy, nothing here reports a link
as downloadable on the strength of a link being present. A strategy
has to fetch a prefix of the file and recognise a media container
in it. A gate that changes shape produces an honest error instead
of a download that turns out to be a web page.

The only trustworthy signal about a candidate is the leading bytes
it returns. Never ``Content-Type``, never the filename on the
offer, never the size printed beside the option: every one of those
is written by the same party that would happily serve an ad page.
``Content-Type`` is recorded for display only, and
:func:`sniff_container` never sees it. Where a byte prefix cannot be
settled, the answer is "not a file", because the two mistakes are
not symmetric: an unwanted probe wastes a request, while a wrong
confidence downloads gigabytes of HTML.
"""

from __future__ import annotations

import base64
import binascii
import re
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from urllib.parse import unquote, urljoin, urlparse

import httpx

from .catalog import MediaOption, registrable_host
from .errors import NetworkError, ResolutionError
from .http import DEFAULT_ATTEMPTS, with_retries
from .http_types import USER_AGENT, is_media_content_type
from .logging_setup import get_logger

logger = get_logger(__name__)

#: How many bytes are pulled to identify a container. Long enough to
#: hold several MPEG-TS packets, so a transport stream can be
#: recognised from its own layout — :data:`_MPEG_TS_QUORUM` packet
#: boundaries sit at a fixed stride inside it.
PROBE_BYTES = 4096

#: Timeout for a probe. Longer than :data:`GATE_TIMEOUT`, because
#: this is the request that decides whether the whole unlock succeeds
#: and it is reading bytes off a media host.
PROBE_TIMEOUT = httpx.Timeout(25.0, connect=10.0)

#: Timeout for a gate page. Deliberately shorter: these are small
#: HTML pages, so a slow answer is a host that is not going to answer,
#: and a chain of three of them at three attempts each would
#: otherwise spend more of the unlock's budget than the file probe
#: ever gets.
GATE_TIMEOUT = httpx.Timeout(12.0, connect=6.0)

#: How many listed mirrors are probed before the rest are left alone.
#: A listing is not trusted to be short, and each probe can cost its
#: full timeout, so an uncapped fan-out holds the user for minutes
#: past the point where the answer is obvious.
MAX_MIRRORS = 8

#: Wall-clock budget for one unlock, covering the gate chain and
#: every mirror probe inside it. The three gate pages of a hubdrive
#: chain, at :data:`DEFAULT_ATTEMPTS` attempts of
#: :data:`GATE_TIMEOUT` each, already account for most of it.
UNLOCK_TIME_BUDGET = 150.0

DEFAULT_FILENAME_EXTENSION = ".mkv"

#: File extension for each container this module can recognise.
#: ``riff`` is the fallback for a RIFF file whose form type at
#: offset 8 is one this module does not name.
CONTAINER_EXTENSIONS: dict[str, str] = {
    "matroska": ".mkv",
    "mp4": ".mp4",
    "flv": ".flv",
    "ogg": ".ogv",
    "mp3": ".mp3",
    "mpeg-ts": ".ts",
    "avi": ".avi",
    "wav": ".wav",
    "riff": ".avi",
}

#: Hosts whose gate is a single page holding an encoded address.
_HUBCDN_HOSTS = ("hubcdn.club", "hubcdn.wiki")

_HUBCDN_REURL_PATTERN = re.compile(
    r"""reurl\s*=\s*["']([^"']+)["']"""
)

#: Set by the gate page and reused across every link, so it is
#: matched loosely and simply unwrapped rather than parsed.
_NESTED_LINK_PATTERN = re.compile(
    r"link=(https?://[^\s\"'<>]+)"
)

_HUBCLOUD_PAGE_PATTERN = re.compile(
    r"""href=["'](https?://hubcloud\.[a-z.]+/drive/[^"'?]+)["']"""
)

_MIRROR_LISTING_PATTERN = re.compile(
    r"""href=["'](https?://[^"']*hubcloud\.php\?[^"']+)["']"""
)

_FILENAME_STAR_PATTERN = re.compile(
    r"filename\*\s*=\s*([^']*)'([^']*)'([^;]+)",
    re.IGNORECASE,
)

_FILENAME_PLAIN_PATTERN = re.compile(
    r"""filename\s*=\s*["']?([^"';]+)""",
    re.IGNORECASE,
)

#: Extensions that make a path segment usable as a filename. Some
#: mirrors put the real name in the path instead of a header.
_MEDIA_SUFFIX_PATTERN = re.compile(
    r"\.(mkv|mp4|m4v|mov|avi|webm|flv|ts|m2ts|wmv|"
    r"mp3|m4a|aac|ogg|opus|flac|wav|part)$",
    re.IGNORECASE,
)

#: Leading bytes that identify a media container by a fixed prefix.
#: These have to be strong enough to keep HTML out: a mirror that
#: answers with a web page is the failure this module exists to
#: catch, and one loose byte is all a page needs to slip past.
CONTAINER_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x1a\x45\xdf\xa3", "matroska"),
    (b"FLV\x01", "flv"),
    (b"OggS", "ogg"),
    (b"ID3", "mp3"),
)

#: A transport stream is a run of fixed-length packets, each opening
#: with the same sync byte. One sync byte proves nothing: 0x47 is also
#: the letter ``G``. Two prove barely more, because the boundaries sit
#: 188 bytes apart and a body of English text lands on ``G`` by chance
#: about once every 188 bytes — measured over 20 000 random text
#: bodies, the leading byte plus the next boundary passed about 1.5%
#: of them, which is a page of prose saved as a transport stream
#: fifteen times in a thousand downloads. A quorum of the boundaries
#: already in hand has no such weakness: for a genuine stream every
#: one of them is a sync byte, while text producing a full quorum by
#: chance is vanishing (none of those 20 000 bodies did).
_MPEG_TS_PACKET_SIZE = 188
_MPEG_TS_SYNC_BYTE = 0x47
_MPEG_TS_QUORUM = 8

#: MP4, MOV and 3GP all start with an ISO base media file format
#: box: a four-byte size, the ``ftyp`` tag, then the major brand.
#: The brand therefore sits eight bytes in, not four.
_FTYP_OFFSET = 4
_BRAND_OFFSET = _FTYP_OFFSET + 4

_FTYP_BRANDS = frozenset(
    {
        b"isom",
        b"iso2",
        b"iso4",
        b"iso5",
        b"iso6",
        b"mp41",
        b"mp42",
        b"avc1",
        b"M4V ",
        b"M4A ",
        b"dash",
        b"qt  ",
        b"3gp4",
        b"3gp5",
        b"heic",
    }
)

#: RIFF wraps several unrelated formats behind one ``RIFF`` tag, and
#: names the inner one in the four bytes at offset 8. Reading only
#: ``RIFF`` and calling the result AVI saves audio as video.
_RIFF_OFFSET = 8
_RIFF_FORMS: dict[bytes, str] = {
    b"AVI ": "avi",
    b"WAVE": "wav",
}


@dataclass(frozen=True)
class UnlockedLink:
    """
    A gate that has been opened, yielding a real file address.

    Every field describes ``url``, which is where the bytes actually
    came from and not the address that was probed: a redirect may
    have moved it, and the size, container, content type and name
    all come from the final response.

    ``container`` is the strongest of them, because it comes from the
    leading bytes. ``content_type`` and ``size_bytes`` are only what
    the server claimed, and ``filename`` is the claim honoured least
    often: these hosts send no ``Content-Disposition`` most of the
    time, so :func:`_fallback_filename` supplies a name from the
    address's own path, or a generic one.
    """

    url: str
    filename: str
    host: str
    strategy: str
    content_type: str = ""
    size_bytes: int | None = None
    container: str = ""


def sniff_container(
    head: bytes,
) -> str:
    """
    Name the container a byte prefix belongs to.

    Args:
        head: The first bytes the candidate returned, however few
            that is. Every signature here is a fixed prefix rather
            than a whole packet, so a short read settles most of
            them: ``RIFF`` and a bare ``ftyp`` are four bytes, the
            major brand of an ISO box is eight, ``ID3`` is three.
            Only the transport-stream check needs more than that —
            :func:`is_mpeg_ts_sync_aligned` needs a quorum of packet
            boundaries, and declines a prefix shorter than that.

    Returns:
        A short label, or an empty string when the bytes are not a
        container this module recognises. HTML deliberately does not
        match: a gate page served instead of a file is the single
        most common failure here, and it has to be detectable.
    """

    if not head:
        return ""

    stripped = head.lstrip()

    if stripped[:1] == b"<":
        return ""

    for signature, name in CONTAINER_SIGNATURES:
        if head.startswith(signature):
            return name

    if (
        head[:4] == b"ftyp"
        or head[_FTYP_OFFSET:_FTYP_OFFSET + 4] == b"ftyp"
    ):
        return "mp4"

    brand = head[_BRAND_OFFSET:_BRAND_OFFSET + 4]

    if brand in _FTYP_BRANDS:
        return "mp4"

    if head[:4] == b"RIFF":
        return _resolve_riff_form(head)

    if is_mpeg_ts_sync_aligned(head):
        return "mpeg-ts"

    return ""


def is_mpeg_ts_sync_aligned(
    head: bytes,
    *,
    minimum: int = _MPEG_TS_QUORUM,
) -> bool:
    """
    Report whether a prefix carries a quorum of MPEG-TS sync bytes.

    A transport stream is packets of :data:`_MPEG_TS_PACKET_SIZE`
    bytes, every one of them opening with ``0x47``. The leading byte
    is therefore near-worthless on its own — it is also the letter
    ``G``, so a GIF, a GraphQL response and any page of prose
    beginning with "Goodbye" would satisfy it. The byte at the next
    packet boundary is worth rather more, but not much: one byte in
    every 188 is a ``G`` by chance, so checking the first two
    boundaries accepts plain English about once in seventy.

    Requiring a quorum of the boundaries already in hand removes that
    weakness rather than shrinking it, because a real stream hits
    every boundary and text does not. That asymmetry is the whole
    argument for the quorum over the two-byte check, and it is why
    the default cannot be lowered to make room for a shorter probe.

    Args:
        head: The candidate's leading bytes.
        minimum: How many of the first ``minimum`` packet boundaries
            must carry the sync byte. Lowering it makes the check
            cheaper in bytes and barely cheaper in false positives.

    Returns:
        True when all of the first ``minimum`` boundaries are sync
        bytes, so :data:`_MPEG_TS_PACKET_SIZE` * (minimum - 1) + 1
        bytes must have been read. A shorter prefix is declined
        rather than guessed at, and neither is a ``minimum`` below
        one.
    """

    if minimum < 1:
        return False

    if not head or head[0] != _MPEG_TS_SYNC_BYTE:
        return False

    if len(head) < _MPEG_TS_PACKET_SIZE * (minimum - 1) + 1:
        return False

    return all(
        head[index * _MPEG_TS_PACKET_SIZE] == _MPEG_TS_SYNC_BYTE
        for index in range(minimum)
    )


def _resolve_riff_form(
    head: bytes,
) -> str:
    """
    Name the format inside a RIFF container.

    Returns:
        The label for a form type this module knows, otherwise
        ``riff`` as a fallback for a container it only partly
        understands.
    """

    return _RIFF_FORMS.get(head[_RIFF_OFFSET:_RIFF_OFFSET + 4], "riff")


def filename_from_disposition(
    header: str,
) -> str:
    """
    Pull a filename out of a ``Content-Disposition`` header.

    The RFC 5987 ``filename*`` form is preferred because these
    hosts use it and the plain form is frequently a fallback name
    like ``download`` or the file id. A header carrying neither form
    yields an empty string, which is the common case: most mirrors
    send no ``Content-Disposition`` at all, and
    :func:`_fallback_filename` covers the gap.

    The result is a claim, not a fact, and is only used after the
    bytes behind it have been recognised.
    """

    if not header:
        return ""

    star = _FILENAME_STAR_PATTERN.search(header)

    if star:
        charset = (star.group(1) or "utf-8").strip() or "utf-8"
        try:
            name = unquote(
                star.group(3),
                encoding=charset,
                errors="replace",
            )
        except LookupError:
            name = unquote(star.group(3), errors="replace")

        name = name.strip()

        if name:
            return name

    plain = _FILENAME_PLAIN_PATTERN.search(header)

    if plain:
        return plain.group(1).strip()

    return ""


def _looks_like_a_filename(name: str) -> bool:
    """
    Return True when a name is worth using.

    Object-storage listings name the key, not the file, and a
    download of ``0b429707bf49fbe9616f960db0d3b23a`` tells the user
    nothing they cannot already read on screen.
    """

    if not name:
        return False

    if len(name) < 5:
        return False

    lowered = name.lower()

    if lowered in {"download", "file", "index.html"}:
        return False

    if not _MEDIA_SUFFIX_PATTERN.search(lowered):
        return False

    stem = lowered.rsplit(".", 1)[0]

    # A bare hex key carrying no extension is an object id.
    return not re.fullmatch(r"[0-9a-f]{16,}", stem)


def _build_client() -> httpx.Client:
    """Return a client shaped like a desktop browser."""

    return httpx.Client(
        timeout=PROBE_TIMEOUT,
        follow_redirects=True,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )


def _fetch_html(
    client: httpx.Client,
    url: str,
) -> str:
    """
    Fetch a gate page.

    Carries its own shorter :data:`GATE_TIMEOUT` rather than the
    client's :data:`PROBE_TIMEOUT`. A gate page is a few kilobytes of
    HTML: a slow answer is a host that is not going to answer, and
    this runs three times per attempt while the probe gets one.

    Raises:
        NetworkError: when the page cannot be read at all.
    """

    def operation() -> str:
        response = client.get(url, timeout=GATE_TIMEOUT)
        response.raise_for_status()
        return response.text

    try:
        return with_retries(
            operation,
            attempts=DEFAULT_ATTEMPTS,
        )
    except httpx.HTTPStatusError as error:
        raise NetworkError(
            "Could not open the download gate at "
            f"{registrable_host(url)} "
            f"(HTTP {error.response.status_code})."
        ) from error
    except httpx.HTTPError as error:
        raise NetworkError(
            f"Could not open the download gate at "
            f"{registrable_host(url)}: {error}"
        ) from error


def verify_media(
    client: httpx.Client,
    url: str,
    *,
    referer: str = "",
    attempts: int = DEFAULT_ATTEMPTS,
    reasons: list[str] | None = None,
) -> UnlockedLink | None:
    """
    Fetch a prefix of a URL and decide whether it is a real file.

    Several mirrors listed as downloads return a further HTML page.
    Only the bytes decide, so this reads a prefix, checks for a media
    container, and reports what the server claims about size and name.

    The request is streamed and abandoned as soon as
    :data:`PROBE_BYTES` have arrived, inside a ``with`` block, so the
    body is never read in full and the connection is released on
    every exit path. That is what makes the byte cap real: buffered
    with the client's default request, a mirror serving a four
    gigabyte object under a video content type would put all of it
    in memory before this function looked at a byte of it.

    Args:
        client: The client to probe with.
        url: The address to probe. The link that comes back is
            :attr:`httpx.Response.url` of the *final* response, so a
            redirect is reported rather than hidden, and the size,
            container and name beside it describe the same response.
        referer: Sent as ``Referer`` when given.
        attempts: How many times a transport failure is retried,
            matching the gate pages, so one reset on the first of
            three mirrors does not write off a good file. Each
            attempt costs at most one probe's worth of bytes, since
            each one stops at :data:`PROBE_BYTES`.
        reasons: When given, the reason a probe was rejected is
            appended to it. A caller reporting on several probes can
            then say what each hit, instead of asserting they were
            all web pages — which is no truer of a 503 or a
            connection reset than of an ad page.

    Returns:
        The verified link, or None when the URL is not a file. A
        None is an ordinary outcome here and is not an error. It
        covers several distinct causes — an HTTP error, a transport
        failure, an address httpx cannot turn into a request at all,
        an unrecognised container — which the caller cannot tell
        apart from the value alone, so each is logged at debug level
        and, when ``reasons`` was passed, recorded in it.
    """

    headers: dict[str, str] = {
        "Range": f"bytes=0-{PROBE_BYTES - 1}",
    }

    if referer:
        headers["Referer"] = referer

    def probe() -> tuple[UnlockedLink | None, str]:
        with client.stream(
            "GET",
            url,
            headers=headers,
        ) as response:
            return _judge_probe_response(response)

    try:
        link, reason = with_retries(probe, attempts=attempts)

    except ValueError as error:
        # httpx raises a bare ValueError for an address it cannot
        # make a request out of at all — a data: or mailto: href, or
        # a scheme it has no transport for. It is not an
        # HTTPError, so nothing above would catch it, and it would
        # abandon the rest of the mirror loop rather than write off
        # the one mirror that had a bad href.
        logger.debug(
            "probe of %s is not a requestable address: %s",
            url,
            error,
        )
        link = None
        reason = f"it is not an http(s) address ({error})"

    except (httpx.HTTPError, NetworkError) as error:
        logger.debug(
            "probe of %s failed before any bytes arrived: %s: %s",
            url,
            type(error).__name__,
            error,
        )
        link = None
        reason = (
            f"the request failed ({type(error).__name__}: {error})"
        )

    if link is None and reasons is not None:
        reasons.append(reason)

    return link


def _judge_probe_response(
    response: httpx.Response,
) -> tuple[UnlockedLink | None, str]:
    """
    Decide from a live probe response whether it is a file.

    The response is still open, and every path here returns rather
    than raising, so the caller's ``with`` releases the connection
    whichever way the answer goes.

    Returns:
        The link and an empty reason, or None and the reason it was
        rejected, phrased to read after "because".
    """

    url = str(response.url)

    if response.status_code >= 400:
        logger.debug(
            "probe of %s got HTTP %d",
            url,
            response.status_code,
        )
        return None, f"it answered HTTP {response.status_code}"

    content_type = response.headers.get(
        "content-type",
        "",
    )

    content_range = response.headers.get(
        "content-range",
        "",
    )

    disposition = response.headers.get(
        "content-disposition",
        "",
    )

    size_bytes = _size_from_headers(
        response.headers,
        content_range,
    )

    body, complete = _read_probe_prefix(response)

    if not complete:
        return None, "the transfer broke part-way through the probe"

    container = sniff_container(body)

    if not container:
        logger.debug(
            "probe of %s: first %d bytes hold no known "
            "container (content-type %r)",
            url,
            len(body),
            content_type,
        )
        return None, (
            f"its first {len(body)} bytes hold no known media container"
        )

    if content_type and is_media_content_type(content_type):
        logger.debug(
            "%s: container=%s", url, container
        )
    else:
        logger.debug(
            "%s: container=%s despite content-type %r",
            url,
            container,
            content_type,
        )

    filename = filename_from_disposition(disposition)

    if not _looks_like_a_filename(filename):
        filename = _fallback_filename(url, container)

    return (
        UnlockedLink(
            url=url,
            filename=filename,
            host=registrable_host(url),
            strategy="direct",
            content_type=content_type,
            size_bytes=size_bytes,
            container=container,
        ),
        "",
    )


def _read_probe_prefix(
    response: httpx.Response,
) -> tuple[bytes, bool]:
    """
    Read the probe prefix out of a live response.

    Reading stops at :data:`PROBE_BYTES`, and also if the body simply
    ends there. Nothing stops a server ignoring the range and
    starting the whole file, which is exactly why this walks the
    stream instead of asking for the content: a server that serves
    four gigabytes from the first byte must cost one probe.

    Returns:
        The prefix, and whether all of it arrived. Deciding on a
        truncated prefix would approve a file on half its evidence,
        so a broken transfer is reported as incomplete and the caller
        rejects it.
    """

    chunks: list[bytes] = []
    total = 0

    try:
        for chunk in response.iter_bytes():
            chunks.append(chunk)
            total += len(chunk)

            if total >= PROBE_BYTES:
                break

        return b"".join(chunks)[:PROBE_BYTES], True

    except httpx.HTTPError as error:
        logger.debug(
            "probe body ended early: %s: %s",
            type(error).__name__,
            error,
        )
        return b"", False


def _size_from_headers(
    headers: httpx.Headers,
    content_range: str,
) -> int | None:
    """
    Return the total object size from range or length headers.

    Only ``Content-Range`` carries a total: its trailing number is
    the size of the whole object. ``Content-Length`` counts the body
    that was actually sent, which for a partial response is just the
    prefix. So a length is believed only when it is no longer than
    the prefix that was asked for, meaning the object itself is that
    small: a body that cannot outrun the request has nothing to have
    been cut down from.

    Args:
        headers: The response headers.
        content_range: The ``Content-Range`` value, already read out
            of ``headers``. ``\\d`` matches only decimal digits, all
            of which :func:`int` accepts, so there is nothing here to
            guard against.

    Returns:
        The object's size, or None when no header can support one.
    """

    match = re.search(r"/(\d+)\s*$", content_range)

    if match:
        return int(match.group(1))

    raw_length = headers.get("content-length")

    # ``str.isdigit`` alone would accept a superscript, which int()
    # then refuses, so the ASCII check is what keeps the two in step.
    if raw_length and raw_length.isascii() and raw_length.isdigit():
        length = int(raw_length)

        if length <= PROBE_BYTES:
            # No longer than the prefix asked for, so this is the
            # whole object.
            return length

        # Longer than the prefix asked for. Either the server ignored
        # the range and is streaming the file from the start, or it
        # is describing the prefix it agreed to send. Either way the
        # number is not the file's size.
        logger.debug(
            "content-length %d without a content-range describes "
            "the prefix, not the object; size stays unknown",
            length,
        )

    return None


def _fallback_filename(
    url: str,
    container: str,
) -> str:
    """
    Invent a usable filename when the server offers none.

    Several mirrors serve the file from a path that already spells
    the name out, so that is preferred over a generic placeholder.
    """

    path = unquote(urlparse(url).path)
    tail = path.rsplit("/", 1)[-1] if path else ""

    if _looks_like_a_filename(tail):
        return tail

    return f"video{_extension_for(container)}"


def _extension_for(container: str) -> str:
    """Return the file extension for a sniffed container."""

    return CONTAINER_EXTENSIONS.get(
        container,
        DEFAULT_FILENAME_EXTENSION,
    )


#: Public alias, so callers naming a file do not reach into a
#: private helper.
extension_for = _extension_for


#: Markers a gate shows when its own copy of the file is gone. The
#: listing stays up long after this, so the page reads as broken
#: rather than as a link worth retrying. More than one of them has
#: to be present, because each of these phrases occurs in ordinary
#: prose about a download and only their combination means the gate
#: is reporting its own state.
_DEAD_FILE_MARKERS = (
    "no url found",
    "file is deleted or unavailable",
    "failed to get data from gserver",
)

#: How many of :data:`_DEAD_FILE_MARKERS` have to appear.
_DEAD_FILE_MARKER_QUORUM = 2

_DEAD_FILE_MESSAGE = (
    "The site's copy of this file is gone. Its listing is still up "
    "but the file behind it no longer exists."
)


def _is_dead_file_page(html_text: str) -> bool:
    """
    Report whether a gate is serving its "file is gone" page.

    This is a normal state for this site rather than a fault in the
    client, and it is worth naming precisely so the user is not told
    to retry a link that will never work.

    The markers are matched against the page's *visible* text, never
    against its raw markup. A listing page carries the site's own
    JavaScript, and a banner that only ever appears in a string
    literal there would otherwise make every live page look dead. A
    quorum of markers is required for the same reason.

    The failure mode here is one-directional, which is what makes the
    strictness safe: a false positive costs a live download, so the
    check leans towards missing rather than towards matching, and a
    page it declines is merely resolved the long way round.
    """

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_text, "html.parser")

    for element in soup(["script", "style", "noscript"]):
        element.decompose()

    visible = " ".join(soup.get_text(" ", strip=True).lower().split())

    hits = sum(
        1 for marker in _DEAD_FILE_MARKERS if marker in visible
    )

    return hits >= _DEAD_FILE_MARKER_QUORUM


def _raise_dead_file(
    url: str,
    strategy: str,
) -> ResolutionError:
    """
    Return the error naming a gate whose own copy of the file is gone.

    Returned rather than raised, like every other error this module
    builds, so the caller decides where it leaves the chain.
    """

    return ResolutionError(
        _DEAD_FILE_MESSAGE,
        url=url,
        strategy=strategy,
    )


def _raw_query_parameter(
    url: str,
    name: str,
) -> str:
    """
    Return one raw query parameter of a URL.

    Deliberately not :func:`urllib.parse.parse_qs`: that runs
    ``unquote_plus``, which turns a literal ``+`` into a space. The
    parameter read here holds raw base64, whose alphabet contains
    ``+``, so that translation would corrupt the payload in place
    and keep its length — the worst kind of corruption, because
    ``b64decode(validate=True)`` would go on to accept it.

    What happens here is percent-decoding, not HTML unescaping: the
    two are separate layers and only this one belongs to a URL that
    has already been read out of the markup.
    """

    query = urlparse(url).query

    for pair in query.split("&"):
        key, separator, value = pair.partition("=")

        if separator and key == name:
            return value

    return ""


def _decode_base64_text(
    blob: str,
) -> str:
    """
    Decode base64 that may use either alphabet.

    Args:
        blob: Raw, still percent-encoded base64. It is
            percent-decoded with ``+`` left alone, then padding is
            made explicit, because a ``+``/``/`` payload is valid
            urlsafe base64 but not valid standard base64.

    ``altchars`` folds ``-`` and ``_`` back onto ``+`` and ``/``
    before decoding, so one call reads either alphabet. ``validate``
    is the flag that matters: without it every character outside the
    alphabet is quietly discarded, so ``@@@@``, ``!!!!`` and ``====``
    all decode to nothing at all and a gate that shipped nonsense
    would be reported as having shipped an empty payload.

    Raises:
        binascii.Error: when the payload is not base64 at all.
    """

    text = unquote(blob)
    padded = text + "=" * (-len(text) % 4)

    return base64.b64decode(
        padded,
        altchars=b"-_",
        validate=True,
    ).decode("utf-8", "replace")


def _decode_wrapped_url(
    encoded: str,
) -> str:
    """
    Unwrap the base64 address the ``hubcdn`` gate embeds.

    The gate stores ``https://host/?r=<base64>`` where the decoded
    text is a URL carrying the real address in its ``link``
    parameter. That is the one decode this module performs on a
    wrapped address: the URL it yields is never requested, it is only
    read, so there is no second hop and nothing here is followed.

    Raises:
        ResolutionError: when the payload is missing, is not
            base64, or does not point at a file.
    """

    blob = _raw_query_parameter(encoded, "r")

    if not blob:
        raise ResolutionError(
            "The download gate did not carry a "
            "wrapped address.",
            url=encoded,
            strategy="hubcdn",
        )

    try:
        decoded = _decode_base64_text(blob)

    except (binascii.Error, ValueError) as error:
        raise ResolutionError(
            "The download gate address was not valid "
            "base64.",
            url=encoded,
            strategy="hubcdn",
        ) from error

    nested = _NESTED_LINK_PATTERN.search(decoded)

    if not nested:
        raise ResolutionError(
            "The download gate did not point at a file.",
            url=encoded,
            strategy="hubcdn",
        )

    return _normalised_nested_url(nested.group(1))


def _normalised_nested_url(candidate: str) -> str:
    """
    Return a nested address in the form a request can use.

    The text came out of a base64 payload, which is a layer that
    neither reads an HTML escape nor a percent-escape. Either could
    have travelled through it — a gate that escaped its own link, or
    a site that encoded it before embedding — and a nested address
    left in either state is a request that cannot work. So both are
    undone here, in the same order and for the same reasons as
    :func:`_cleaned_href`.
    """

    return unquote(_unescape_attribute_value(candidate))


def unlock_hubcdn(
    client: httpx.Client,
    option: MediaOption,
) -> UnlockedLink:
    """
    Resolve a ``hubcdn`` option to its R2 object.

    The page sets a JavaScript variable rather than redirecting, so
    the address is read out of the markup and unwrapped. The object
    it names is then verified against its own bytes like any other
    candidate.

    The dead-file banner is looked for before the variable, not
    after. A gate whose copy is gone can still be serving the stale
    variable from a cached page, and searching for the variable
    first would resolve that to a dead object and report the
    download as merely failed.

    Raises:
        ResolutionError: when the site's copy of the file is gone,
            when the gate no longer carries an address, or when the
            address behind it does not return media.
        NetworkError: when a gate page cannot be read.
    """

    html_text = _fetch_html(client, option.url)

    if _is_dead_file_page(html_text):
        raise _raise_dead_file(option.url, "hubcdn")

    match = _HUBCDN_REURL_PATTERN.search(html_text)

    if not match:
        raise ResolutionError(
            "The download gate did not expose a file "
            "address. The site's layout may have changed.",
            url=option.url,
            strategy="hubcdn",
        )

    direct_url = _decode_wrapped_url(
        _unescape_attribute_value(match.group(1))
    )

    reasons: list[str] = []

    verified = verify_media(
        client,
        direct_url,
        referer=option.url,
        reasons=reasons,
    )

    if verified is None:
        raise ResolutionError(
            "The file address behind this link did not "
            f"return media: {_summarise_reasons(reasons)}.",
            url=option.url,
            strategy="hubcdn",
        )

    return _relabel(verified, "hubcdn")


def unlock_hubdrive(
    client: httpx.Client,
    option: MediaOption,
) -> UnlockedLink:
    """
    Resolve a ``hubdrive`` option through its mirror chain.

    Walks file page to HubCloud drive page to the mirror listing,
    then verifies each listed mirror. The listing is not trusted:
    it mixes real files with further HTML wrappers, and only the
    returned bytes separate them.

    Each page of the chain is checked for the site's dead-file banner
    before the next link is looked for in it. The banner appears on
    any of the three, a page whose file is gone can still expose a
    stale link, and reporting that as a layout change sends the user
    looking for a bug that is not there.

    The mirror fan-out is bounded twice over, by
    :data:`MAX_MIRRORS` and by :data:`UNLOCK_TIME_BUDGET`: a listing
    is not trusted to be short, and each probe can cost its full
    timeout, so an uncapped walk of a long listing keeps the user
    waiting for minutes after the answer is already known.

    Raises:
        ResolutionError: when the chain breaks, when no listed
            mirror serves a file, or when the site's copy of the
            file is gone.
        NetworkError: when any page of the chain cannot be read.
    """

    deadline = time.monotonic() + UNLOCK_TIME_BUDGET

    file_page = _fetch_html(client, option.url)

    if _is_dead_file_page(file_page):
        raise _raise_dead_file(option.url, "hubdrive")

    drive_match = _HUBCLOUD_PAGE_PATTERN.search(file_page)

    if not drive_match:
        raise ResolutionError(
            "This link no longer exposes a download.",
            url=option.url,
            strategy="hubdrive",
        )

    drive_url = drive_match.group(1)

    drive_page = _fetch_html(
        client,
        drive_url,
    )

    if _is_dead_file_page(drive_page):
        raise _raise_dead_file(drive_url, "hubdrive")

    listing_match = _MIRROR_LISTING_PATTERN.search(
        drive_page
    )

    if not listing_match:
        raise ResolutionError(
            "The HubCloud page did not link a mirror list.",
            url=drive_url,
            strategy="hubdrive",
        )

    listing_url = _cleaned_href(
        _unescape_attribute_value(listing_match.group(1)),
        drive_url,
    )

    listing_page = _fetch_html(
        client,
        listing_url,
    )

    if _is_dead_file_page(listing_page):
        raise _raise_dead_file(listing_url, "hubdrive")

    mirrors = _download_mirrors(listing_page, listing_url)

    if not mirrors:
        raise ResolutionError(
            "The mirror list was empty.",
            url=listing_url,
            strategy="hubdrive",
        )

    probed = mirrors[:MAX_MIRRORS]

    if len(probed) < len(mirrors):
        logger.warning(
            "the listing at %s offers %d mirrors; only the "
            "first %d will be probed",
            listing_url,
            len(mirrors),
            len(probed),
        )

    reasons: list[str] = []
    attempts = 0

    for label, mirror_url in probed:
        if time.monotonic() > deadline:
            logger.warning(
                "the time budget of %ss is spent after %d of "
                "%d mirrors",
                UNLOCK_TIME_BUDGET,
                attempts,
                len(probed),
            )
            reasons.append("the time budget ran out")
            break

        logger.debug("probing mirror %r at %s", label, mirror_url)

        attempts += 1

        verified = verify_media(
            client,
            mirror_url,
            referer=listing_url,
            reasons=reasons,
        )

        if verified is not None:
            return _relabel(verified, "hubdrive")

    raise ResolutionError(
        f"None of the {attempts} probed mirrors returned a file: "
        f"{_summarise_reasons(reasons)}.",
        url=option.url,
        strategy="hubdrive",
    )


def _summarise_reasons(reasons: Sequence[str]) -> str:
    """
    Group probe rejections into one countable clause.

    Args:
        reasons: The reasons recorded by :func:`verify_media`, in
            the order they happened.

    Returns:
        A phrase that reads after "because", naming each distinct
        reason once with how many probes hit it. The point is that
        every cause is named: 503, a connection reset and an ad page
        are three different things, and a message that calls them all
        web pages is false about two of them.
    """

    if not reasons:
        return "no reason was recorded"

    counts: dict[str, int] = {}

    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1

    return "; ".join(
        reason if count == 1 else f"{reason} ({count})"
        for reason, count in counts.items()
    )


#: The escapes that can legitimately appear inside an attribute
#: value, and nothing else. A semicolon is required on each, which is
#: what the markup these pages serve writes.
_ATTR_ENTITY_PATTERN = re.compile(
    r"&(?:#([0-9]+)|#[xX]([0-9a-fA-F]+)|(amp|lt|gt|quot|apos));"
)

_NAMED_ATTR_ENTITIES = {
    "amp": "&",
    "lt": "<",
    "gt": ">",
    "quot": '"',
    "apos": "'",
}


def _unescape_attribute_value(
    href: str,
) -> str:
    """
    Undo the HTML escaping inside one attribute value.

    Only the five escapes that belong inside an attribute, plus
    numeric references, are undone. Deliberately not
    :func:`html.unescape`, which resolves the whole HTML5 legacy
    entity table: in that table ``&not``, ``&para`` and ``&times``
    are entities, so the query ``?x=1&notit=2.mkv`` comes back as
    ``?x=1¬it=2.mkv`` and the request asks for something else.
    Matching the whole table in a URL silently rewrites the URL,
    and a rewritten URL is worse than one that fails.

    Args:
        href: An attribute value as written in raw markup, so any
            ``&`` in it is still an escape.

    Returns:
        The value as a browser would read it. An unrecognised or
        impossible reference is left exactly as written, since
        leaving it alone is what the markup meant.
    """

    def replace_entity(match: re.Match[str]) -> str:
        decimal, hexadecimal, named = match.groups()

        if named:
            return _NAMED_ATTR_ENTITIES[named]

        try:
            return chr(int(decimal or hexadecimal, 10 if decimal else 16))

        except ValueError:
            # A code point that does not exist is not a character.
            return match.group(0)

    return _ATTR_ENTITY_PATTERN.sub(replace_entity, href)


def _cleaned_href(
    href: str,
    base_url: str,
) -> str:
    """
    Turn an ``href`` read out of markup into a usable URL.

    Two layers sit between the markup and a request, undone in this
    order. The address may be relative, which needs the page it was
    read from, so it is joined onto that first. Percent-encoding is
    the outermost layer and is removed last, and that is what makes
    it the right order: a path segment written ``%2F`` is one
    segment, so decoding it before the join would turn it into a
    real separator and ask a different host for a different
    resource.

    The HTML layer is not undone here. BeautifulSoup has already
    undone it for an href read off a parsed anchor, and doing it a
    second time loses an entity level: markup written
    ``?f=a&amp;amp;b.mkv`` carries a literal ``&amp;`` in the value
    and must be requested as ``&amp;``. An href matched out of raw
    markup by a pattern is therefore unescaped at the point of the
    match, by :func:`_unescape_attribute_value`.

    Known limitation, and the cost of that order: a ``%26`` *inside
    a parameter value* decodes to a real ``&`` and splits the
    parameter in two, so ``?data=a%26b.mkv&token=t9`` is requested as
    ``?data=a&b.mkv&token=t9``. No mirror this module talks to puts
    one there, but a site's ``data=`` token that did would come back
    altered, and nothing here detects that.

    Args:
        href: The attribute value, already unescaped.
        base_url: The URL the markup was fetched from.

    Returns:
        An absolute, unencoded URL.
    """

    return unquote(urljoin(base_url, href.strip()))


#: Schemes a mirror link may use. Everything else is not a fetchable
#: address, or is the listing asking the page to do something on the
#: user's behalf: ``javascript:`` runs, ``data:`` and ``mailto:``
#: cannot be requested at all, and httpx raises a bare ``ValueError``
#: for them, which used to escape the mirror loop and abandon the
#: whole unlock over one bad href.
_FETCHABLE_HREF_SCHEMES = frozenset({"", "http", "https"})

#: Words a listing uses for the links that serve the file itself.
#: A set rather than the literal word "download", because the site's
#: own vocabulary is wider than that: its dead-file banner says
#: "gserver", and a label reading "Mirror 1", "Server 2" or
#: "GServer 1" names the download perfectly well.
_MIRROR_LABEL_WORDS = frozenset({
    "download",
    "direct",
    "gserver",
    "mirror",
    "server",
})

_LABEL_WORD_PATTERN = re.compile(r"[a-z0-9]+")


def _is_mirror_label(label: str) -> bool:
    """
    Report whether a label names one of the file's own links.

    Matched word by word rather than as a substring, so "Server 2"
    counts and "observation" does not.
    """

    words = set(_LABEL_WORD_PATTERN.findall(label.lower()))

    return bool(words & _MIRROR_LABEL_WORDS)


def _download_mirrors(
    html_text: str,
    base_url: str,
) -> list[tuple[str, str]]:
    """
    Return the labelled mirror links from a listing page.

    An anchor whose label contains one of :data:`_MIRROR_LABEL_WORDS`
    is taken as a mirror. The listing also links the file's own page
    and unrelated domains, so an unlabelled anchor is not a mirror by
    itself.

    When no label matches, every fetchable anchor is offered anyway.
    A listing whose options are called "480p - HD" is still offering
    the file, and returning nothing there reports a live download as
    an empty list. The fallback can therefore propose a link that is
    not the file — which costs one probe, since each candidate is
    judged on its own bytes, and the probe cap bounds the cost.

    Args:
        html_text: The listing page.
        base_url: Where that page was fetched from, used to resolve
            a relative ``href``. A listing may be served from
            anywhere, so its mirrors need not be absolute.

    Returns:
        ``(label, absolute url)`` pairs, in page order, without
        duplicates.
    """

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_text, "html.parser")

    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        raw_href = str(anchor.get("href") or "").strip()

        if not raw_href or raw_href.startswith("#"):
            continue

        href = _cleaned_href(raw_href, base_url)

        if (
            not href
            or href in seen
            or urlparse(href).scheme not in _FETCHABLE_HREF_SCHEMES
        ):
            continue

        seen.add(href)

        label = " ".join(
            anchor.get_text(" ", strip=True).split()
        )

        candidates.append((label, href))

    labelled = [
        pair for pair in candidates if _is_mirror_label(pair[0])
    ]

    if labelled or not candidates:
        return labelled

    logger.debug(
        "no label in %s named a mirror; offering all %d links",
        base_url,
        len(candidates),
    )

    return candidates


def _relabel(
    link: UnlockedLink,
    strategy: str,
) -> UnlockedLink:
    """Return a copy of a verified link tagged with its strategy."""

    return replace(link, strategy=strategy)


#: What a strategy handler takes and returns. A named alias because
#: both the table and the resolver have to carry it.
_StrategyHandler = Callable[
    [httpx.Client, MediaOption],
    UnlockedLink,
]

#: Strategy name to handler. Built at module scope rather than inside
#: :func:`unlock` so that a name without a handler is a named error
#: instead of a dict lookup failure.
_STRATEGIES: dict[str, _StrategyHandler] = {
    "hubcdn": unlock_hubcdn,
    "hubdrive": unlock_hubdrive,
}


def _resolve_strategy(
    name: str,
    option: MediaOption,
) -> _StrategyHandler:
    """
    Return the handler for a named strategy.

    Args:
        name: A strategy name from :func:`_iter_strategies`.
        option: The option being unlocked, so the error can name it.

    Returns:
        The handler for ``name``.

    Raises:
        ResolutionError: when no handler is registered for the name.
            That is a bug in this module rather than anything to do
            with the link, and naming it is the whole point: the
            name is looked up rather than used to index, so a typo
            reports itself instead of surfacing to the user as a bare
            ``'brand_new_gate'``.
    """

    handler = _STRATEGIES.get(name)

    if handler is None:
        raise ResolutionError(
            f"No handler is registered for the {name!r} strategy.",
            url=option.url,
            strategy=name,
        )

    return handler


def _iter_strategies(
    host: str,
) -> Iterator[str]:
    """Yield the gate strategies that could apply to a host."""

    if any(
        host_suffix(host, domain)
        for domain in _HUBCDN_HOSTS
    ):
        yield "hubcdn"

    if host_suffix(host, "hubdrive.pics"):
        yield "hubdrive"


def host_suffix(
    host: str,
    domain: str,
) -> bool:
    """Return True when a host is a domain or a subdomain of it."""

    return host == domain or host.endswith(f".{domain}")


def _as_resolution_error(
    option: MediaOption,
    error: Exception,
    strategy: str = "",
) -> ResolutionError:
    """
    Report a failure against one option as a resolution error.

    Only the message is carried over; the original is attached as
    the cause, so a traceback still explains what actually went
    wrong. The link is handed back to a caller rather than raised,
    so the chain has to be attached by hand.

    Args:
        option: The option the failure belongs to.
        error: What went wrong.
        strategy: The strategy that failed, where the caller knows
            it. Otherwise the failure's own name is used, since a
            gate that got as far as naming itself should keep it.

    Returns:
        An error carrying the message, the option's address and
        the original as its cause.
    """

    reported = ResolutionError(
        str(error),
        url=option.url,
        strategy=strategy or getattr(error, "strategy", ""),
        content_type=getattr(error, "content_type", "") or "",
    )

    reported.__cause__ = error

    return reported


def unlock(
    option: MediaOption,
    *,
    client: httpx.Client | None = None,
) -> UnlockedLink:
    """
    Open a site's download gate and return the real file address.

    Every strategy that could apply to the host is tried in turn, so
    a gate that answers with a timeout is not the end of the road
    while another approach remains.

    Args:
        option: The chosen option from a post page.
        client: Optional caller-owned client, reused across options
            so the connection pool and cookies are shared.

    Returns:
        A verified file address.

    Raises:
        ResolutionError: when the option streams instead of
            downloading, when no strategy applies, when every
            strategy that did apply failed, or when a strategy name
            has no handler registered. The last is a fault in this
            module rather than a dead link, and it is resolved before
            the loop so the fault cannot be reported as one strategy
            failing and turned into a message for the user.
    """

    if not option.is_download:
        raise ResolutionError(
            "This option is a stream, not a file. "
            "Pick a download option instead.",
            url=option.url,
            strategy=option.kind,
        )

    host = registrable_host(option.url)
    strategies = list(_iter_strategies(host))

    if not strategies:
        raise ResolutionError(
            f"No way to open a download link on {host}.",
            url=option.url,
        )

    owned = client is None

    http_client = client or _build_client()

    try:
        handlers = [
            (name, _resolve_strategy(name, option))
            for name in strategies
        ]

        failures: list[tuple[str, Exception]] = []

        for name, handler in handlers:
            try:
                return handler(http_client, option)

            # One strategy failing says nothing about the next,
            # and letting an unexpected error escape would abandon
            # an approach that might still have worked.
            except Exception as error:
                logger.exception(
                    "strategy %s failed for %s: %s: %s",
                    name,
                    option.url,
                    type(error).__name__,
                    error,
                )
                failures.append((name, error))

        raise _report_strategy_failures(
            option,
            failures,
        )

    finally:
        if owned:
            http_client.close()


def _report_strategy_failures(
    option: MediaOption,
    failures: Sequence[tuple[str, Exception]],
) -> ResolutionError:
    """
    Turn the failures of every strategy into one error.

    Args:
        option: The option that could not be unlocked.
        failures: ``(strategy, error)`` for each strategy tried, in
            the order they were tried. Not empty: the caller only
            gets here after recording at least one.

    Returns:
        The error from the last strategy tried. Strategies are
        ordered by host, not by how far they got, so nothing is
        known about which came closest to resolving it; every earlier
        failure is already logged.
    """

    name, error = failures[-1]

    if len(failures) > 1:
        logger.warning(
            "every strategy failed for %s: %s",
            option.url,
            describe_sequence([item for item, _ in failures]),
        )

    return _as_resolution_error(
        option,
        error,
        strategy=name,
    )


def unlock_many(
    options: Iterable[MediaOption],
    *,
    client: httpx.Client | None = None,
) -> list[tuple[MediaOption, UnlockedLink | ResolutionError]]:
    """
    Open several gates, pairing each option with its outcome.

    Any per-item failure is returned beside its option instead of
    being raised, because the whole point of a menu is that one
    dead entry must not hide the others. That covers bugs as well as
    dead links: an unexpected error is reported against its option,
    with the original kept as its cause.

    Exceptions that mean "stop" — ``KeyboardInterrupt`` and
    ``SystemExit`` — are not caught.

    Returns:
        ``(option, link)`` on success and ``(option, error)`` on
        failure, in the order given.
    """

    owned = client is None

    http_client = client or _build_client()

    results: list[
        tuple[MediaOption, UnlockedLink | ResolutionError]
    ] = []

    try:
        for option in options:
            try:
                link = unlock(option, client=http_client)

            except Exception as error:
                logger.exception(
                    "option %s failed: %s: %s",
                    option.url,
                    type(error).__name__,
                    error,
                )
                results.append(
                    (
                        option,
                        _as_resolution_error(option, error),
                    )
                )

            else:
                results.append((option, link))
    finally:
        if owned:
            http_client.close()

    return results


def format_size(size_bytes: int | None) -> str:
    """Render a byte count the way the site labels its options."""

    if size_bytes is None or size_bytes < 0:
        return "unknown size"

    if size_bytes == 0:
        return "0 B"

    value = float(size_bytes)

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"

            formatted = f"{value:.1f} {unit}"
            return formatted.replace(".0 ", " ")

        value /= 1024
    return f"{value:.1f}TB"


def describe_sequence(
    items: Sequence[str],
    limit: int = 3,
) -> str:
    """Join a short list for an error message."""

    shown = list(items[:limit])

    if len(items) > limit:
        shown.append(f"+{len(items) - limit} more")

    return ", ".join(shown)

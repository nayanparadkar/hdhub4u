"""
Turning a site's gated link into a downloadable file.

Every download link on a post page points at a host that serves
HTML, not media. The HTML is a short redirect gate, and reading it
is what produces the real file URL. Each gate family has its own
shape, so each gets its own strategy here:

``hubcdn.club``
    302s to a sibling domain whose page carries a variable named
    ``reurl``. That value wraps, base64-encoded, the direct
    address of a Cloudflare R2 object. One hop, no scripting.

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
import html
import re
from dataclasses import dataclass
from typing import Iterable, Iterator, Sequence
from urllib.parse import unquote, urljoin, urlparse

import httpx

from .catalog import MediaOption, registrable_host
from .errors import NetworkError, ResolutionError
from .http import DEFAULT_ATTEMPTS, with_retries
from .http_types import USER_AGENT, is_media_content_type
from .logging_setup import get_logger

logger = get_logger(__name__)

#: How many bytes are pulled to identify a container. Long enough to
#: hold an MPEG-TS packet, so a transport stream can be recognised
#: from its own layout.
PROBE_BYTES = 4096

PROBE_TIMEOUT = httpx.Timeout(25.0, connect=10.0)

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

_DISPOSITION_DOWNLOAD_PATTERN = re.compile(
    r"download\s+filename\*",
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
#: with the same sync byte. One sync byte alone proves nothing, so a
#: second one is required at exactly the next packet boundary.
_MPEG_TS_PACKET_SIZE = 188
_MPEG_TS_SYNC_BYTE = b"\x47"
_MPEG_TS_SECOND_SYNC_OFFSET = _MPEG_TS_PACKET_SIZE

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

    Every field other than the address itself was read out of a
    response that had already been shown to begin with a media
    container. ``container`` is the strongest of them: it comes
    from the leading bytes, where ``content_type`` and ``filename``
    only come from headers the server chose to send.
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
        head: The first bytes the candidate returned. A prefix
            long enough to hold an MPEG-TS packet is expected, so
            that :func:`is_mpeg_ts_sync_aligned` can do its job;
            a shorter one may be declined by that check only.

    Returns:
        A short label, or an empty string when the bytes are not a
        container this module recognises. HTML deliberately does not
        match: a gate page served instead of a file is the single
        most common failure here, and it has to be detectable.
    """

    if not head:
        return ""

    stripped = head.lstrip()

    if stripped[:1] in (b"<",) or stripped[:14].lower() == b"<!doctype html":
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

    if head[:1] == _MPEG_TS_SYNC_BYTE and is_mpeg_ts_sync_aligned(head):
        return "mpeg-ts"

    return ""


def is_mpeg_ts_sync_aligned(
    head: bytes,
) -> bool:
    """
    Report whether a prefix carries two MPEG-TS sync bytes.

    A transport stream is packets of :data:`_MPEG_TS_PACKET_SIZE`
    bytes, every one of them opening with ``0x47``. The leading
    byte of a prefix is therefore near-worthless on its own: it is
    also the letter ``G``, so a GIF, a GraphQL response and any page
    of plain prose beginning with "Goodbye" would satisfy it. The
    byte at the next packet boundary is what makes the claim.

    Args:
        head: The candidate's leading bytes.

    Returns:
        True only when both sync bytes are present, so at least
        :data:`_MPEG_TS_PACKET_SIZE` bytes must have been read. A
        shorter prefix is declined rather than guessed at.
    """

    if head[:1] != _MPEG_TS_SYNC_BYTE:
        return False

    if len(head) < _MPEG_TS_SECOND_SYNC_OFFSET + 1:
        return False

    return head[_MPEG_TS_SECOND_SYNC_OFFSET] == 0x47


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
    like ``download`` or the file id.

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


def _looks_like_an_object_key(name: str) -> bool:
    """Return True when a name is a storage key, not a filename."""

    if not name:
        return False

    if _MEDIA_SUFFIX_PATTERN.search(name.lower()):
        return False

    return bool(re.fullmatch(r"[0-9a-f]{16,}", name))


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

    Raises:
        NetworkError: when the page cannot be read at all.
    """

    def operation() -> str:
        response = client.get(url)
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
) -> UnlockedLink | None:
    """
    Fetch a prefix of a URL and decide whether it is a real file.

    Several mirrors listed as downloads return a further HTML page.
    Only the bytes decide, so this reads a prefix, checks for a
    media container, and reports what the server claims about size
    and name.

    The prefix is requested rather than streamed indefinitely: it is
    read in full and the connection is released, so the same client
    can carry the next probe.

    Returns:
        The verified link, or None when the URL is not a file. A
        None is an ordinary outcome here and is not an error. It
        covers several distinct causes — an HTTP error, a transport
        failure, an unrecognised container, a protocol error in the
        address — which the caller cannot tell apart from the value
        alone, so each is logged at debug level before None is
        returned.
    """

    headers: dict[str, str] = {
        "Range": f"bytes=0-{PROBE_BYTES - 1}",
    }

    if referer:
        headers["Referer"] = referer

    try:
        response = client.get(url, headers=headers)

    except httpx.HTTPError as error:
        logger.debug(
            "probe of %s failed before any bytes arrived: %s: %s",
            url,
            type(error).__name__,
            error,
        )
        return None

    if response.status_code >= 400:
        logger.debug(
            "probe of %s got HTTP %d",
            url,
            response.status_code,
        )
        return None

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

    body = _read_probe_prefix(response)

    container = sniff_container(body)

    if not container:
        logger.debug(
            "probe of %s: first %d bytes hold no known "
            "container (content-type %r)",
            url,
            len(body),
            content_type,
        )
        return None

    if content_type and is_media_content_type(content_type):
        logger.debug(
            "%s: container=%s", url, container
        )
    else:
        logger.debug(
            "%s: container=%s despite content-type %r",
            url,
            content_type,
        )

    filename = filename_from_disposition(disposition)

    if not _looks_like_a_filename(filename):
        filename = _fallback_filename(url, container)

    return UnlockedLink(
        url=url,
        filename=filename,
        host=registrable_host(url),
        strategy="direct",
        content_type=content_type,
        size_bytes=size_bytes,
        container=container,
    )


def _read_probe_prefix(
    response: httpx.Response,
) -> bytes:
    """
    Read the probe prefix out of a response.

    A range-limited response already holds the whole prefix, so it
    is read in full and the connection returned to the pool instead
    of being abandoned mid-body. Nothing stops a server ignoring
    the range and starting the whole file, so reading stops at
    :data:`PROBE_BYTES`, and also if the body simply ends there.
    """

    try:
        chunks: list[bytes] = []

        for chunk in response.iter_bytes():
            chunks.append(chunk)

            if sum(map(len, chunks)) >= PROBE_BYTES:
                break

        return b"".join(chunks)[:PROBE_BYTES]

    except httpx.HTTPError as error:
        # Deciding on a truncated prefix could approve a file on
        # half its evidence, so a broken transfer is a rejection.
        logger.debug(
            "probe body ended early: %s: %s",
            type(error).__name__,
            error,
        )
        return b""


def _size_from_headers(
    headers: httpx.Headers,
    content_range: str,
) -> int | None:
    """
    Return the total object size from range or length headers.

    Only ``Content-Range`` carries a total: its trailing number is
    the size of the whole object. ``Content-Length`` counts the body
    that was actually sent, which for a partial response is just the
    prefix. So a length is believed only when it is too short to be
    a prefix, meaning the object itself is that small.

    Returns:
        The object's size, or None when no header can support one.
    """

    match = re.search(r"/(\d+)\s*$", content_range)

    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None

    raw_length = headers.get("content-length")

    if raw_length and raw_length.isdigit():
        length = int(raw_length)

        if length < PROBE_BYTES:
            # Too small to be a prefix of anything, so this is the
            # whole object.
            return length

        # Too big to be the whole object. Either the server ignored
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
#: rather than as a link worth retrying.
_DEAD_FILE_MARKERS = (
    "no url found",
    "file is deleted or unavailable",
    "failed to get data from gserver",
)


def _is_dead_file_page(html_text: str) -> bool:
    """
    Report whether a gate is serving its "file is gone" page.

    This is a normal state for this site rather than a fault in the
    client, and it is worth naming precisely so the user is not told
    to retry a link that will never work.
    """

    lowered = html_text.lower()

    return any(
        marker in lowered for marker in _DEAD_FILE_MARKERS
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
    ``b64decode(validate=False)`` would go on to accept it.
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
        blob: Raw, still percent-encoded base64. It is unescaped
            with ``+`` left alone, then padding is made explicit,
            because a ``+``/``/`` payload is valid urlsafe base64
            but not valid standard base64.

    Raises:
        binascii.Error: when the payload is not base64 at all.
    """

    text = unquote(blob)
    padded = text + "=" * (-len(text) % 4)

    return base64.urlsafe_b64decode(padded).decode(
        "utf-8",
        "replace",
    )


def _decode_wrapped_url(
    encoded: str,
) -> str:
    """
    Unwrap the base64 address the ``hubcdn`` gate embeds.

    The gate stores ``https://host/?r=<base64>`` where the decoded
    text is a second URL carrying the real address in its ``link``
    parameter. Two hops, both plain text.

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

    return nested.group(1)


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

    Raises:
        ResolutionError: when the gate no longer carries an address,
            or when the address behind it does not return media.
        NetworkError: when a gate page cannot be read.
    """

    html_text = _fetch_html(client, option.url)

    match = _HUBCDN_REURL_PATTERN.search(html_text)

    if not match:
        if _is_dead_file_page(html_text):
            raise ResolutionError(
                "The site's copy of this file is gone. "
                "Its listing is still up but the file behind it "
                "no longer exists.",
                url=option.url,
                strategy="hubcdn",
            )

        raise ResolutionError(
            "The download gate did not expose a file "
            "address. The site's layout may have changed.",
            url=option.url,
            strategy="hubcdn",
        )

    direct_url = _decode_wrapped_url(match.group(1))

    verified = verify_media(
        client,
        direct_url,
        referer=option.url,
    )

    if verified is None:
        raise ResolutionError(
            "The file address behind this link did not "
            "return media.",
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

    Raises:
        ResolutionError: when the chain breaks, or when no listed
            mirror serves a file.
        NetworkError: when any page of the chain cannot be read.
    """

    file_page = _fetch_html(client, option.url)

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
        listing_match.group(1),
        drive_url,
    )

    listing_page = _fetch_html(
        client,
        listing_url,
    )

    mirrors = _download_mirrors(listing_page, listing_url)

    if not mirrors:
        raise ResolutionError(
            "The mirror list was empty.",
            url=listing_url,
            strategy="hubdrive",
        )

    rejected: list[str] = []

    for label, mirror_url in mirrors:
        verified = verify_media(
            client,
            mirror_url,
            referer=listing_url,
        )

        if verified is None:
            rejected.append(label)
            continue

        return _relabel(verified, "hubdrive")

    raise ResolutionError(
        "None of the "
        f"{len(mirrors)} listed mirrors returned a file "
        f"({len(rejected)} were web pages).",
        url=option.url,
        strategy="hubdrive",
    )


def _cleaned_href(
    href: str,
    base_url: str,
) -> str:
    """
    Turn an ``href`` read out of markup into a usable URL.

    Three layers sit between the markup and a request: the page was
    matched as text, so ``&amp;`` is still escaped and has to come
    back; it may be percent-encoded, which is a URL layer rather
    than an HTML one and is undone last; and it may be relative,
    which needs the address of the page it was read from.

    Args:
        href: The attribute value as written in the markup.
        base_url: The URL the markup was fetched from.

    Returns:
        An absolute, unescaped, unencoded URL, or an empty string
        when nothing usable is left.
    """

    return unquote(
        urljoin(base_url, html.unescape(href.strip()))
    )


def _download_mirrors(
    html_text: str,
    base_url: str,
) -> list[tuple[str, str]]:
    """
    Return the labelled mirror links from a listing page.

    Only anchors whose label mentions a download are taken. The
    listing also links the file's own page and unrelated domains.

    Args:
        html_text: The listing page.
        base_url: Where that page was fetched from, used to resolve
            a relative ``href``. A listing may be served from
            anywhere, so its mirrors need not be absolute; left
            relative they would raise a protocol error that reads
            exactly like a mirror serving a web page.

    Returns:
        ``(label, absolute url)`` pairs, in page order, without
        duplicates.
    """

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_text, "html.parser")

    mirrors: list[tuple[str, str]] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        raw_href = str(anchor.get("href") or "").strip()

        if not raw_href or raw_href.startswith(("#", "javascript:")):
            continue

        label = " ".join(
            anchor.get_text(" ", strip=True).split()
        )

        lowered = label.lower()

        if "download" not in lowered:
            continue

        href = _cleaned_href(raw_href, base_url)

        if not href or href in seen:
            continue

        seen.add(href)

        mirrors.append((label, href))

    return mirrors


def _relabel(
    link: UnlockedLink,
    strategy: str,
) -> UnlockedLink:
    """Return a copy of a verified link tagged with its strategy."""

    from dataclasses import replace

    return replace(link, strategy=strategy)


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
    strategy: str,
) -> ResolutionError:
    """
    Report a failure against one option as a resolution error.

    Only the message is carried over; the original is attached as
    the cause, so a traceback still explains what actually went
    wrong. The link is handed back to a caller rather than raised,
    so the chain has to be attached by hand.
    """

    reported = ResolutionError(
        str(error),
        url=option.url,
        strategy=strategy,
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
            downloading, when no strategy applies, or when every
            strategy that did apply failed.
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
        handlers = {
            "hubcdn": unlock_hubcdn,
            "hubdrive": unlock_hubdrive,
        }

        failures: list[tuple[str, Exception]] = []

        for name in strategies:
            try:
                return handlers[name](http_client, option)

            # One strategy failing says nothing about the next,
            # and letting an unexpected error escape would abandon
            # an approach that might still have worked.
            except Exception as error:
                logger.debug(
                    "strategy %s failed for %s: %s: %s",
                    name,
                    option.url,
                    type(error).__name__,
                    error,
                    exc_info=True,
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

    Returns:
        The error from the last strategy tried, which is the one
        that got furthest. Earlier failures are already logged.
    """

    if not failures:
        return ResolutionError(
            "The download gate could not be opened.",
            url=option.url,
        )

    name, error = failures[-1]

    if len(failures) > 1:
        logger.debug(
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
                logger.debug(
                    "option %s failed: %s: %s",
                    option.url,
                    type(error).__name__,
                    error,
                    exc_info=True,
                )
                results.append(
                    (
                        option,
                        _as_resolution_error(
                            option,
                            error,
                            strategy=option.kind,
                        ),
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

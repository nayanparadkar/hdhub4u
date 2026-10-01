"""
Live catalog access for the site.

The website does not render search results into its HTML. The
``/search.html`` page is an empty shell whose inline script talks to
a Typesense-compatible endpoint on ``search.pingora.fyi`` and paints
the grid with JavaScript.

Two consequences shape this module:

* Search results are fetched from that endpoint rather than by
  parsing the page, so a listing is available without a browser.
* The endpoint rejects requests that do not look like they came from
  the site's own search page, so the browser-shaped ``Origin`` and
  ``Referer`` headers are part of the request, not decoration.

Download options are read from the post page instead, which *is*
static HTML. There, the anchor label is a poor signal: a link
labelled ``1080p x264 [2.3GB]`` may be a file or a player, decided
entirely by the host it points at. So options are classified by host.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlparse

import httpx

from .errors import NetworkError, ParseError
from .http_types import USER_AGENT
from .logging_setup import get_logger

logger = get_logger(__name__)

#: The site's own origin. Post permalinks are rebased onto it.
SITE_ORIGIN = "https://new1.hdhub4u.free"

#: The page a visitor is standing on when a search is issued.
SEARCH_PAGE_URL = f"{SITE_ORIGIN}/search.html"

#: Typesense-compatible endpoint the search page queries.
SEARCH_API_URL = (
    "https://search.pingora.fyi/collections/post/documents/search"
)

#: Endpoint behind the trending-tags ribbon on the search page.
POPULAR_API_URL = (
    "https://search.pingora.fyi"
    "/collections/popular_searches/documents/search"
)

#: Fields the site's own query weights, highest priority first.
QUERY_BY = "post_title,category,stars,director,imdb_id"

#: Matching weights, positionally aligned with :data:`QUERY_BY`.
QUERY_BY_WEIGHTS = "4,2,2,2,4"

DEFAULT_LIMIT = 15
MAX_LIMIT = 50

#: Hosts that serve a real file, mapped to the label shown in menus.
DOWNLOAD_HOSTS = frozenset(
    {
        "hubcdn.club",
        "hubcdn.wiki",
        "hubdrive.pics",
    }
)

#: Hosts that serve a player page. Their labels often advertise a
#: size and a resolution, but following one yields a stream rather
#: than a file, so offering them as downloads is a dead end.
STREAMING_HOSTS = frozenset(
    {
        "greenmotors.club",
        "greenmotors.cc",
        "hdstream4u.com",
        "hubstream.art",
    }
)

#: Hosts that are neither a mirror nor a player, e.g. an unrelated
#: site linked in the body copy. Never offered as a download.
UNKNOWN_KIND = "unknown"

DOWNLOAD_KIND = "download"
STREAMING_KIND = "streaming"

_YEAR_PATTERN = re.compile(r"\((\d{4})\)")

_SIZE_PATTERN = re.compile(
    r"\[\s*(\d+(?:\.\d+)?)\s*(gb|mb)\s*\]",
    re.IGNORECASE,
)

_RESOLUTION_PATTERN = re.compile(
    r"(?<!\d)(2160|1440|1080|720|480|360)\s*p",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CatalogItem:
    """One search result, ready to list."""

    title: str
    url: str
    thumbnail: str = ""
    year: str = ""
    categories: tuple[str, ...] = ()
    stars: tuple[str, ...] = ()
    imdb_id: str = ""

    def short_title(self, width: int = 70) -> str:
        """Return the title trimmed to a terminal-friendly width."""

        if len(self.title) <= width:
            return self.title

        return f"{self.title[: width - 1].rstrip()}…"


@dataclass(frozen=True)
class MediaOption:
    """A single choice offered by a post page."""

    label: str
    url: str
    kind: str
    host: str
    resolution: str = ""
    size_label: str = ""

    @property
    def is_download(self) -> bool:
        """Return True when this option points at a file."""

        return self.kind == DOWNLOAD_KIND


def _browser_headers() -> dict[str, str]:
    """
    Return the headers the site's own search page would send.

    The search endpoint answers ``403`` to anything that does not
    carry an ``Origin`` on the site, so these are load-bearing.
    """

    return {
        "Accept": (
            "application/json, text/plain, */*"
        ),
        "Origin": SITE_ORIGIN,
        "Referer": f"{SEARCH_PAGE_URL}?q=",
        "User-Agent": USER_AGENT,
    }


def normalize_permalink(permalink: str) -> str:
    """
    Rebase a stored permalink onto the site's own origin.

    The index carries permalinks against a sibling domain that
    rotates. The site itself ignores that host and uses only the
    path, so the path is what is reproduced here. Without this a
    result would be advertised at an address the CLI cannot read.
    """

    path = urlparse(permalink).path or "/"

    if not path.startswith("/"):
        path = f"/{path}"

    return f"{SITE_ORIGIN}{path}"


def _today_tag() -> str:
    """Return the analytics tag the search page sends."""

    return date.today().isoformat()


def search_catalog(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    page: int = 1,
    client: httpx.Client | None = None,
) -> list[CatalogItem]:
    """
    Search the live catalog.

    Args:
        query: Free text, matched the way the website matches it.
        limit: Maximum results to return, clamped to 50.
        page: 1-based result page.
        client: Optional caller-owned HTTP client.

    Returns:
        Results in the site's own order, newest first.

    Raises:
        NetworkError: when the endpoint is unreachable.
        ParseError: when the payload is not a search response.
    """

    term = query.strip()

    if not term:
        return []

    params = {
        "q": term,
        "query_by": QUERY_BY,
        "query_by_weights": QUERY_BY_WEIGHTS,
        "sort_by": "sort_by_date:desc",
        "limit": max(1, min(int(limit), MAX_LIMIT)),
        "page": max(1, int(page)),
        "highlight_fields": "none",
        "use_cache": "true",
        "analytics_tag": _today_tag(),
    }

    owned = client is None

    http_client = client or httpx.Client(
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=True,
    )

    try:
        response = http_client.get(
            SEARCH_API_URL,
            params=params,
            headers=_browser_headers(),
        )

        response.raise_for_status()

        payload = response.json()

    except httpx.HTTPStatusError as error:
        raise NetworkError(
            "The search service rejected the request "
            f"(HTTP {error.response.status_code})."
        ) from error

    except httpx.HTTPError as error:
        raise NetworkError(
            f"Could not reach the search service: {error}"
        ) from error

    except ValueError as error:
        raise ParseError(
            "The search service returned a "
            "response that was not JSON."
        ) from error

    finally:
        if owned:
            http_client.close()

    return _items_from_payload(payload)


def popular_searches(
    *,
    limit: int = 20,
    client: httpx.Client | None = None,
) -> list[str]:
    """
    Return the trending search terms shown on the search page.

    Used to seed the interactive prompt so a user who does not know
    what to type still gets suggestions.

    Raises:
        NetworkError: when the endpoint is unreachable.
    """

    params = {
        "q": "*",
        "query_by": "q",
        "per_page": max(1, min(int(limit), MAX_LIMIT)),
        "sort_by": "count:desc",
    }

    owned = client is None

    http_client = client or httpx.Client(
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=True,
    )

    try:
        response = http_client.get(
            POPULAR_API_URL,
            params=params,
            headers=_browser_headers(),
        )

        response.raise_for_status()

        payload = response.json()

    except httpx.HTTPError as error:
        raise NetworkError(
            f"Could not load trending searches: {error}"
        ) from error

    except ValueError:
        return []

    finally:
        if owned:
            http_client.close()

    terms: list[str] = []
    seen: set[str] = set()

    for hit in _hits(payload):
        document = hit.get("document") or {}
        term = str(document.get("q") or "").strip()

        if not term or term in seen:
            continue

        seen.add(term)
        terms.append(term)

    return terms[:limit]


def _hits(
    payload: Any,
) -> list[Mapping[str, Any]]:
    """
    Return the hit list from a Typesense-shaped payload.

    Accepts the object form and the bare-list form, because the two
    collections differ in which they return.
    """

    if isinstance(payload, list):
        raw_hits: Any = payload
    elif isinstance(payload, Mapping):
        raw_hits = payload.get("hits") or []
    else:
        raw_hits = []

    if not isinstance(raw_hits, list):
        return []

    return [
        hit
        for hit in raw_hits
        if isinstance(hit, Mapping)
    ]


def _as_tuple(value: Any) -> tuple[str, ...]:
    """Coerce a document field into a tuple of clean strings."""

    if isinstance(value, str):
        return (value,) if value.strip() else ()

    if isinstance(value, Sequence):
        return tuple(
            str(entry).strip()
            for entry in value
            if str(entry).strip()
        )

    return ()


def _items_from_payload(
    payload: Any,
) -> list[CatalogItem]:
    """Build catalog items from a decoded search response."""

    items: list[CatalogItem] = []

    for hit in _hits(payload):
        document = hit.get("document")

        if not isinstance(document, Mapping):
            continue

        title = str(
            document.get("post_title") or ""
        ).strip()

        permalink = str(
            document.get("permalink") or ""
        ).strip()

        if not title or not permalink:
            continue

        year_match = _YEAR_PATTERN.search(title)

        items.append(
            CatalogItem(
                title=title,
                url=normalize_permalink(permalink),
                thumbnail=str(
                    document.get("post_thumbnail") or ""
                ),
                year=year_match.group(1) if year_match else "",
                categories=_as_tuple(
                    document.get("category")
                ),
                stars=_as_tuple(document.get("stars")),
                imdb_id=str(
                    document.get("imdb_id") or ""
                ),
            )
        )

    return items


def result_count(
    payload: Any,
) -> int:
    """
    Return the total number of matches a payload reported.

    Only meaningful when the caller holds the raw payload; the
    listing helpers discard it.
    """

    if not isinstance(payload, Mapping):
        return 0

    try:
        return int(payload.get("found") or 0)
    except (TypeError, ValueError):
        return 0


def registrable_host(url: str) -> str:
    """
    Return a host reduced to something comparable.

    Strips the port and a leading ``www.`` so ``www.hubcdn.club``
    and ``hubcdn.club`` classify the same way.
    """

    host = (urlparse(url).netloc or "").lower()

    if "@" in host:
        host = host.rsplit("@", 1)[1]

    host = host.split(":")[0]

    if host.startswith("www."):
        host = host[4:]

    return host


def host_suffix_match(
    host: str,
    domain: str,
) -> bool:
    """
    Return True when ``host`` is ``domain`` or below it.

    Gate pages redirect across sibling domains (``hubcdn.club`` to
    ``hubcdn.wiki``), so matching has to survive that.
    """

    return host == domain or host.endswith(f".{domain}")


def classify_kind(url: str) -> str:
    """
    Return whether a link resolves to a file or to a player.

    The anchor label cannot answer this. A link reading
    ``1080p x264 [2.3GB]`` is a file on one host and an HLS stream
    on another, and only the host decides.
    """

    host = registrable_host(url)

    if not host:
        return UNKNOWN_KIND

    for domain in DOWNLOAD_HOSTS:
        if host_suffix_match(host, domain):
            return DOWNLOAD_KIND

    for domain in STREAMING_HOSTS:
        if host_suffix_match(host, domain):
            return STREAMING_KIND

    return UNKNOWN_KIND


def _option_label(anchor: Any) -> str:
    """
    Return the human label on a download anchor.

    Post pages wrap these anchors in headings and indentation, so
    the text arrives with runs of newlines and tabs inside it. The
    label is also the only place the resolution and size appear,
    and those come back out of it later, so the whitespace is
    collapsed rather than merely stripped.
    """

    text = anchor.get_text(" ", strip=True)

    if not text:
        text = str(anchor.get("title") or "")

    collapsed = " ".join(text.split())

    if not collapsed:
        return ""

    return collapsed


def extract_size_label(label: str) -> str:
    """Return a ``[1.1GB]``-style size hint from a label."""

    match = _SIZE_PATTERN.search(label)

    if not match:
        return ""

    return f"{match.group(1)}{match.group(2).upper()}"


def extract_resolution(label: str) -> str:
    """Return the vertical resolution mentioned in a label."""

    match = _RESOLUTION_PATTERN.search(label)

    if not match:
        return ""

    return f"{match.group(1)}p"


def extract_options(
    html: str,
    base_url: str = SITE_ORIGIN,
) -> list[MediaOption]:
    """
    Return the download and streaming choices on a post page.

    Only anchors inside the download block are considered. The page
    also links to related posts, categories and an unrelated mirror
    site, and a naive sweep of every anchor produces dozens of
    options that have nothing to do with this title.

    Args:
        html: The post page markup.
        base_url: Used to resolve relative hrefs.

    Returns:
        Options in page order, which is ascending quality.
    """

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    options: list[MediaOption] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()

        if not href or href.startswith(("#", "javascript:")):
            continue

        url = urljoin(base_url, href)

        kind = classify_kind(url)

        if kind == UNKNOWN_KIND:
            continue

        if url in seen:
            continue

        seen.add(url)

        label = _option_label(anchor)

        if not label:
            continue

        options.append(
            MediaOption(
                label=label,
                url=url,
                kind=kind,
                host=registrable_host(url),
                resolution=extract_resolution(label),
                size_label=extract_size_label(label),
            )
        )

    return options


def download_options(
    options: Sequence[MediaOption],
) -> list[MediaOption]:
    """Return only the options that offer a real file."""

    return [
        option
        for option in options
        if option.is_download
    ]

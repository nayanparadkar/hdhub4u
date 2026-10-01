"""HTML parsing and title extraction."""

import re
from collections.abc import Sequence
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .http_types import USER_AGENT

SERIES_KEYWORDS = {
    "season",
    "web-series",
    "web series",
    "tv-show",
    "tv show",
    "series",
}


EPISODE_KEYWORDS = {
    "episode",
    "episodes",
    "ep-",
    "ep.",
    "all-episodes",
}


STRONG_MOVIE_KEYWORDS = {
    "full-movie",
    "full movie",
}


MOVIE_KEYWORDS = {
    "movie",
    "film",
}


WATCH_KEYWORDS = {
    "watch",
    "stream",
    "play",
}


CATEGORY_KEYWORDS = {
    "/category/",
    "/genre/",
    "/tag/",
    "/page/",
}


EXCLUDED_PATHS = {
    "/request-a-movie/",
    "/disclaimer/",
    "/how-to-download/",
    "/join-our-group/",
}


def get_string_attribute(
    tag,
    attribute: str,
) -> str:
    """
    Safely read a BeautifulSoup HTML attribute.

    BeautifulSoup can type HTML attributes as either
    strings or lists. Only return a stripped string.
    """
    value = tag.get(attribute)

    if isinstance(value, str):
        return value.strip()

    return ""


def is_excluded_path(
    url: str,
) -> bool:
    path = urlparse(url).path.lower()

    if not path.endswith("/"):
        path += "/"

    return path in EXCLUDED_PATHS


def fetch_page(
    url: str,
) -> tuple[str, str, int]:
    with httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(
            15.0,
            connect=5.0,
        ),
        headers={
            "User-Agent": USER_AGENT,
        },
    ) as client:
        response = client.get(url)

    response.raise_for_status()

    return (
        str(response.url),
        response.text,
        response.status_code,
    )


def is_category_url(
    url: str,
) -> bool:
    path = urlparse(url).path.lower()

    return any(
        keyword in path
        for keyword in CATEGORY_KEYWORDS
    )


def classify_media_content(
    text: str,
    url: str,
) -> str:
    if is_excluded_path(url):
        return "other"

    if is_category_url(url):
        return "category"

    value = f"{text} {url}".lower()

    # Explicit full-movie markers should win over
    # generic "season" / "series" words.
    if any(
        keyword in value
        for keyword in STRONG_MOVIE_KEYWORDS
    ):
        return "movie"

    if any(
        keyword in value
        for keyword in EPISODE_KEYWORDS
    ):
        return "episode"

    if any(
        keyword in value
        for keyword in SERIES_KEYWORDS
    ):
        return "webseries"

    if any(
        keyword in value
        for keyword in MOVIE_KEYWORDS
    ):
        return "movie"

    if any(
        keyword in value
        for keyword in WATCH_KEYWORDS
    ):
        return "watch"

    return "other"


def clean_title(
    title: str,
) -> str:
    title = " ".join(title.split())

    unwanted = {
        "Download",
        "Watch",
        "Watch Now",
        "Download Now",
        "Full Movie",
        "Full Series",
    }

    if title in unwanted:
        return ""

    return title.strip()


def extract_title_from_url(
    url: str,
) -> str:
    path = urlparse(url).path.strip("/")

    if not path:
        return ""

    slug = path.split("/")[-1]

    slug = unquote(slug)

    title = slug.replace("-", " ")

    metadata_patterns = [
        r"\b(?:2160|1440|1080|720|480|360|240)p\b",
        r"\b4k\b",
        r"\bfull\s+hd\b",
        r"\bhd\b",
        r"\bwebrip\b",
        r"\bweb[- ]dl\b",
        r"\bbluray\b",
        r"\bblu[- ]ray\b",
        r"\bhdtc\b",
        r"\bhdrip\b",
        r"\bdvdrip\b",
        r"\bds4k\b",
        r"\bx264\b",
        r"\bx265\b",
        r"\bhevc\b",
        r"\bunrated\b",
        r"\bstudio dub\b",
        r"\bdubbed\b",
        r"\bdual audio\b",
        r"\ball episodes\b",
        r"\bepisodes\b",
        r"\bepisode\b",
    ]

    for pattern in metadata_patterns:
        title = re.sub(
            pattern,
            "",
            title,
            flags=re.IGNORECASE,
        )

    language_patterns = [
        r"\bhindi\b",
        r"\benglish\b",
        r"\bgujarati\b",
        r"\bmarathi\b",
        r"\btamil\b",
        r"\btelugu\b",
        r"\bmalayalam\b",
        r"\bkannada\b",
        r"\bbengali\b",
        r"\bpunjabi\b",
        r"\bserbian\b",
    ]

    for pattern in language_patterns:
        title = re.sub(
            pattern,
            "",
            title,
            flags=re.IGNORECASE,
        )

    # Remove common quality / version markers.
    title = re.sub(
        r"\bv\d+\b",
        "",
        title,
        flags=re.IGNORECASE,
    )

    # Remove standalone years.
    title = re.sub(
        r"\s+\d{4}\b",
        "",
        title,
    )

    # Remove season numbers.
    title = re.sub(
        r"\s+season\s+\d+\b",
        "",
        title,
        flags=re.IGNORECASE,
    )

    # Remove common content suffixes.
    title = re.sub(
        r"\bfull\s+movie\b",
        "Full Movie",
        title,
        flags=re.IGNORECASE,
    )

    title = re.sub(
        r"\bfull\s+series\b",
        "Full Series",
        title,
        flags=re.IGNORECASE,
    )

    title = re.sub(
        r"\s+",
        " ",
        title,
    ).strip()

    return title.title()


def extract_link_title(
    anchor,
) -> str:
    title = anchor.get_text(
        " ",
        strip=True,
    )

    if title:
        cleaned = clean_title(title)

        if cleaned:
            return cleaned

    title = get_string_attribute(
        anchor,
        "title",
    )

    if title:
        cleaned = clean_title(title)

        if cleaned:
            return cleaned

    image = anchor.find("img")

    if image:
        for attribute in (
            "alt",
            "title",
        ):
            value = get_string_attribute(
                image,
                attribute,
            )

            if value:
                cleaned = clean_title(value)

                if cleaned:
                    return cleaned

    return ""


def extract_content_links(
    html: str,
    base_url: str,
) -> list[dict[str, str]]:
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    base_domain = urlparse(
        base_url
    ).netloc

    for anchor in soup.find_all(
        "a",
        href=True,
    ):
        href = get_string_attribute(
            anchor,
            "href",
        )

        if not href:
            continue

        url = urljoin(
            base_url,
            href,
        )

        parsed = urlparse(url)
        path = parsed.path.lower()

        if parsed.netloc != base_domain:
            continue

        if not path or path == "/":
            continue

        if is_excluded_path(url):
            continue

        title = extract_link_title(
            anchor
        )

        if not title:
            title = extract_title_from_url(
                url
            )

        media_type = classify_media_content(
            title,
            url,
        )

        if media_type not in {
            "movie",
            "webseries",
            "episode",
        }:
            continue

        if url in seen_urls:
            continue

        seen_urls.add(url)

        results.append(
            {
                "title": title or "[no title]",
                "url": url,
                "type": media_type,
            }
        )

    return results


def extract_download_options(
    html: str,
    base_url: str,
) -> list[dict[str, str]]:
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    option_keywords = {
        "480p",
        "720p",
        "1080p",
        "2160p",
        "4k",
        "bluray",
        "web-dl",
        "webrip",
        "download",
        "watch online",
    }

    ignored_titles = {
        "how to download ?",
        "how to download",
        "4k movies",
    }

    base_domain = urlparse(
        base_url
    ).netloc

    for anchor in soup.find_all(
        "a",
        href=True,
    ):
        href = get_string_attribute(
            anchor,
            "href",
        )

        if not href:
            continue

        title = extract_link_title(
            anchor
        )

        if not title:
            continue

        text = title.lower().strip()

        if text in ignored_titles:
            continue

        if not any(
            keyword in text
            for keyword in option_keywords
        ):
            continue

        url = urljoin(
            base_url,
            href,
        )

        parsed_url = urlparse(url)

        if parsed_url.netloc == base_domain:
            continue

        if url in seen_urls:
            continue

        seen_urls.add(url)

        if (
            "watch online" in text
            or "watch" in text
            or "stream" in text
        ):
            option_type = "Streaming"
        else:
            option_type = "Download"

        results.append(
            {
                "title": title,
                "url": url,
                "type": option_type,
            }
        )

    return results


def is_downloadable_option(
    option: dict[str, str],
) -> bool:
    """
    Return True when an option is a file download.

    A streaming option points at a player page, not at a file, so
    offering it as a download choice only produces a dead end.
    """

    return (
        str(
            option.get("type", "")
        ).lower()
        != "streaming"
    )


def downloadable_options(
    options: Sequence[dict[str, str]],
) -> list[dict[str, str]]:
    """Return only the options that offer a file."""

    return [
        option
        for option in options
        if is_downloadable_option(option)
    ]

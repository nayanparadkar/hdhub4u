"""Fetches and classifies arbitrary URLs."""

from __future__ import annotations

from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .http_types import USER_AGENT

MEDIA_EXTENSIONS = (
    ".mp4",
    ".mkv",
    ".webm",
    ".avi",
    ".mov",
    ".m4v",
    ".mp3",
    ".flac",
    ".wav",
)


def get_url_type(
    url: str,
    content_type: str,
) -> str:
    """
    Classify a URL as "media", "webpage" or "other".

    The path extension is checked first because many media servers
    return application/octet-stream for everything.
    """

    path = urlparse(url).path.lower()

    if path.endswith(MEDIA_EXTENSIONS):
        return "media"

    if content_type.startswith(("video/", "audio/")):
        return "media"

    if content_type.startswith("text/html"):
        return "webpage"

    return "other"


def extract_title(
    html: str,
) -> str:
    """Return the text of the page's <title> element."""

    soup = BeautifulSoup(html, "html.parser")

    if soup.title is None:
        return ""

    return soup.title.get_text(
        " ",
        strip=True,
    )


def _error_result(
    url: str,
    message: str,
) -> dict[str, object]:
    """Build a uniform result dict for a failed request."""

    return {
        "url": url,
        "final_url": "",
        "status": 0,
        "content_type": "",
        "type": "error",
        "redirects": 0,
        "title": "",
        "html": "",
        "error": message,
    }


def inspect_url(
    url: str,
) -> dict[str, object]:
    """
    Fetch a URL and describe it.

    Errors are reported inside the returned dict rather than raised,
    so callers do not need three separate exception handlers.
    """

    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(
                8.0,
                connect=3.0,
            ),
            headers={
                "User-Agent": USER_AGENT,
            },
        ) as client:
            response = client.get(url)

    except httpx.TimeoutException:
        return _error_result(
            url,
            "Request timed out",
        )

    except (httpx.RequestError, ValueError) as error:
        return _error_result(
            url,
            str(error),
        )

    content_type = response.headers.get(
        "content-type",
        "",
    ).lower()

    final_url = str(response.url)

    result: dict[str, object] = {
        "url": url,
        "final_url": final_url,
        "status": response.status_code,
        "content_type": content_type,
        "type": get_url_type(
            final_url,
            content_type,
        ),
        "redirects": len(response.history),
        "title": "",
        "html": "",
        "error": "",
    }

    if content_type.startswith("text/html"):
        result["title"] = extract_title(response.text)
        result["html"] = response.text

    return result

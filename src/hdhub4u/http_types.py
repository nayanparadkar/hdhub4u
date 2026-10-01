"""Shared HTTP helpers.

This module is the single source of truth for the media content types
and the User-Agent used by every outbound request, so that the resolver,
the downloader and the inspector can never disagree about what counts
as downloadable media.
"""

from __future__ import annotations

from urllib.parse import urlparse

MEDIA_CONTENT_TYPES = (
    "video/",
    "audio/",
    "application/octet-stream",
)

#: File extensions for the media types we recognise. Used to give a
#: saved file an extension, because a media file without one is not
#: recognised by a player or by the operating system.
MEDIA_EXTENSIONS: dict[str, str] = {
    "video/mp4": ".mp4",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "video/x-msvideo": ".avi",
    "video/quicktime": ".mov",
    "video/x-m4v": ".m4v",
    "video/mpeg": ".mpg",
    "video/x-ms-wmv": ".wmv",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/aac": ".aac",
}

#: Fallback when the server says nothing useful. Most movie links
#: are Matroska or MP4, so either is a better guess than none.
DEFAULT_MEDIA_EXTENSION = ".mp4"

#: Extensions recognised on a URL path, used when the server sends no
#: content-type or a generic one.
KNOWN_MEDIA_SUFFIXES = (
    ".mp4",
    ".mkv",
    ".webm",
    ".avi",
    ".mov",
    ".m4v",
    ".mp3",
    ".m4a",
    ".flac",
    ".wav",
    ".ogg",
    ".aac",
)

USER_AGENT = (
    "Mozilla/5.0 "
    "(X11; Linux x86_64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/151.0 Safari/537.36"
)

DOWNLOADER_USER_AGENT = "MediaDownloader/1.0"


def normalize_content_type(
    content_type: str,
) -> str:
    """
    Return a content type without parameters.

    ``video/mp4; charset=utf-8`` becomes ``video/mp4``.
    """

    return (
        content_type
        .lower()
        .split(";", 1)[0]
        .strip()
    )


def is_media_content_type(
    content_type: str,
) -> bool:
    """Return True when a response looks like downloadable media."""

    normalized = normalize_content_type(
        content_type
    )

    if not normalized:
        return False

    return normalized.startswith(
        MEDIA_CONTENT_TYPES
    )


def extension_from_content_type(
    content_type: str,
) -> str:
    """
    Return the file extension for a media content type.

    Returns an empty string when the type is unknown or is not
    media, so a caller never invents an extension for a page.
    """

    normalized = normalize_content_type(
        content_type
    )

    if not normalized:
        return ""

    extension = MEDIA_EXTENSIONS.get(
        normalized
    )

    if extension:
        return extension

    # A specific subtype with an unknown prefix, such as
    # video/something-new, is still media.
    if normalized.startswith(
        ("video/", "audio/")
    ):
        return DEFAULT_MEDIA_EXTENSION

    return ""


def extension_from_url(
    url: str,
) -> str:
    """
    Return the media extension implied by a URL path.

    Returns an empty string when the path carries no recognised
    media suffix.
    """

    path = urlparse(
        url
    ).path.lower()

    for extension in KNOWN_MEDIA_SUFFIXES:
        if path.endswith(extension):
            return extension

    return ""


def resolve_extension(
    url: str,
    content_type: str,
) -> str:
    """
    Return the best extension for a media response.

    The URL is consulted first because a file host that sends
    ``application/octet-stream`` for everything still names the
    real format in the path. The content type decides when the URL
    is uninformative, and the media content types fall back to a
    common container.

    Returns an empty string when neither source identifies the
    response as media.
    """

    extension = extension_from_url(url)

    if extension:
        return extension

    if not is_media_content_type(content_type):
        return ""

    return (
        extension_from_content_type(content_type)
        or DEFAULT_MEDIA_EXTENSION
    )

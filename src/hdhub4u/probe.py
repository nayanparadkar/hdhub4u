"""Probe candidate links so only real downloads are offered.

A media page advertises several download options, and some hosts
answer with an HTML interstitial rather than a file. Choosing by
resolution alone therefore offers options that cannot be
downloaded. Probing first turns that into a plain statement about
which options work and, for the rest, why not.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from .http_types import resolve_extension
from .logging_setup import get_logger
from .resolver import HostAdapterRegistry, LinkResolver

logger = get_logger(__name__)

#: Content types that mean the host wants a browser, not a client.
HTML_CONTENT_TYPES = (
    "text/html",
    "application/xhtml",
)


@dataclass(frozen=True)
class ProbeResult:
    """What one candidate link turned out to be."""

    url: str
    is_media: bool
    status_code: int
    content_type: str
    extension: str
    host: str
    reason: str = ""

    @property
    def label(self) -> str:
        """Return a short terminal label for this result."""

        if self.is_media:
            return "downloadable"

        return self.reason or "not downloadable"

    def describe(self) -> str:
        """Return one line describing the outcome."""

        if self.is_media:
            return (
                f"{self.host} "
                f"[{self.status_code}] "
                f"{self.content_type} "
                f"{self.extension}"
            )

        return (
            f"{self.host} "
            f"[{self.status_code}] "
            f"{self.reason}"
        )


def classify_failure(
    status_code: int,
    content_type: str,
) -> str:
    """
    Return why a link is not downloadable.

    The wording names the actual obstacle so the user can tell a
    link needing a browser apart from a dead link.
    """

    normalized = (
        content_type
        .lower()
        .split(";", 1)[0]
        .strip()
    )

    # The status is checked first: a 403 carrying an HTML body is a
    # refusal, and calling it a browser gate would misdescribe it.
    if status_code in (401, 403):
        return f"blocked: HTTP {status_code}"

    if status_code == 404:
        return "missing: HTTP 404"

    if status_code == 429:
        return "throttled: too many requests"

    if 500 <= status_code < 600:
        return f"server error: HTTP {status_code}"

    if normalized.startswith(
        HTML_CONTENT_TYPES
    ):
        return (
            "gated: the server returned a web page, "
            "which means it needs a browser"
        )

    if not normalized:
        return (
            "unclear: the server sent no content-type"
        )

    return (
        f"not a media file: {normalized}"
    )


class OptionProbe:
    """Resolves candidate links and records what each one serves."""

    def __init__(
        self,
        resolver: LinkResolver,
        registry: HostAdapterRegistry | None = None,
    ) -> None:
        self.resolver = resolver
        self.registry = registry

    def probe(
        self,
        url: str,
    ) -> ProbeResult:
        """
        Resolve one URL and describe the outcome.

        Never raises: an unusable URL produces a result carrying
        the reason, so a caller can report every option at once
        rather than stopping at the first bad one.
        """

        host = urlparse(url).hostname or ""

        resolver = self.resolver

        if self.registry is not None:
            resolver = self.registry.resolver_for(
                url,
                fallback=self.resolver,
            )

        try:
            resolved = resolver.resolve(url)

        except httpx.HTTPStatusError as error:
            # The server answered with a status the resolver treats
            # as fatal. The status still explains the link better
            # than the exception text does.
            status = error.response.status_code

            logger.info(
                "probe got HTTP %d for %s",
                status,
                host or url,
            )

            return ProbeResult(
                url=url,
                is_media=False,
                status_code=status,
                content_type=(
                    error.response.headers.get(
                        "content-type",
                        "",
                    )
                ),
                extension="",
                host=host,
                reason=classify_failure(
                    status,
                    error.response.headers.get(
                        "content-type",
                        "",
                    ),
                ),
            )

        except Exception as error:
            logger.warning(
                "probe failed for %s: %s",
                host or url,
                error,
            )

            return ProbeResult(
                url=url,
                is_media=False,
                status_code=0,
                content_type="",
                extension="",
                host=host,
                reason=f"unreachable: {error}",
            )

        if not resolved.is_media:
            return ProbeResult(
                url=url,
                is_media=False,
                status_code=resolved.status_code,
                content_type=resolved.content_type,
                extension="",
                host=host,
                reason=classify_failure(
                    resolved.status_code,
                    resolved.content_type,
                ),
            )

        return ProbeResult(
            url=url,
            is_media=True,
            status_code=resolved.status_code,
            content_type=resolved.content_type,
            extension=resolve_extension(
                resolved.final_url,
                resolved.content_type,
            ),
            host=host,
        )

    def probe_all(
        self,
        options: list[dict[str, str]],
    ) -> list[ProbeResult]:
        """Probe every option, preserving the given order."""

        return [
            self.probe(
                str(option.get("url", ""))
            )
            for option in options
        ]


def group_by_downloadability(
    results: list[ProbeResult],
) -> tuple[
    list[ProbeResult],
    list[ProbeResult],
]:
    """
    Split results into downloadable and unusable.

    Returns:
        (downloadable, unusable), each in the original order.
    """

    usable = [
        result
        for result in results
        if result.is_media
    ]

    blocked = [
        result
        for result in results
        if not result.is_media
    ]

    return (usable, blocked)


def summarize_gating(
    results: list[ProbeResult],
) -> str:
    """
    Return a one-line count of what can and cannot be downloaded.

    Used to tell the user the situation before they commit to an
    option that cannot work.
    """

    usable, blocked = group_by_downloadability(
        results
    )

    total = len(results)

    if not blocked:
        return (
            "All options are directly "
            "downloadable."
            if total != 1
            else "The option is directly "
            "downloadable."
        )

    hosts: list[str] = []

    for result in blocked:
        if (
            result.host
            and result.host not in hosts
        ):
            hosts.append(result.host)

    return (
        f"{len(usable)} of {total} option"
        f"{'' if total == 1 else 's'} directly "
        f"downloadable. Not downloadable: "
        f"{', '.join(hosts)}."
    )

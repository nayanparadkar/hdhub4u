"""Link resolution for download URLs.

Resolution answers one question: does this URL hand back a media file,
and what does the server end up serving? Sites differ in how they gate
links, so the logic is split behind a :class:`LinkResolver` protocol
and composed by :func:`build_resolver`.

The resolved URL is for display only. Downloads must use the original
URL, because signed query parameters are part of the authorization
and are lost once a redirect is followed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

import httpx

from .http_types import USER_AGENT, is_media_content_type
from .logging_setup import get_logger

logger = get_logger(__name__)

#: Hosts known to answer a HEAD request with HTML instead of media.
HTML_STATUS_FALLBACK = frozenset({
    403,
    405,
    501,
})


@dataclass(frozen=True)
class ResolveResult:
    """The outcome of resolving one candidate URL."""

    original_url: str
    final_url: str
    status_code: int
    content_type: str
    is_media: bool
    redirects: int
    strategy: str = "direct"

    def describe(self) -> str:
        """Return a one-line summary for the terminal."""

        return (
            f"{self.final_url} "
            f"[{self.status_code}] "
            f"{self.content_type or 'unknown'} "
            f"({self.redirects} redirect"
            f"{'' if self.redirects == 1 else 's'}, "
            f"via {self.strategy})"
        )


@runtime_checkable
class LinkResolver(Protocol):
    """
    Resolves a download URL to something the server will stream.

    Implementations must never raise for an unusable URL; return a
    result with ``is_media=False`` instead, so the caller can report
    a precise reason rather than a traceback.
    """

    name: str

    def resolve(
        self,
        url: str,
    ) -> ResolveResult:
        """Attempt to resolve one URL."""
        ...


class DirectResolver:
    """
    Follows ordinary HTTP redirects.

    Tries HEAD first because it is cheap, then falls back to a
    one-byte ranged GET for servers that reject HEAD or answer with
    an HTML error page.
    """

    name = "direct"

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        connect_timeout: float = 5.0,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.user_agent = user_agent

    def resolve(
        self,
        url: str,
    ) -> ResolveResult:
        headers = {
            "User-Agent": self.user_agent,
        }

        timeout = httpx.Timeout(
            self.timeout,
            connect=self.connect_timeout,
        )

        with httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers=headers,
        ) as client:

            response = client.head(url)

            if response.status_code in HTML_STATUS_FALLBACK:
                response.close()

                response = client.get(
                    url,
                    headers={
                        **headers,
                        "Range": "bytes=0-0",
                    },
                )

            response.raise_for_status()

            content_type = response.headers.get(
                "content-type",
                "",
            )

            return ResolveResult(
                original_url=url,
                final_url=str(response.url),
                status_code=response.status_code,
                content_type=content_type,
                is_media=is_media_content_type(
                    content_type
                ),
                redirects=len(response.history),
                strategy=self.name,
            )


class ChainResolver:
    """
    Tries each strategy in order and returns the first success.

    A strategy only fails when the URL is unusable. When every
    strategy declines, the result from the first one is returned so
    the caller sees the original server's own content type rather
    than a generic message.
    """

    name = "chain"

    def __init__(
        self,
        resolvers: list[LinkResolver],
    ) -> None:
        if not resolvers:
            raise ValueError(
                "ChainResolver needs at least one strategy"
            )

        self.resolvers = resolvers

    def resolve(
        self,
        url: str,
    ) -> ResolveResult:
        first: ResolveResult | None = None

        for resolver in self.resolvers:
            try:
                result = resolver.resolve(url)

            except Exception as error:
                logger.warning(
                    "resolver %s failed: %s",
                    resolver.name,
                    error,
                )

                continue

            if first is None:
                first = result

            if result.is_media:
                logger.info(
                    "resolved via %s: %s",
                    resolver.name,
                    result.describe(),
                )

                return result

            logger.info(
                "resolver %s declined (%s)",
                resolver.name,
                result.content_type or "no content-type",
            )

        if first is None:
            raise ValueError(
                "No resolver strategy could attempt the URL"
            )

        return first


def build_resolver(
    *,
    timeout: float = 15.0,
    connect_timeout: float = 5.0,
    user_agent: str = USER_AGENT,
    strategies: list[LinkResolver] | None = None,
) -> LinkResolver:
    """
    Return the resolver to use.

    Pass ``strategies`` to insert additional strategies ahead of
    the direct one; anything that resolves a gated link goes there.
    """

    chain: list[LinkResolver] = list(strategies or [])

    chain.append(
        DirectResolver(
            timeout=timeout,
            connect_timeout=connect_timeout,
            user_agent=user_agent,
        )
    )

    if len(chain) == 1:
        return chain[0]

    return ChainResolver(chain)


class HostAdapterRegistry:
    """
    Per-host resolution strategies, keyed by hostname.

    A host that needs more than a plain redirect walk gets its own
    strategy here, and the chain is rebuilt for that host only. The
    direct resolver stays the fallback for every host, so adding an
    adapter never removes existing behaviour.
    """

    def __init__(
        self,
        adapters: dict[
            str,
            type[LinkResolver],
        ] | None = None,
    ) -> None:
        self.adapters: dict[
            str,
            type[LinkResolver],
        ] = dict(adapters or {})

    def register(
        self,
        host: str,
        factory: type[LinkResolver],
    ) -> None:
        """Register a strategy factory for one hostname."""

        self.adapters[_normalize_host(host)] = factory

    def matches(
        self,
        url: str,
    ) -> type[LinkResolver] | None:
        """
        Return the factory handling a URL, or None.

        A registered parent domain covers its subdomains, so
        ``example.com`` handles ``cdn.example.com``.
        """

        host = _normalize_host(url)

        if not host:
            return None

        if host in self.adapters:
            return self.adapters[host]

        parts = host.split(".")

        for index in range(
            1,
            len(parts) - 1,
        ):
            parent = ".".join(
                parts[index:]
            )

            if parent in self.adapters:
                return self.adapters[parent]

        return None

    def resolver_for(
        self,
        url: str,
        fallback: LinkResolver | None = None,
        **kwargs,
    ) -> LinkResolver:
        """
        Return a resolver built for one URL.

        The URL's own adapter goes first, then the direct resolver,
        so an adapter that declines still leaves a working attempt.
        When ``fallback`` is given it replaces the direct resolver,
        which lets a caller supply a resolver that makes no network
        calls.
        """

        factory = self.matches(url)

        strategies: list[LinkResolver] = []

        if factory is not None:
            strategies.append(factory(**kwargs))

        if fallback is not None:
            chain = ChainResolver(
                [*strategies, fallback]
            )

            return chain

        return build_resolver(
            strategies=strategies,
            **kwargs,
        )


def _normalize_host(
    value: str,
) -> str:
    """
    Return a bare lowercase hostname.

    Accepts either a hostname or a full URL so callers can pass
    either form.
    """

    candidate = value.strip().lower()

    if "//" not in candidate:
        candidate = f"//{candidate}"

    return (
        urlparse(candidate)
        .hostname
        or ""
    )

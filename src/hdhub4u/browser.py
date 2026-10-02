"""Optional browser support for pages that need JavaScript.

Some pages only exist once client-side script runs: a plain HTTP
fetch returns a shell, so the indexer records a placeholder title
and the download list comes back empty. Rendering those pages in a
real browser fixes that class of problem, and the same browser can
drive this application's own interface in a test.

Playwright is an optional dependency. Everything here degrades to a
clear :class:`BrowserUnavailable` error when it is not installed, so
no caller has to import it directly and the core tool keeps working
without a browser.

What this module deliberately does not do is drive a link gate. It
renders a page and reports what the DOM advertises; it does not
click through an interstitial, wait out a countdown, or read a
token out of script state. Those hosts are answered with an honest
"this needs a browser" message instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .errors import HdHubError
from .http_types import USER_AGENT
from .logging_setup import get_logger

logger = get_logger(__name__)

#: Seconds allowed for a page to settle.
DEFAULT_TIMEOUT_MS = 20_000

#: How long to wait for the network to go quiet after load.
DEFAULT_SETTLE_MS = 800

#: Suffixes treated as a direct media source when a page embeds one.
MEDIA_SOURCE_SUFFIXES = (
    ".mp4",
    ".mkv",
    ".webm",
    ".m4v",
    ".mov",
    ".mp3",
    ".m4a",
    ".flac",
)


class BrowserUnavailable(HdHubError):
    """Playwright or its browser binaries are not installed."""


@dataclass(frozen=True)
class RenderedPage:
    """What a browser saw after the page settled."""

    url: str
    final_url: str
    html: str
    status_code: int
    title: str

    @property
    def is_empty_shell(self) -> bool:
        """
        Return True when rendering produced almost no markup.

        A shell like this is the signature of a page whose content
        never arrived, so callers can fall back rather than index
        nothing.
        """

        return len(self.html) < 500


def browser_status() -> str:
    """
    Return a one-line description of browser availability.

    Used by ``hdhub4u status`` so a missing browser is visible
    before a crawl silently produces placeholder titles.
    """

    try:
        from playwright.sync_api import (
            sync_playwright,
        )

    except ImportError:
        return (
            "not installed (pip install playwright, "
            "then: playwright install chromium)"
        )

    try:
        with sync_playwright() as driver:
            browser = driver.chromium.launch()

            browser.close()

    except Exception:
        return (
            "playwright present but no browser binary: "
            "run 'playwright install chromium'"
        )

    return "ready (chromium)"


class BrowserSession:
    """
    A reusable headless browser.

    Launching Chromium costs about a second, so one session is
    shared across a whole crawl rather than paying that per page.
    Use as a context manager so the process is always cleaned up.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.user_agent = user_agent

        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None

    def __enter__(self) -> BrowserSession:
        self.start()

        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def start(self) -> None:
        """
        Launch the browser.

        Raises:
            BrowserUnavailable: when Playwright or its binaries are
                missing.
        """

        if self._context is not None:
            return

        try:
            from playwright.sync_api import (
                sync_playwright,
            )

        except ImportError as error:
            raise BrowserUnavailable(
                "Playwright is not installed. "
                "Run: pip install playwright"
            ) from error

        try:
            self._playwright = sync_playwright().start()

            self._browser = (
                self._playwright.chromium.launch(
                    headless=self.headless
                )
            )

            self._context = (
                self._browser.new_context(
                    user_agent=self.user_agent,
                    ignore_https_errors=True,
                )
            )

        except Exception as error:
            self.close()

            raise BrowserUnavailable(
                "Could not launch chromium. "
                "Run: playwright install chromium "
                f"({error})"
            ) from error

    def close(self) -> None:
        """Close the browser, ignoring teardown errors."""

        for attribute in (
            "_context",
            "_browser",
            "_playwright",
        ):
            resource = getattr(
                self,
                attribute,
                None,
            )

            if resource is None:
                continue

            try:
                if attribute == "_playwright":
                    resource.stop()
                else:
                    resource.close()

            except Exception:
                logger.debug(
                    "error closing %s",
                    attribute,
                )

            setattr(self, attribute, None)

    def render(
        self,
        url: str,
        *,
        wait_for: str | None = None,
        settle_ms: int = DEFAULT_SETTLE_MS,
    ) -> RenderedPage:
        """
        Load a URL and return the DOM after scripts have run.

        Args:
            url: the page to load.
            wait_for: an optional selector to wait for before
                reading the DOM, for pages that populate
                asynchronously.
            settle_ms: quiet time to allow after load.

        Returns:
            The rendered page.

        Raises:
            BrowserUnavailable: when the session is not started or
                the browser cannot be launched.
        """

        if self._context is None:
            self.start()

        assert self._context is not None

        page = self._context.new_page()

        try:
            page.set_default_timeout(
                self.timeout_ms
            )

            response = page.goto(
                url,
                wait_until="domcontentloaded",
            )

            if wait_for:
                try:
                    page.wait_for_selector(
                        wait_for,
                        timeout=self.timeout_ms,
                    )

                except Exception:
                    logger.debug(
                        "selector %s never appeared on %s",
                        wait_for,
                        url,
                    )

            page.wait_for_timeout(settle_ms)

            html = page.content()

            return RenderedPage(
                url=url,
                final_url=page.url,
                html=html,
                status_code=(
                    response.status
                    if response is not None
                    else 0
                ),
                title=page.title(),
            )

        finally:
            try:
                page.close()

            except Exception:
                logger.debug(
                    "could not close page"
                )

    def find_media_sources(
        self,
        url: str,
        *,
        settle_ms: int = DEFAULT_SETTLE_MS,
    ) -> list[str]:
        """
        Return direct media URLs a page advertises in its DOM.

        Looks at ``<video>``, ``<audio>`` and ``<source>`` elements,
        which is how a page embeds a playable file. A URL is only
        accepted when its path carries a media suffix, so a player
        pointing at a script is not mistaken for media.

        This is a read of the rendered DOM. It does not interact
        with the page.

        Returns:
            Absolute media URLs, in document order.
        """

        if self._context is None:
            self.start()

        assert self._context is not None

        page = self._context.new_page()

        try:
            page.set_default_timeout(
                self.timeout_ms
            )

            page.goto(
                url,
                wait_until="domcontentloaded",
            )

            page.wait_for_timeout(settle_ms)

            candidates = page.eval_on_selector_all(
                "video[src], audio[src], "
                "video source[src], "
                "audio source[src]",
                """elements => elements.map(
                    element => element.getAttribute('src')
                )""",
            )

        finally:
            try:
                page.close()

            except Exception:
                logger.debug(
                    "could not close page"
                )

        found: list[str] = []
        seen: set[str] = set()

        for candidate in candidates or []:
            if not isinstance(candidate, str):
                continue

            value = candidate.strip()

            if not value or value in seen:
                continue

            if value.startswith(
                ("data:", "blob:", "javascript:")
            ):
                continue

            path = value.split("?")[0].lower()

            if not path.endswith(
                MEDIA_SOURCE_SUFFIXES
            ):
                continue

            seen.add(value)

            found.append(value)

        return found

    def screenshot(
        self,
        url: str,
        destination: str,
        *,
        full_page: bool = True,
        settle_ms: int = DEFAULT_SETTLE_MS,
    ) -> str:
        """
        Save a screenshot of a page.

        Useful for seeing what a page actually shows when a link
        fails, instead of inferring it from a content type.

        Returns:
            The path written.
        """

        if self._context is None:
            self.start()

        assert self._context is not None

        page = self._context.new_page()

        try:
            page.goto(
                url,
                wait_until="domcontentloaded",
            )

            page.wait_for_timeout(settle_ms)

            page.screenshot(
                path=destination,
                full_page=full_page,
            )

        finally:
            try:
                page.close()

            except Exception:
                logger.debug(
                    "could not close page"
                )

        return destination


_SHELL_MARKERS = re.compile(
    r"<(main|article|div)\b",
    re.IGNORECASE,
)


def looks_unrendered(
    html: str,
) -> bool:
    """
    Return True when HTML looks like an unrendered page shell.

    Used to decide whether a fetch should be retried through a
    browser, so the decision does not depend on page length alone.
    """

    if len(html) < 500:
        return True

    if "<body" not in html.lower():
        return True

    return not _SHELL_MARKERS.search(html)

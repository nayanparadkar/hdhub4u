"""Crawler that builds the local media index.

Uses an explicit work queue rather than recursion so a long crawl can
be bounded by ``max_pages`` and by a wall-clock budget.
"""

from __future__ import annotations

import os
import time
from collections import deque
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .browser import (
    BrowserSession,
    BrowserUnavailable,
    looks_unrendered,
)
from .database import (
    add_media_batch,
    backfill_titles,
    get_media_count,
    initialize_database,
    placeholder_title_count,
)
from .errors import NetworkError
from .http import with_retries
from .logging_setup import get_logger
from .parser import (
    extract_content_links,
)
from .parser import fetch_page as _fetch_page
from .parser import is_category_url, is_excluded_path

logger = get_logger(__name__)

START_URL = "https://new1.hdhub4u.free/"

MAX_PAGES = 50

#: Stop the crawl after this many seconds so it cannot run away.
TIME_BUDGET_SECONDS = 300.0

#: Pause between page requests to stay polite.
REQUEST_DELAY_SECONDS = 0.5

#: Set to "1" to render pages through a real browser. Off by
#: default because launching a browser costs about a second per
#: start, and most pages on this site render fine over plain HTTP.
USE_BROWSER_ENV = "HDHUB_USE_BROWSER"

#: Set to "1" to retry a page through the browser only when the
#: response looks like an unrendered shell, rather than for every
#: page.
USE_BROWSER_ON_SHELL_ENV = "HDHUB_BROWSER_ON_SHELL"


def _env_flag(
    name: str,
) -> bool:
    """Return True when an environment variable is switched on."""

    return (
        os.environ.get(
            name,
            "",
        )
        .strip()
        .lower()
        in {
            "1",
            "true",
            "yes",
            "on",
        }
    )


class CrawlBudget:
    """Tracks page count and elapsed time for one crawl."""

    def __init__(
        self,
        max_pages: int = MAX_PAGES,
        seconds: float = TIME_BUDGET_SECONDS,
    ) -> None:
        self.max_pages = max_pages
        self.seconds = seconds
        self.started = time.monotonic()

    @property
    def elapsed(self) -> float:
        """Return how long the crawl has been running."""

        return time.monotonic() - self.started

    def expired(self) -> bool:
        """Return True once the time budget is spent."""

        return self.elapsed > self.seconds


def fetch_page(
    url: str,
    *,
    attempts: int = 3,
    session: BrowserSession | None = None,
    render: bool = False,
    render_on_shell: bool = False,
) -> tuple[str, str, int]:
    """
    Fetch one page with retries.

    When ``render`` is set the page is loaded in a real browser, so
    content that only appears after client-side script runs is
    indexed instead of being recorded as a placeholder. When
    ``render_on_shell`` is set the browser is used only when the
    plain response looks like an empty shell.

    Returns:
        (final_url, html, status_code).

    Raises:
        NetworkError: when every attempt fails.
        BrowserUnavailable: when rendering was asked for but no
            browser is installed.
    """

    def _fetch() -> tuple[str, str, int]:
        plain = with_retries(
            lambda: _fetch_page(url),
            attempts=attempts,
        )

        final_url, html, status = plain

        needs_browser = render or (
            render_on_shell
            and looks_unrendered(html)
        )

        if not needs_browser:
            return plain

        if session is None:
            raise BrowserUnavailable(
                "Rendering was requested but no browser "
                "session was supplied."
            )

        rendered = session.render(url)

        if rendered.is_empty_shell:
            # The browser saw no more than the plain fetch did, so
            # the original response is the better answer.
            logger.debug(
                "rendering %s produced a shell",
                url,
            )

            return plain

        logger.info(
            "rendered %s (%d bytes)",
            url,
            len(rendered.html),
        )

        return (
            rendered.final_url,
            rendered.html,
            rendered.status_code or status,
        )

    return _fetch()


def is_same_domain(
    url: str,
    base_url: str,
) -> bool:
    """Return True when both URLs share a host."""

    return (
        urlparse(url).netloc
        == urlparse(base_url).netloc
    )


def is_allowed_page(
    url: str,
    base_url: str,
) -> bool:
    """Return True when a URL is a crawlable page on the same site."""

    if not is_same_domain(url, base_url):
        return False

    if is_excluded_path(url):
        return False

    path = urlparse(url).path.lower()

    if is_category_url(url):
        return True

    if not path or path == "/":
        return False

    return True


def extract_category_links(
    html: str,
    base_url: str,
) -> list[str]:
    """Return unique category links found in a page."""

    soup = BeautifulSoup(html, "html.parser")

    results: list[str] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href")

        if not isinstance(href, str):
            continue

        href = href.strip()

        if not href:
            continue

        url = urljoin(base_url, href)

        if not is_category_url(url):
            continue

        if not is_allowed_page(url, base_url):
            continue

        if url in seen:
            continue

        seen.add(url)
        results.append(url)

    return results


def index_category(
    url: str,
    *,
    session: BrowserSession | None = None,
    render: bool = False,
    render_on_shell: bool = False,
) -> tuple[int, str, str]:
    """
    Index one category page.

    Returns:
        (items_added, html, final_url). The html is empty when the
        page could not be fetched.
    """

    try:
        final_url, html, status = fetch_page(
            url,
            session=session,
            render=render,
            render_on_shell=render_on_shell,
        )

    except NetworkError as error:
        logger.error(
            "failed to fetch %s: %s",
            url,
            error,
        )

        return (0, "", url)

    except Exception as error:
        logger.error(
            "failed to fetch %s: %s",
            url,
            error,
        )

        return (0, "", url)

    logger.info(
        "[%d] %s (%d items)",
        status,
        final_url,
        len(
            extract_content_links(html, final_url)
        ),
    )

    content_links = extract_content_links(
        html,
        final_url,
    )

    if content_links:
        add_media_batch(
            [
                (
                    item["title"],
                    item["url"],
                    item["type"],
                    item.get("image", ""),
                )
                for item in content_links
            ]
        )

    return (len(content_links), html, final_url)


def build_index(
    start_url: str = START_URL,
    *,
    max_pages: int = MAX_PAGES,
    time_budget: float = TIME_BUDGET_SECONDS,
    delay: float = REQUEST_DELAY_SECONDS,
    use_browser: bool | None = None,
) -> int:
    """
    Crawl category pages and populate the local index.

    ``use_browser`` renders each page in a real browser. It
    defaults to the ``HDHUB_USE_BROWSER`` environment variable, so
    the capability is available without changing any call site.

    Returns:
        The number of media items found during this run.
    """

    initialize_database()

    if use_browser is None:
        use_browser = _env_flag(
            USE_BROWSER_ENV
        )

    render_on_shell = _env_flag(
        USE_BROWSER_ON_SHELL_ENV
    )

    if use_browser and not render_on_shell:
        render_on_shell = False

    budget = CrawlBudget(
        max_pages,
        time_budget,
    )

    session: BrowserSession | None = None

    if use_browser:
        session = BrowserSession()

        try:
            session.start()

        except BrowserUnavailable as error:
            logger.error(
                "browser unavailable: %s",
                error,
            )

            return 0

    try:
        return _crawl(
            start_url,
            budget=budget,
            delay=delay,
            session=session,
            render=use_browser,
            render_on_shell=render_on_shell,
        )

    finally:
        if session is not None:
            session.close()


def _crawl(
    start_url: str,
    *,
    budget: CrawlBudget,
    delay: float,
    session: BrowserSession | None,
    render: bool,
    render_on_shell: bool,
) -> int:
    """Walk category pages, rendering through the browser if asked."""

    try:
        (
            homepage_url,
            homepage_html,
            _,
        ) = fetch_page(
            start_url,
            session=session,
            render=render,
            render_on_shell=render_on_shell,
        )

    except Exception as error:
        logger.error(
            "could not reach %s: %s",
            start_url,
            error,
        )

        return 0

    queue = deque(
        extract_category_links(
            homepage_html,
            homepage_url,
        )
    )

    visited: set[str] = set()
    total_found = 0

    while (
        queue
        and len(visited) < budget.max_pages
        and not budget.expired()
    ):
        url = queue.popleft()

        if url in visited:
            continue

        visited.add(url)

        found, html, final_url = index_category(
            url,
            session=session,
            render=render,
            render_on_shell=render_on_shell,
        )

        total_found += found

        if not html:
            continue

        for category_url in extract_category_links(
            html,
            final_url,
        ):
            if (
                category_url not in visited
                and category_url not in queue
            ):
                queue.append(category_url)

        logger.info(
            "queue: %d pending, %d visited, %.0fs",
            len(queue),
            len(visited),
            budget.elapsed,
        )

        if delay:
            time.sleep(delay)

    repaired = backfill_titles()
    remaining = placeholder_title_count()

    logger.info(
        "crawl finished: %d pages, %d items, "
        "%d titles repaired, %d rows total, "
        "%d placeholders left",
        len(visited),
        total_found,
        repaired,
        get_media_count(),
        remaining,
    )

    return total_found


def main() -> None:
    """Console entry point."""

    build_index()


if __name__ == "__main__":
    main()

"""Command-line entry point.

Supports both an interactive session and non-interactive subcommands so
the tool can be scripted:

    hdhub4u                     interactive live search
    hdhub4u search "big boss"    search the site, no prompts
    hdhub4u browse "big boss"    interactive pick, list and download
    hdhub4u search "x" --local   search the offline index instead
    hdhub4u index               rebuild the local index
    hdhub4u status              show where data is stored
    hdhub4u links prune         drop expired cached resolutions

``search`` and ``browse`` talk to the live site. The offline index
built by ``index`` is still available behind ``--local`` and the
``browse`` subcommand, but it is only correct up to the moment it was
built, so it is not the default.
"""

from __future__ import annotations

import argparse
import sys

from .browser import BrowserSession, BrowserUnavailable
from .cli import show_status
from .database import get_media_count, placeholder_title_count
from .flow import interactive as live_interactive
from .flow import run_search as live_search
from .indexer import build_index
from .link_cache import LinkCache
from .logging_setup import configure_logging
from .project import get_link_cache_path
from .search import search_media


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser."""

    parser = argparse.ArgumentParser(
        prog="hdhub4u",
        description=(
            "Search and download from the live site, or from a "
            "local index with --local."
        ),
    )

    parser.add_argument(
        "--log-level",
        default=None,
        help=(
            "logging level: DEBUG, INFO, WARNING or ERROR "
            "(default: WARNING)"
        ),
    )

    subparsers = parser.add_subparsers(
        dest="command",
    )

    parser.add_argument(
        "--browser",
        action="store_true",
        help=(
            "render pages in a real browser where a "
            "command supports it"
        ),
    )

    index_parser = subparsers.add_parser(
        "index",
        help="rebuild the local index by crawling",
    )

    index_parser.add_argument(
        "--browser",
        action="store_true",
        help=(
            "render each page in a real browser, for "
            "content that only appears after scripts run"
        ),
    )

    render_parser = subparsers.add_parser(
        "render",
        help=(
            "load a URL in a real browser and report what "
            "the rendered page contains"
        ),
    )

    render_parser.add_argument(
        "url",
        help="the page to load",
    )

    render_parser.add_argument(
        "--screenshot",
        default=None,
        help="also save a PNG of the rendered page",
    )

    render_parser.add_argument(
        "--selector",
        default=None,
        help=(
            "wait for this CSS selector before "
            "reading the page"
        ),
    )

    search_parser = subparsers.add_parser(
        "search",
        help="search the site without prompts",
    )

    search_parser.add_argument(
        "query",
        help="title to search for",
    )

    search_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="maximum results to print",
    )

    search_parser.add_argument(
        "--local",
        action="store_true",
        help="search the offline index instead",
    )

    browse_parser = subparsers.add_parser(
        "browse",
        help="interactively browse and download",
    )

    browse_parser.add_argument(
        "query",
        nargs="?",
        default="",
        help="title to start with (optional)",
    )

    subparsers.add_parser(
        "status",
        help="show index size and data locations",
    )

    links_parser = subparsers.add_parser(
        "links",
        help="manage the resolved-link cache",
    )

    links_parser.add_argument(
        "action",
        choices=("prune", "clear"),
        help="prune removes expired entries; clear removes all",
    )

    return parser


def run_search(
    query: str,
    limit: int,
) -> int:
    """
    Print results from the offline index.

    Kept as a plain-text helper for the ``--local`` path and for
    callers that want index output without the live tables.
    """

    results = search_media(
        query,
        limit=limit,
    )

    if not results:
        print(f"No matches for {query!r}.")

        return 1

    for index, item in enumerate(
        results,
        start=1,
    ):
        print(
            f"{index:3}. [{item['type']}] "
            f"{item['title']}"
        )
        print(f"     {item['url']}")

    return 0


def run_status() -> int:
    """Print index statistics and data locations."""

    print(f"Indexed items      : {get_media_count()}")

    print(
        "Placeholder titles: "
        f"{placeholder_title_count()}"
    )

    show_status()

    return 0


def run_links(
    action: str,
) -> int:
    """Prune or clear the resolved-link cache."""

    cache = LinkCache(get_link_cache_path())

    if action == "clear":
        cache.clear()

        print("Link cache cleared.")

        return 0

    removed = cache.prune()

    print(f"Removed {removed} expired entries.")

    return 0


def run_render(
    url: str,
    selector: str | None,
    screenshot: str | None,
) -> int:
    """Render one page and report what the browser saw."""

    try:
        with BrowserSession() as session:
            page = session.render(
                url,
                wait_for=selector,
            )

            sources = session.find_media_sources(url)

            if screenshot:
                session.screenshot(
                    url,
                    screenshot,
                )

    except BrowserUnavailable as error:
        print(f"Browser unavailable: {error}")

        return 1

    print(f"Requested : {page.url}")
    print(f"Final     : {page.final_url}")
    print(f"Status    : {page.status_code}")
    print(f"Title     : {page.title}")
    print(f"HTML      : {len(page.html)} bytes")

    if page.is_empty_shell:
        print(
            "Note: the rendered page was almost empty, "
            "so client-side content did not appear."
        )

    if sources:
        print()
        print("Direct media sources in the DOM:")

        for source in sources:
            print(f"  {source}")

    else:
        print()
        print(
            "No direct media source in the DOM. This "
            "page does not expose a file for a download "
            "client to fetch."
        )

    if screenshot:
        print()
        print(f"Screenshot: {screenshot}")

    return 0


def main(
    argv: list[str] | None = None,
) -> int:
    """Parse arguments and dispatch."""

    parser = build_parser()

    args = parser.parse_args(argv)

    configure_logging(args.log_level)

    if args.command == "index":
        build_index(
            use_browser=args.browser,
        )

        return 0

    if args.command == "render":
        return run_render(
            args.url,
            args.selector,
            args.screenshot,
        )

    if args.command == "search":
        if getattr(args, "local", False):
            return run_search(
                args.query,
                args.limit,
            )

        return live_search(
            args.query,
            limit=args.limit,
            interactive=False,
        )

    if args.command == "status":
        return run_status()

    if args.command == "links":
        return run_links(args.action)

    if args.command == "browse":
        return live_interactive(
            getattr(args, "query", "") or "",
        )

    return live_interactive()


def run() -> int:
    """Console script wrapper."""

    try:
        return main()

    except KeyboardInterrupt:
        print()

        return 130


if __name__ == "__main__":
    sys.exit(run())

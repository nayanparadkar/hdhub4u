"""Command-line entry point.

The common case is one search, so it takes no subcommand at all:

    hdhub4u                          interactive search, empty prompt
    hdhub4u dune                     interactive search, query filled in
    hdhub4u search "big boss"        list matches, no prompts
    hdhub4u status                   show where data is stored

Three more commands exist for maintenance and are hidden from
``--help``: ``index`` rebuilds the offline snapshot, ``links`` manages
the resolved-link cache, and ``render`` loads one page in a browser.
They are documented in the README rather than listed, because they are
not how anybody downloads anything.

Output adapts to the destination. On a terminal the results are drawn
as a table; piped, or with ``--json``, they are plain records meant to
be read by something else.

``search`` talks to the live site. The offline index built by
``index`` is still available behind ``--local``, but it is only correct
up to the moment it was built, so it is not the default.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from typing import Any

from .browser import BrowserSession, BrowserUnavailable
from .catalog import title_quality
from .errors import HdHubError
from .flow import interactive as live_interactive
from .flow import run_search as live_search
from .indexer import build_index
from .link_cache import LinkCache
from .logging_setup import configure_logging
from .project import get_link_cache_path
from .search import search_media
from .ui import (
    ResultRow,
    print_error,
    print_results,
    show_status,
)

#: A year as the site's own titles write it, "(2024)".
_YEAR_IN_TITLE = re.compile(r"\((\d{4})\)")

#: Commands advertised in ``--help``. What somebody downloads with.
VISIBLE_COMMANDS = ("search", "browse", "status")

#: Maintenance commands. They work exactly as documented, they are
#: just not part of the front door.
HIDDEN_COMMANDS = ("index", "render", "links")

ALL_COMMANDS = VISIBLE_COMMANDS + HIDDEN_COMMANDS


def _hide_from_help(
    subparsers: Any,
    name: str,
) -> None:
    """
    Drop a subcommand from ``--help`` without disabling it.

    ``help=argparse.SUPPRESS`` does not do this. For a subcommand
    argparse prints the literal string ``==SUPPRESS==`` in place of the
    description, so the command is still listed, only with a worse
    one. Each subcommand's entry in the listing is a pseudo-action, and
    dropping that one leaves the command parsing exactly as before.

    Touches a private attribute because argparse offers no public way
    to say this. The alternative -- inventing a description for a
    command nobody should type -- is worse.

    Args:
        subparsers: The subparsers action holding the listings.
        name: The subcommand to hide.
    """

    subparsers._choices_actions = [
        action
        for action in subparsers._choices_actions
        if action.dest != name
    ]


def _add_log_level(
    parser: argparse.ArgumentParser,
) -> None:
    """
    Add ``--log-level`` to a parser, defaulted so it never overwrites.

    The option is declared on the root parser *and* on every
    subcommand, because ``hdhub4u search x --log-level DEBUG`` is what
    a person types and it used to be refused with "unrecognized
    arguments". A subparser writes its own defaults into the shared
    namespace as it parses, so an ordinary default of ``None`` on the
    subcommand would wipe a level given before the subcommand name.
    Suppressing the default means an absent flag leaves the namespace
    untouched and the caller reads it with a fallback.
    """

    parser.add_argument(
        "--log-level",
        default=argparse.SUPPRESS,
        metavar="LEVEL",
        help=(
            "logging level: DEBUG, INFO, WARNING or ERROR "
            "(default: WARNING)"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser."""

    parser = argparse.ArgumentParser(
        prog="hdhub4u",
        description=(
            "Search and download from the live site. A bare word is "
            "taken as a search: hdhub4u dune"
        ),
    )

    _add_log_level(parser)

    subparsers = parser.add_subparsers(
        dest="command",
        metavar="{" + ",".join(VISIBLE_COMMANDS) + "}",
    )

    index_parser = subparsers.add_parser("index")

    _hide_from_help(subparsers, "index")
    _add_log_level(index_parser)

    index_parser.add_argument(
        "--browser",
        action="store_true",
        help=(
            "render each page in a real browser, for "
            "content that only appears after scripts run"
        ),
    )

    render_parser = subparsers.add_parser("render")

    _hide_from_help(subparsers, "render")

    _add_log_level(render_parser)

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
        help="list matches without prompting",
    )

    _add_log_level(search_parser)

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

    search_parser.add_argument(
        "--json",
        action="store_true",
        help="print one JSON object instead of a table",
    )

    browse_parser = subparsers.add_parser(
        "browse",
        help="interactively browse and download",
    )

    _add_log_level(browse_parser)

    browse_parser.add_argument(
        "query",
        nargs="?",
        default="",
        help="title to start with (optional)",
    )

    status_parser = subparsers.add_parser(
        "status",
        help="show index size and data locations",
    )

    _add_log_level(status_parser)

    links_parser = subparsers.add_parser("links")

    _hide_from_help(subparsers, "links")

    _add_log_level(links_parser)

    links_parser.add_argument(
        "action",
        choices=("prune", "clear"),
        help="prune removes expired entries; clear removes all",
    )

    return parser


def local_rows(
    results: list[dict[str, object]],
) -> list[ResultRow]:
    """
    Convert offline-index hits into the shared printing shape.

    The index knows a title, an address and a type. It stores no
    resolution and no year of its own, so those are read off the
    title, which is where the site wrote them.
    """

    rows: list[ResultRow] = []

    for item in results:
        title = str(item.get("title", ""))

        year_match = _YEAR_IN_TITLE.search(title)

        rows.append(
            ResultRow(
                title=title,
                url=str(item.get("url", "")),
                kind=str(item.get("type", "")),
                quality=title_quality(title),
                year=(
                    year_match.group(1)
                    if year_match
                    else ""
                ),
            )
        )

    return rows


def run_search(
    query: str,
    limit: int,
    as_json: bool = False,
) -> int:
    """
    Print results from the offline index.

    Goes through the same printers as the live search, so ``--local``
    and live agree on what the output looks like. The AC on a pipe is
    the same either way, and the ``--local`` path used to be the only
    one producing plain text.
    """

    rows = local_rows(
        search_media(query, limit=limit)
    )

    print_results(
        rows,
        query=query,
        as_json=as_json,
        page=1,
    )

    return 0 if rows else 1


def run_status() -> int:
    """Print index statistics and data locations."""

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


def _options_taking_a_value(
    parser: argparse.ArgumentParser,
) -> set[str]:
    """
    Return the root options that consume the argument after them.

    Read off the parser rather than hard-coded, so a new root flag
    with a value cannot make :func:`normalise_argv` mistake its value
    for a subcommand.

    ``store_true`` flags declare ``nargs=0`` and are skipped; the
    subparsers action and the positionals carry no option strings and
    are skipped by that test.
    """

    return {
        option
        for action in parser._actions
        if action.option_strings and action.nargs != 0
        for option in action.option_strings
    }


def normalise_argv(
    argv: Sequence[str],
    parser: argparse.ArgumentParser | None = None,
) -> list[str]:
    """
    Rewrite a bare query into an explicit ``browse`` invocation.

    ``hdhub4u dune`` is the same as ``hdhub4u browse dune``. A word
    that is not a command name is taken as the search query, and the
    flags around it are left alone.

    Only *one* bare word is taken, because only one word is what
    quoting is for. ``hdhub4u the big boss`` is a title that lost its
    quotes, and it is refused with an instruction to put them back --
    joining the words instead would mean a typo'd subcommand silently
    became a search for the typo.

    Args:
        argv: The arguments as typed.
        parser: Parser used to report an unquoted title. One is built
            if not given.

    Returns:
        Arguments with a leading query rewritten to ``browse <query>``.

    Raises:
        SystemExit: when several bare words were given.
    """

    tokens = list(argv)

    if not tokens:
        return tokens

    reporting = parser or build_parser()

    value_options = _options_taking_a_value(reporting)

    words: list[tuple[int, str]] = []

    index = 0

    while index < len(tokens):
        token = tokens[index]

        if token in value_options:
            index += 2
            continue

        if token.startswith("-"):
            index += 1
            continue

        words.append((index, token))

        index += 1

    # A command name is left for argparse; so is no bare word at all.
    if not words or words[0][1] in ALL_COMMANDS:
        return tokens

    if len(words) > 1:
        reporting.error(
            "a title with spaces needs quotes: "
            f'hdhub4u "{" ".join(word for _, word in words)}"'
        )

    position, query = words[0]

    # The query moves to the front; every flag stays where it was, so
    # the order a person typed is preserved and nothing is duplicated.
    return [
        "browse",
        query,
        *[
            token
            for offset, token in enumerate(tokens)
            if offset != position
        ],
    ]


def main(
    argv: list[str] | None = None,
) -> int:
    """Parse arguments and dispatch."""

    parser = build_parser()

    tokens = sys.argv[1:] if argv is None else argv

    args = parser.parse_args(
        normalise_argv(tokens, parser)
    )

    # The option is declared on the root parser and on each
    # subcommand, so it may appear on either side of the command
    # name. Absent from both it is not in the namespace at all.
    configure_logging(
        getattr(args, "log_level", None)
    )

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
                as_json=getattr(
                    args,
                    "json",
                    False,
                ),
            )

        return live_search(
            args.query,
            limit=args.limit,
            interactive=False,
            as_json=getattr(
                args,
                "json",
                False,
            ),
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
    """
    Console script wrapper.

    Everything this program raises on purpose derives from
    :class:`HdHubError` and already carries a message written to be
    shown to a person. Letting one of those escape printed a Python
    traceback over the message, which is the least useful way to
    report that a site was unreachable. An interrupt is not a failure
    and exits by the shell's convention instead.
    """

    try:
        return main()

    except HdHubError as error:
        print_error(str(error))

        return 1

    except KeyboardInterrupt:
        print()

        return 130


if __name__ == "__main__":
    sys.exit(run())

"""
The live search-and-download flow.

This is the path a user actually walks: type a name, pick the right
entry from a list, pick a quality, and get the file.

It is deliberately separate from the local-index workflow in
:mod:`hdhub4u.cli`. That one is built around an offline SQLite
snapshot and never touches the network for a query; this one always
asks the live site, so it is correct the moment a new title is
posted and needs no ``index`` step.

The order of operations mirrors the website. Searching goes to the
catalog, the chosen entry's post page is read for its options, and
only then is a gate opened. Nothing is resolved speculatively: a
gate is a multi-hop network walk, so it runs for the one option the
user picked rather than for the whole menu.
"""

from __future__ import annotations

from typing import Sequence

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import catalog, unlock
from .catalog import (
    DOWNLOAD_KIND,
    SITE_ORIGIN,
    CatalogItem,
    MediaOption,
    search_catalog,
)
from .downloader import (
    clear_progress_line,
    create_download_job,
    download_file,
    print_download_progress,
)
from .errors import DownloadError, HdHubError, ResolutionError
from .project import get_downloads_dir
from .ui import clear_screen, pause, print_error, print_header

console = Console()

APP_TITLE = "HDHUB4U"

QUIT_WORDS = frozenset({"q", "quit", "exit"})
BACK_WORDS = frozenset({"b", "back"})

#: Outcomes of one search-and-select pass.
QUIT = "quit"
BACK = "back"
AGAIN = "again"


class QuitFlow(Exception):
    """Raised to unwind straight out of the interactive app."""


def _prompt(
    message: str,
) -> str | None:
    """
    Read one line, returning None when input ends.

    Matches the rest of the application so an interrupt unwinds the
    flow instead of surfacing a traceback.
    """

    try:
        return input(message).strip()

    except (EOFError, KeyboardInterrupt):
        return None


def _ask(
    question: str,
) -> bool:
    """Ask a yes/no question, defaulting to no."""

    answer = _prompt(question)

    return bool(answer) and answer.lower() in {"y", "yes"}


def render_results(
    items: Sequence[CatalogItem],
    query: str,
    page: int,
) -> None:
    """Print a numbered result table."""

    if not items:
        console.print(
            Panel(
                f"Nothing matched '{query}'.\n"
                "Try a shorter spelling, or the "
                "original English title.",
                border_style="yellow",
                title="No results",
            )
        )
        return

    table = Table(
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
        expand=False,
    )

    table.add_column("#", justify="right", width=4)
    table.add_column("Title", min_width=34, max_width=58)
    table.add_column("Year", justify="right", width=6)
    table.add_column("Type", width=12)
    table.add_column("Quality", width=18)

    for number, item in enumerate(items, start=1):
        table.add_row(
            str(number),
            item.short_title(56),
            item.year or "-",
            _kind_label(item),
            _quality_label(item),
        )

    console.print(table)
    console.print(
        f"[dim]page {page} · {len(items)} shown[/dim]"
    )


def _kind_label(
    item: CatalogItem,
) -> str:
    """Return a short series/movie label for a result row."""

    joined = " ".join(item.categories).lower()

    if "series" in joined or "episode" in joined:
        return "Series"

    if item.imdb_id:
        return "Movie"

    return "-"


def _quality_label(
    item: CatalogItem,
) -> str:
    """Return the resolutions a result's title advertises."""

    found: list[str] = []

    for part in item.title.replace("/", " ").split():
        token = part.strip("[]()[],:").lower()

        if (
            token in {"480p", "720p", "1080p", "2160p", "4k"}
            and token not in found
        ):
            found.append(token.upper())

    if not found:
        return "-"

    return " ".join(found[:4])


def _parse_choice(
    raw: str | None,
    count: int,
) -> str:
    """
    Turn typed input into an action.

    Returns the digit as a string, or one of ``quit``/``back``/
    ``again`` so the caller can decide what a non-numeric entry
    meant.
    """

    if raw is None:
        return "quit"

    value = raw.lower()

    if value in QUIT_WORDS:
        return "quit"

    if value in BACK_WORDS:
        return "back"

    if value.isdigit() and 1 <= int(value) <= count:
        return value

    return "again"


def fetch_options(
    url: str,
    *,
    client: httpx.Client | None = None,
) -> list[MediaOption]:
    """
    Read the options a post page offers.

    Raises:
        HdHubError: when the page cannot be fetched or read.
    """

    owned = client is None

    http_client = client or httpx.Client(
        timeout=httpx.Timeout(25.0, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": _user_agent()},
    )

    try:
        response = http_client.get(url)

        response.raise_for_status()

        return catalog.extract_options(
            response.text,
            SITE_ORIGIN,
        )

    except httpx.HTTPStatusError as error:
        raise HdHubError(
            f"The post page returned HTTP "
            f"{error.response.status_code}."
        ) from error

    except httpx.HTTPError as error:
        raise HdHubError(
            f"Could not open the post page: {error}"
        ) from error

    finally:
        if owned:
            http_client.close()


def _user_agent() -> str:
    """Return the shared browser user agent."""

    from .http_types import USER_AGENT

    return USER_AGENT


def render_options(
    item: CatalogItem,
    options: Sequence[MediaOption],
) -> None:
    """Print the option table for a chosen title."""

    console.print(
        Panel(
            item.title,
            border_style="cyan",
            title="Selected",
            subtitle=item.url,
        )
    )

    if not options:
        console.print(
            Panel(
                "This post offers no download or "
                "streaming links right now.",
                border_style="yellow",
                title="No options",
            )
        )
        return

    table = Table(
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
        expand=False,
    )

    table.add_column("#", justify="right", width=4)
    table.add_column("Quality", min_width=18, max_width=34)
    table.add_column("Size", justify="right", width=9)
    table.add_column("Kind", width=11)
    table.add_column("Host", width=20)

    for number, option in enumerate(options, start=1):
        kind = (
            "Download"
            if option.kind == DOWNLOAD_KIND
            else "Stream"
        )

        table.add_row(
            str(number),
            option.label[:34],
            option.size_label or "-",
            kind,
            option.host[:20],
        )

    console.print(table)


def choose_option(
    options: Sequence[MediaOption],
) -> MediaOption | None:
    """
    Let the user pick one option.

    Returns None to step back out of the title entirely.
    """

    if not options:
        return None

    console.print()
    console.print(" [bold]Enter option number[/bold]")
    console.print(" [bold]d[/bold]  Downloads only")
    console.print(" [bold]s[/bold]  Streams only")
    console.print(" [bold]b[/bold]  Back to results")
    console.print(" [bold]q[/bold]  Quit")

    while True:
        raw = _prompt("> ")

        if raw is None or raw.lower() in QUIT_WORDS:
            raise QuitFlow

        value = raw.lower()

        if value in BACK_WORDS:
            return None

        if value in {"d", "s"}:
            wanted = DOWNLOAD_KIND if value == "d" else "streaming"

            matches = [
                option
                for option in options
                if option.kind == wanted
            ]

            if not matches:
                print_error(
                    f"No {wanted} options on this title."
                )
                continue

            return matches[0]

        if not value.isdigit() or not 1 <= int(value) <= len(
            options
        ):
            print_error("Enter an option number from the list.")
            continue

        return options[int(value) - 1]


def start_download(
    item: CatalogItem,
    option: MediaOption,
    *,
    client: httpx.Client | None = None,
) -> bool:
    """
    Open the gate and transfer the file.

    Returns True when a file was written, False when the user
    declined or the gate could not be opened.
    """

    console.print(
        f"[dim]opening {option.host} gate for "
        f"{option.label}...[/dim]"
    )

    try:
        link = unlock.unlock(option, client=client)

    except ResolutionError as error:
        print_error(
            f"Could not open that download link.\n{error}"
        )
        return False

    except HdHubError as error:
        print_error(f"Could not open that link.\n{error}")
        return False

    console.print(
        f"[green]found[/green] {link.container or 'media'} file · "
        f"{unlock.format_size(link.size_bytes)}"
    )
    console.print(f"[dim]{link.filename}[/dim]")
    console.print(f"[dim]{link.url}[/dim]")

    if not _ask("Start download? [y/N] "):
        return False

    directory = get_downloads_dir()

    job = create_download_job(
        title=item.title,
        quality=option.resolution or "unknown",
        option={
            "title": option.label,
            "url": link.url,
        },
        root=directory,
        extension=_suffix(link.filename, link.container),
        filename=link.filename,
    )

    if job.output_path.exists():
        print_error(
            f"Already downloaded: {job.output_path.name}"
        )
        return False

    console.print()

    try:
        path = download_file(
            job,
            progress_callback=print_download_progress,
        )

    except DownloadError as error:
        clear_progress_line()
        print()
        print_error(f"Download failed.\n{error}")
        return False

    clear_progress_line()

    print()
    console.print(
        f"[bold green]Saved[/bold green] {path}"
    )
    console.print(
        f"[dim]{unlock.format_size(path.stat().st_size)}[/dim]"
    )

    return True


def _suffix(
    filename: str,
    container: str,
) -> str:
    """Return the extension to enforce on the saved file."""

    from pathlib import Path

    suffix = Path(filename).suffix

    if suffix:
        return suffix

    return unlock.extension_for(container)


def show_title(
    item: CatalogItem,
    options: Sequence[MediaOption],
    *,
    client: httpx.Client | None = None,
) -> str:
    """
    Drive the menu for one chosen title.

    Returns:
        ``quit``, ``back`` to the results, or ``again`` to stay.
    """

    render_options(item, options)

    if not options:
        pause("\nPress Enter to go back...")
        return "back"

    option = choose_option(options)

    if option is None:
        return "back"

    if option.kind != DOWNLOAD_KIND:
        console.print(
            Panel(
                "That option is a stream, not a file.\n"
                f"Open it in a browser:\n{option.url}",
                border_style="yellow",
                title="Stream only",
            )
        )
        pause("\nPress Enter to go back...")
        return "again"

    start_download(item, option, client=client)

    pause("\nPress Enter to go back...")

    return "again"


def browse_results(
    query: str,
    *,
    limit: int = catalog.DEFAULT_LIMIT,
    client: httpx.Client,
) -> str:
    """
    List results for a query and act on the chosen one.

    Returns:
        :data:`QUIT` to leave the application, :data:`BACK` to
        return to the search prompt.

    Raises:
        QuitFlow: when the user asks to quit from deep in the menu.
    """

    page = 1

    while True:
        try:
            items = search_catalog(
                query,
                limit=limit,
                page=page,
                client=client,
            )

        except HdHubError as error:
            print_error(f"Search failed.\n{error}")
            return BACK

        clear_screen()

        print_header(APP_TITLE, f'Search: "{query}"')

        render_results(items, query, page)

        console.print()
        console.print(" [bold]Enter result number[/bold]")
        console.print(" [bold]n[/bold]  Next page")
        console.print(" [bold]b[/bold]  New search")
        console.print(" [bold]q[/bold]  Quit")

        raw = _prompt("> ")

        if raw is None or raw.lower() in QUIT_WORDS:
            return QUIT

        value = raw.strip().lower()

        if value in BACK_WORDS:
            return BACK

        if value == "n":
            page += 1
            continue

        action = _parse_choice(raw, len(items))

        if action == AGAIN:
            print_error("Enter a result number.")
            pause()
            continue

        _handle_selection(
            items[int(action) - 1],
            client,
        )

        page = 1


def run_search(
    query: str,
    *,
    limit: int = catalog.DEFAULT_LIMIT,
    interactive: bool = True,
    client: httpx.Client | None = None,
) -> int:
    """
    Search the live catalog and, interactively, download something.

    Args:
        query: What to search for.
        limit: Results per page.
        interactive: When False, list the results and stop.
        client: Optional caller-owned HTTP client.

    Returns:
        A process exit code.
    """

    owned = client is None

    http_client = client or httpx.Client(
        timeout=httpx.Timeout(25.0, connect=10.0),
        follow_redirects=True,
        headers={
            "User-Agent": _user_agent(),
            "Origin": SITE_ORIGIN,
            "Referer": f"{catalog.SEARCH_PAGE_URL}?q=",
        },
    )

    try:
        if not interactive:
            try:
                items = search_catalog(
                    query,
                    limit=limit,
                    client=http_client,
                )

            except HdHubError as error:
                print_error(f"Search failed.\n{error}")
                return 1

            render_results(items, query, 1)

            return 0 if items else 1

        outcome = browse_results(
            query,
            limit=limit,
            client=http_client,
        )

        return 0 if outcome in {QUIT, BACK} else 0

    except QuitFlow:
        return 0

    except KeyboardInterrupt:
        print()
        return 130

    finally:
        if owned:
            http_client.close()


def _handle_selection(
    item: CatalogItem,
    client: httpx.Client,
) -> None:
    """Show one title's options and run its menu until the user backs out."""

    while True:
        try:
            options = fetch_options(item.url, client=client)

        except HdHubError as error:
            print_error(str(error))
            pause("\nPress Enter to go back...")
            return

        outcome = show_title(
            item,
            options,
            client=client,
        )

        if outcome != AGAIN:
            return


def interactive(
    initial_query: str = "",
) -> int:
    """
    Run the search prompt until the user quits.

    Returns:
        A process exit code.
    """

    query = initial_query

    try:
        with httpx.Client(
            timeout=httpx.Timeout(25.0, connect=10.0),
            follow_redirects=True,
            headers={
                "User-Agent": _user_agent(),
                "Origin": SITE_ORIGIN,
                "Referer": f"{catalog.SEARCH_PAGE_URL}?q=",
            },
        ) as client:
            while True:
                if not query:
                    clear_screen()

                    print_header(
                        APP_TITLE,
                        "Search the catalog",
                    )

                    entered = _prompt(
                        "\nMovie or series name: "
                    )

                    if (
                        entered is None
                        or entered.lower() in QUIT_WORDS
                    ):
                        return 0

                    if not entered:
                        continue

                    query = entered

                if browse_results(query, client=client) == QUIT:
                    return 0

                query = ""

    except QuitFlow:
        return 0

    except KeyboardInterrupt:
        print()
        return 130

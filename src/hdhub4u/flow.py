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
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import catalog, keys, unlock
from .catalog import (
    DOWNLOAD_KIND,
    SITE_ORIGIN,
    CatalogItem,
    MediaOption,
    search_page,
)
from .downloader import (
    TransferProgress,
    clear_progress_line,
    create_download_job,
    download_file,
)
from .errors import DownloadError, HdHubError, ResolutionError
from .link_cache import LinkCache
from .project import (
    get_downloads_dir,
    get_link_cache_path,
)
from .ui import (
    ResultRow,
    clear_screen,
    console,
    pause,
    print_error,
    print_header,
    print_results,
    print_table_results,
    waiting,
)

APP_TITLE = "HDHUB4U"

#: Extensions a saved file is allowed to carry, taken from the
#: containers the probe recognises plus the few names a server may
#: legitimately use for the same bytes. Anything else in a URL is a
#: page or an executable, not a video.
MEDIA_EXTENSIONS = frozenset(
    {
        ".mkv",
        ".mp4",
        ".m4v",
        ".mov",
        ".flv",
        ".ogv",
        ".ogg",
        ".webm",
        ".avi",
        ".mpg",
        ".mpeg",
        ".ts",
        ".m2ts",
        ".mp3",
        ".m4a",
        ".aac",
        ".flac",
        ".wav",
        ".srt",
        ".sub",
    }
)

#: The Escape character. Also delivered as a key name by the
#: arrow-key picker; a typed one reaches _prompt as this byte.
ESCAPE = "\x1b"

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


def _prompt_result(
    message: str,
) -> str | None:
    """
    Read one answer at the result prompt, Escape included.

    ``input()`` reads in canonical mode, where the terminal holds on
    to a lone Escape and never delivers it. Verified on a pty: one
    Escape was echoed and the read went on blocking, so the Escape
    documented on the results screen did nothing unless Enter followed
    it. Reading a key at a time is what makes the documented key the
    real key.

    Digits are accumulated until Enter rather than acted on at once. A
    read per keystroke fires on the ``1`` of a ``10`` and selects the
    wrong title, and pages run to twenty.

    Echo is off in raw mode, so each key is echoed here as it is
    accepted. Backspace erases in place for the same reason a terminal
    would.

    Falls back to :func:`_prompt` wherever raw input is unavailable,
    so a pipe keeps working exactly as before.

    Args:
        message: The prompt to draw.

    Returns:
        The answer as typed, the name of the key pressed for a key
        that is not text, or None when input ends.
    """

    if not keys.can_read_keys():
        return _prompt(message)

    answer = ""

    console.print(
        Text(message),
        end="",
    )

    while True:
        try:
            key = keys.read_key()

        except QuitFlow:
            return None

        if key is None:
            return None

        if key == "enter":
            console.print()

            return answer

        if key == "esc":
            console.print()

            return "esc"

        if key in QUIT_WORDS or key in BACK_WORDS:
            console.print()

            return key

        if key == "backspace":
            if answer:
                answer = answer[:-1]

                console.print(
                    Text("\b \b"),
                    end="",
                )

            continue

        if len(key) == 1 and key.isprintable() and not key.isspace():
            answer += key

            console.print(
                Text(
                    key,
                    style="bold",
                ),
                end="",
            )


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
    total: int = 0,
) -> None:
    """
    Print a numbered result table.

    A thin adapter over the shared table renderer, so the interactive
    list, a piped listing and ``--json`` are the same rows.

    Args:
        items: The results to draw.
        query: The query they came from.
        page: 1-based result page.
        total: Matches the service reported, or zero if it said none.
    """

    print_table_results(
        as_rows(items),
        query=query,
        page=page,
        total=total,
    )


def as_rows(
    items: Sequence[CatalogItem],
) -> list[ResultRow]:
    """
    Convert catalog results into the shared printing shape.

    Both output paths -- the terminal table and ``--json`` -- read
    these, so a script sees every field the table decides not to show.
    """

    return [
        ResultRow(
            title=item.title,
            url=item.url,
            kind=_item_kind(item),
            quality=_quality_label(item),
            year=item.year,
        )
        for item in items
    ]


def _item_kind(
    item: CatalogItem,
) -> str:
    """
    Return the machine kind of a result: series, movie, or empty.

    Lowercase and stable, because this is what reaches ``--json``.
    The table renders a display name for it, so a script does not have
    to match on capitalisation chosen for a human.
    """

    joined = " ".join(item.categories).lower()

    if "series" in joined or "episode" in joined:
        return "series"

    if item.imdb_id:
        return "movie"

    return ""


def _quality_label(
    item: CatalogItem,
) -> str:
    """Return the resolutions a result's title advertises."""

    return catalog.title_quality(item.title) or "-"


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

    # isdecimal, not isdigit: "²".isdigit() is True and "²" as an int
    # raises ValueError, which would end the session on a keystroke
    # rather than ask again.
    if value.isdecimal() and 1 <= int(value) <= count:
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


def option_table(
    options: Sequence[MediaOption],
    *,
    cursor: int | None = None,
) -> Table:
    """
    Build the option table for a chosen title.

    Four columns: number, quality, size, kind. The host went because
    it is the one column nobody chooses on -- a user picks a
    resolution and a size, and every host here is an interchangeable
    CDN -- and five columns plus borders did not fit a narrow terminal.

    The title is the widest cell and is left unbounded so it wraps.
    Its earlier ``[:34]`` slice was a truncation that no width asked
    for: it cut at 80 columns and at 140 alike.

    Args:
        options: The options to show.
        cursor: Zero-based row to highlight, or None for a plain
            listing with no selection in it.

    Returns:
        A table, unprinted, so a caller can redraw it in place.
    """

    table = Table(
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
        expand=True,
    )

    table.add_column(
        "#",
        justify="right",
        width=4,
    )
    table.add_column("Quality")
    table.add_column(
        "Size",
        justify="right",
        width=9,
    )
    table.add_column(
        "Kind",
        width=11,
    )

    for number, option in enumerate(options):
        kind = (
            "Download"
            if option.kind == DOWNLOAD_KIND
            else "Stream"
        )

        # label and size_label are scraped from a post page, so the
        # cells are Text rather than markup.
        table.add_row(
            str(number + 1),
            Text(option.label),
            Text(option.size_label or "-"),
            kind,
            style="bold reverse" if number == cursor else None,
        )

    return table


def render_options(
    item: CatalogItem,
    options: Sequence[MediaOption],
) -> None:
    """Print the option table for a chosen title."""

    # The title and the address are site-controlled, so both are
    # Text. Read as markup, a title of "[/]" is swallowed and one of
    # "[red]" restyles the panel.
    heading = Text()
    heading.append(item.title)
    heading.append("\n")
    heading.append(item.url, style="dim")

    console.print(
        Panel(
            heading,
            border_style="cyan",
            title="Selected",
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

    console.print(option_table(options))
    console.print(_OPTION_HELP)


#: The one line of keys under the option table. Kept as a constant so
#: the drawing and the key handling cannot disagree about what is
#: offered -- which is the bug that made "d" work and nothing say so.
_OPTION_HELP = (
    "[bold]↑↓[/bold] move  "
    "[bold]Enter[/bold] choose  "
    "[bold]d[/bold] downloads  "
    "[bold]s[/bold] streams  "
    "[bold]b[/bold] back  "
    "[bold]q[/bold] quit"
)


def choose_option(
    options: Sequence[MediaOption],
) -> MediaOption | None:
    """
    Let the user pick one option.

    Arrow keys on a terminal, a typed number anywhere else. Both reach
    the same answer, and both are always live: a terminal can still be
    driven by typing ``3``, so a pipeline through the picker is never
    the only way through.

    Returns:
        The chosen option, or None to step back out of the title.
    """

    if not options:
        return None

    if keys.can_read_keys():
        return _choose_by_key(options)

    return _choose_by_number(options)


def _choose_by_key(
    options: Sequence[MediaOption],
) -> MediaOption | None:
    """
    Drive the option table with the arrow keys.

    Redraws in place rather than scrolling, because a list of options
    is something a person compares across: the sizes and the
    resolutions have to stay on screen together while the cursor moves
    over them.

    Args:
        options: The options offered.

    Returns:
        The chosen option, or None to step back.

    Raises:
        QuitFlow: when the user asks to quit.
    """

    visible = list(options)
    cursor = 0

    with Live(
        console=console,
        auto_refresh=False,
        transient=True,
    ) as live:
        while True:
            live.update(
                Group(
                    option_table(visible, cursor=cursor),
                    _OPTION_HELP,
                )
            )

            key = keys.read_key()

            if key is None:
                raise QuitFlow

            if key in QUIT_WORDS:
                raise QuitFlow

            if key in BACK_WORDS or key == "esc":
                return None

            if key in {"up", "k"}:
                cursor = (cursor - 1) % len(visible)
                continue

            if key in {"down", "j"}:
                cursor = (cursor + 1) % len(visible)
                continue

            if key == "home":
                cursor = 0
                continue

            if key == "end":
                cursor = len(visible) - 1
                continue

            if key == "enter":
                return visible[cursor]

            if key == "d":
                visible = _only(options, DOWNLOAD_KIND)
                cursor = 0
                continue

            if key == "s":
                visible = _only(options, "streaming")
                cursor = 0
                continue

            if key == "a":
                visible = list(options)
                cursor = 0
                continue

            # A typed number jumps straight to a row, so the number
            # somebody can see on screen is one they can type.
            if key.isdecimal() and 1 <= int(key) <= len(visible):
                return visible[int(key) - 1]

            if key == "ctrl-c":
                raise QuitFlow


def _only(
    options: Sequence[MediaOption],
    kind: str,
) -> list[MediaOption]:
    """
    Return the options of one kind, or all of them.

    Falls back to the full list when a filter would leave nothing, so
    pressing "s" on a downloads-only post cannot produce an empty
    table with no way back except quit.
    """

    matches = [
        option
        for option in options
        if option.kind == kind
    ]

    return matches or list(options)


def _choose_by_number(
    options: Sequence[MediaOption],
) -> MediaOption | None:
    """
    Choose an option by typing its number.

    The path taken when the input is a pipe or a file, where a
    terminal cannot be put into raw mode and an arrow key would never
    arrive as one character.

    Args:
        options: The options offered.

    Returns:
        The chosen option, or None to step back.

    Raises:
        QuitFlow: when the user asks to quit.
    """

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

        if not value.isdecimal() or not 1 <= int(value) <= len(
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

    # option.label is text scraped off a page, so it is not
    # interpolated into a markup string.
    console.print(
        Text.assemble(
            (f"opening {option.host} gate for ", "dim"),
            (option.label, "dim"),
        )
    )

    try:
        # Opening a gate is a walk across four or five hosts and can
        # take ten seconds, so it gets a line that moves rather than
        # ten seconds of nothing.
        with waiting(f"resolving {option.host}"):
            link = unlock.unlock(
                option,
                client=client,
                cache=LinkCache(get_link_cache_path()),
            )

    except ResolutionError as error:
        print_error(
            f"Could not open that download link.\n{error}"
        )
        return False

    except HdHubError as error:
        print_error(f"Could not open that link.\n{error}")
        return False

    # The filename and the address both come from a third-party
    # page, so they are printed as Text. Interpolated into a markup
    # string, a title containing "[/]" or "[red]" was read as a style
    # and swallowed, and a filename could repaint the line.
    found = Text()
    found.append("found ", style="green")
    found.append(f"{link.container or 'media'} file")
    found.append(" · ")
    found.append(unlock.format_size(link.size_bytes))
    console.print(found)
    console.print(Text(link.filename, style="dim"))
    console.print(Text(link.url, style="dim"))

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

    progress = TransferProgress(None)

    try:
        path = download_file(
            job,
            progress_callback=progress.update,
        )

    except DownloadError as error:
        clear_progress_line()
        print()
        print_error(f"Download failed.\n{error}")
        return False

    clear_progress_line()

    print()
    console.print(
        Text.assemble(
            ("Saved ", "bold green"),
            (str(path), ""),
        )
    )
    console.print(
        f"[dim]{unlock.format_size(path.stat().st_size)}[/dim]"
    )

    return True


def _suffix(
    filename: str,
    container: str,
) -> str:
    """
    Return the extension to enforce on the saved file.

    Only a known media extension from the URL is believed. A filename
    is server-controlled, and taking whatever its last dot-separated
    run happens to be saved the file as ``.php``, ``.html`` or
    ``.exe`` when a gate named its page that way, which is both a lie
    about the content and a good way to be talked into running it.
    Anything unrecognised falls back to the extension belonging to
    the container the bytes were measured as.
    """

    from pathlib import Path

    suffix = Path(filename).suffix.lower()

    if suffix in MEDIA_EXTENSIONS:
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
        stream_notice = Text(
            "That option is a stream, not a file.\n"
            "Open it in a browser:\n",
        )
        stream_notice.append(option.url)

        console.print(
            Panel(
                stream_notice,
                border_style="yellow",
                title="Stream only",
            )
        )
        pause("\nPress Enter to go back...")
        return "again"

    downloaded = start_download(
        item,
        option,
        client=client,
    )

    # A finished download goes straight back to the results. It used to
    # stop here and ask for Enter, then drop the user onto the option
    # table again, so getting back to the list took two keys and a
    # re-read of the post page -- for somebody picking up another title
    # out of the same twenty results.
    #
    # A failed one stays. Losing the menu on a dead link means picking
    # the quality, the resolution and the gate again to try the next
    # one, which is the moment it most costs to be sent away.
    if downloaded:
        return BACK

    pause("\nPress Enter to go back...")

    return AGAIN


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
            with waiting(f'searching for "{query}"'):
                found = search_page(
                    query,
                    limit=limit,
                    page=page,
                    client=client,
                )

        except HdHubError as error:
            print_error(f"Search failed.\n{error}")
            return BACK

        items = found.items

        clear_screen()

        print_header(APP_TITLE, f'Search: "{query}"')

        render_results(
            items,
            query,
            page,
            found.total,
        )

        console.print()
        console.print(" [bold]Enter result number[/bold]")
        console.print(" [bold]n[/bold]  Next page")
        console.print(" [bold]b[/bold]  New search")
        console.print(" [bold]Esc[/bold]  New search")
        console.print(" [bold]q[/bold]  Quit")

        raw = _prompt_result("> ")

        if raw is None or raw.lower() in QUIT_WORDS:
            return QUIT

        value = raw.strip().lower()

        # Escape goes back as well as "b", because on a keyboard
        # Escape is what the muscle memory reaches for and a key that
        # is listed but does nothing is worse than one that is absent.
        if value in BACK_WORDS or value == "esc" or ESCAPE in value:
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

        # The page is deliberately kept. Coming back from a download
        # should land on the list it was started from, not on page one
        # of a search that had already been scrolled to twenty.


def run_search(
    query: str,
    *,
    limit: int = catalog.DEFAULT_LIMIT,
    interactive: bool = True,
    as_json: bool = False,
    client: httpx.Client | None = None,
) -> int:
    """
    Search the live catalog and, interactively, download something.

    Args:
        query: What to search for.
        limit: Results per page.
        interactive: When False, list the results and stop.
        as_json: Print one JSON object instead of a table. Implies
            non-interactive output regardless of the terminal.
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
                found = search_page(
                    query,
                    limit=limit,
                    client=http_client,
                )

            except HdHubError as error:
                print_error(f"Search failed.\n{error}")
                return 1

            print_results(
                as_rows(found.items),
                query=query,
                as_json=as_json,
                total=found.total,
            )

            return 0 if found.items else 1

        outcome = browse_results(
            query,
            limit=limit,
            client=http_client,
        )

        # Quitting is the user finishing, which is a success. Any
        # other outcome means the loop returned early -- a search
        # error, say -- and reporting 0 for that is how a script
        # driving this is told a download is there when none is.
        return 0 if outcome == QUIT else 1

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
            with waiting("reading options"):
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

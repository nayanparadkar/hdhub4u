"""Terminal rendering, clipboard and browser helpers."""

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()


@dataclass(frozen=True)
class ResultRow:
    """
    One search result, in the one shape every printer agrees on.

    The live catalog and the offline index disagree about almost
    everything -- one yields dataclasses, the other dictionaries, and
    neither names quality the same way. Both are converted to this
    before printing, so a table, a pipe and ``--json`` cannot drift
    apart.

    Fields are all optional except ``title`` and ``url`` because the
    two sources carry different amounts of detail. ``kind`` and
    ``quality`` are what the interactive table does not show; they
    reach a script through ``--json`` rather than being thrown away.
    """

    title: str
    url: str
    kind: str = ""
    quality: str = ""
    year: str = ""

    def as_dict(self) -> dict[str, str]:
        """Return the row as a JSON-ready mapping."""

        return {
            "title": self.title,
            "url": self.url,
            "type": self.kind,
            "quality": self.quality,
            "year": self.year,
        }


def wants_table() -> bool:
    """
    Return True when results should be drawn as a table.

    A terminal gets the table; a pipe gets plain records, because
    box-drawing characters in a redirect are noise a script has to
    strip. Rich already resolves ``FORCE_COLOR`` and ``NO_COLOR`` into
    ``is_terminal`` and ``no_color``, so the usual environment
    variables work with no extra code here -- and ``FORCE_COLOR``
    correctly opts a redirected stream back into the table.
    """

    return console.is_terminal


def print_plain_results(
    rows: Sequence[ResultRow],
    *,
    query: str,
) -> None:
    """
    Print results as plain lines for a pipe or a file.

    Two lines per result: an indexed title, then the address on its
    own line so it can be read with ``grep '^ '`` or ``cut`` without
    the title interfering.
    """

    if not rows:
        _report_no_matches(query)

        return

    for number, row in enumerate(rows, start=1):
        label = row.kind or "item"

        print(f"{number:3}. [{label}] {row.title}")
        print(f"     {row.url}")


def print_json_results(
    rows: Sequence[ResultRow],
    *,
    query: str,
    total: int = 0,
) -> None:
    """
    Print results as a single JSON object.

    A documented shape, always wrapped in an object with ``query``,
    ``count``, ``total`` and ``results``, so an empty result set is
    still valid JSON. Emitting nothing at all when there are no
    matches is what a consumer cannot read.

    ``count`` and ``total`` are different numbers and both are
    reported. ``total`` is null when the search service sent no
    count, which is not the same as a count of zero -- and is the only
    honest way to say "this search was not counted".

    Args:
        rows: The results to print.
        query: The query they came from.
        total: Matches the service reported, or zero if it said none.
    """

    print(
        json.dumps(
            {
                "query": query,
                "count": len(rows),
                "total": total or None,
                "results": [
                    row.as_dict() for row in rows
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def page_footer(
    rows: Sequence[ResultRow],
    *,
    page: int = 1,
    total: int = 0,
) -> str:
    """
    Return the line printed under a result table.

    The page and the size of the search are different numbers, and
    the total is reported only when the search service actually sent
    one. Deriving it from the page size would mean telling somebody a
    search found fourteen things because one page held fourteen rows,
    which is how a count stops meaning a count.

    The offline index reports no total, because a fuzzy match over
    SQLite rows is scored in Python and there is no query to count.

    Args:
        rows: The rows on this page.
        page: 1-based page number.
        total: Matches the service reported, or zero if it said none.

    Returns:
        A one-line summary, already styled for a terminal.
    """

    line = f"page {page} · {len(rows)} shown"

    if total > 0:
        noun = "match" if total == 1 else "matches"

        line = f"{line} · {total} {noun}"

    return f"[dim]{line}[/dim]"


def print_table_results(
    rows: Sequence[ResultRow],
    *,
    query: str = "",
    page: int = 1,
    total: int = 0,
) -> None:
    """
    Draw results as a table for a terminal.

    Three columns and no fixed title width. The table is asked to
    expand and the title is left unbounded, so it wraps and absorbs
    whatever room there is.

    The columns that were here before summed to about 106 characters
    once the borders were counted. On an 80-column terminal that did
    not clip, which is the easy thing to assume: the terminal did it.
    Rich kept every character and the right border simply left the
    screen, so quality text ran off the edge and the table lost its
    frame. Three columns fit with room to spare, and above about 100
    columns the title now spreads instead of leaving the table at 90.

    An empty result set gets a panel saying so rather than a table
    with a header and no rows, which reads as a rendering fault
    instead of a fact about the search.

    Args:
        rows: The results to draw.
        query: The query they came from, for the empty case.
        page: 1-based page number, for the footer.
        total: Matches the service reported, or zero if it said none.
    """

    if not rows:
        note = Text(
            f"Nothing matched '{query}'.\n"
            "Try a shorter spelling, or the "
            "original English title.",
        )

        console.print(
            Panel(
                note,
                border_style="yellow",
                title="No results",
            )
        )

        return

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
    table.add_column("Title")
    table.add_column(
        "Year",
        justify="right",
        width=6,
    )

    for number, row in enumerate(rows, start=1):
        # Titles are site-controlled, so the cell is a Text: Rich
        # would otherwise read a title of "[/]" as markup and drop it.
        table.add_row(
            str(number),
            Text(row.title),
            Text(row.year or "-"),
        )

    console.print(table)
    console.print(page_footer(rows, page=page, total=total))


def print_results(
    rows: Sequence[ResultRow],
    *,
    query: str,
    as_json: bool = False,
    page: int = 1,
    total: int = 0,
) -> None:
    """
    Print results to whichever destination asked for them.

    Three destinations, one order of preference: ``--json`` wins, then
    a terminal, then a plain pipe. Every caller goes through here
    rather than choosing a renderer, so the live search, the offline
    index and ``--json`` cannot drift apart -- and so a pipe never
    receives the box-drawing characters a table is made of.

    Args:
        rows: The results to print.
        query: The query they came from, echoed in JSON output.
        as_json: Emit one JSON object instead of a table.
        page: 1-based result page, shown in the table footer.
        total: Matches the service reported, or zero if it said none.
    """

    if as_json:
        print_json_results(rows, query=query, total=total)

    elif wants_table():
        print_table_results(
            rows,
            query=query,
            page=page,
            total=total,
        )

    else:
        print_plain_results(rows, query=query)


def _report_no_matches(query: str) -> None:
    """
    Say on stderr that nothing matched.

    stderr, not stdout: an empty result is not data, and a consumer
    reading stdout gets nothing to parse rather than a sentence. The
    exit code is 1 either way, so the failure is still visible.
    """

    print(
        f"No matches for {query!r}.",
        file=sys.stderr,
    )


def clear_screen() -> None:
    """
    Clear the terminal and home the cursor.

    Written out as an escape sequence rather than shelling out to
    ``clear``: that put a path through the shell, forked a process to
    run a program that may not be installed, and printed ``sh: 1:
    clear: not found`` into the output on a system without it.
    """

    if console.is_terminal:
        console.file.write("\x1b[2J\x1b[H")
        console.file.flush()


def pause(
    message: str = "\nPress Enter to continue...",
) -> None:
    """Wait for the user to acknowledge a message."""

    try:
        input(message)

    except (EOFError, KeyboardInterrupt):
        console.print()


def terminal_width() -> int:
    return shutil.get_terminal_size(
        (100, 20)
    ).columns


class Spinner:
    """
    An animated "working" line, shown only on a terminal.

    Resolving a download gate is a multi-hop walk across four or five
    hosts, and it can take ten seconds with nothing on screen. A line
    that moves says the program has not stopped; silence reads as a
    hang, and a user who thinks it has hung will kill it.

    Off a terminal the frames are dropped and the message is printed
    once. A carriage return in a redirected file is not a spinner, it
    is a file full of ``|/-\\`` that some other tool has to strip.

    Args:
        message: What is being waited on.
        interval: Seconds between frames.
    """

    #: Ten frames, then repeat. Fast enough to read as motion, slow
    #: enough not to be a flicker on a terminal multiplexer.
    FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(
        self,
        message: str,
        *,
        interval: float = 0.1,
    ) -> None:
        self.message = message
        self.interval = interval
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def animates(self) -> bool:
        """Return True when frames will actually be drawn."""

        return bool(console.is_terminal)

    def start(self) -> None:
        """Begin animating, or print the message once."""

        if not self.animates:
            console.print(self.message)
            return

        self._thread = threading.Thread(
            target=self._spin,
            daemon=True,
        )
        self._thread.start()

    def stop(
        self,
        message: str = "",
    ) -> None:
        """
        Stop animating and clear the line.

        Args:
            message: Printed in place of the spinner when given.
        """

        self._stop.set()

        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

        if not self.animates:
            return

        # Erase before writing, or the last frame stays on the line.
        console.file.write("\r\033[2K")
        console.file.flush()

        if message:
            console.print(message)

    def _spin(self) -> None:
        """Draw frames until asked to stop."""

        index = 0

        while not self._stop.is_set():
            frame = self.FRAMES[index % len(self.FRAMES)]

            console.file.write(
                f"\r\033[2K{frame} {self.message}"
            )
            console.file.flush()

            index += 1

            self._stop.wait(self.interval)

    def __enter__(self) -> Spinner:
        self.start()

        return self

    def __exit__(self, *exception: object) -> None:
        self.stop()


@contextlib.contextmanager
def waiting(
    message: str,
) -> Iterator[None]:
    """
    Show a spinner for the length of a slow operation.

    Args:
        message: What is being waited on.

    Yields:
        Nothing; the point is the line on screen while it lasts.
    """

    spinner = Spinner(message)

    spinner.start()

    try:
        yield

    finally:
        spinner.stop()


def print_header(
    title: str = "HDHUB4U DOWNLOADER",
    subtitle: str = "",
) -> None:
    content = Text()

    content.append(
        title,
        style="bold cyan",
    )

    if subtitle:
        content.append("\n")
        content.append(
            subtitle,
            style="dim",
        )

    console.print(
        Panel(
            content,
            border_style="cyan",
            expand=False,
            width=min(
                terminal_width(),
                80,
            ),
        )
    )


def print_selected(
    item: Mapping[str, object],
    number: int,
) -> None:
    title = str(
        item["title"]
    )

    media_type = str(
        item["type"]
    )

    url = str(
        item["url"]
    )

    content = Text()

    content.append(
        "Title\n",
        style="bold cyan",
    )

    content.append(
        f"{title}\n\n"
    )

    content.append(
        "Type\n",
        style="bold cyan",
    )

    content.append(
        f"{media_type.title()}\n\n"
    )

    content.append(
        "Result\n",
        style="bold cyan",
    )

    content.append(
        f"#{number}\n\n"
    )

    content.append(
        "URL\n",
        style="bold cyan",
    )

    content.append(
        url,
        style="dim",
    )

    console.print(
        Panel(
            content,
            title="Selected Result",
            border_style="green",
            expand=False,
        )
    )


def print_inspection(
    result: dict[str, object],
) -> None:
    table = Table(
        show_header=False,
        border_style="dim",
        expand=False,
    )

    table.add_column(
        "Property",
        style="bold cyan",
        width=18,
    )

    table.add_column(
        "Value",
        max_width=65,
    )

    table.add_row(
        "Requested URL",
        str(
            result.get(
                "url",
                "",
            )
        ),
    )

    table.add_row(
        "Final URL",
        str(
            result.get(
                "final_url",
                "",
            )
        ),
    )

    table.add_row(
        "HTTP Status",
        str(
            result.get(
                "status",
                "",
            )
        ),
    )

    table.add_row(
        "Content Type",
        str(
            result.get(
                "content_type",
                "",
            )
        ),
    )

    table.add_row(
        "Detected Type",
        str(
            result.get(
                "type",
                "",
            )
        ),
    )

    table.add_row(
        "Redirects",
        str(
            result.get(
                "redirects",
                0,
            )
        ),
    )

    title = str(
        result.get(
            "title",
            "",
        )
    )

    if title:
        table.add_row(
            "Page Title",
            title,
        )

    error = str(
        result.get(
            "error",
            "",
        )
    )

    if error:
        table.add_row(
            "Error",
            error,
        )

    console.print(
        Panel(
            table,
            title="Inspection",
            border_style="yellow",
        )
    )


def print_error(
    message: str,
) -> None:
    """
    Show an error to the user.

    The message is a Rich ``Text``, not a markup string, because it
    carries text this program did not write: server names, page
    titles, and exception messages naming URLs. A title containing
    ``[red]`` or a tag such as ``[/]`` used to be read as markup and
    swallowed, or printed as a style, which is both a lie about what
    the site said and a way for the text to rearrange the panel.
    """

    console.print(
        Panel(
            Text(message),
            title="Error",
            border_style="red",
        )
    )


CLIPBOARD_COMMANDS = (
    ["wl-copy"],
    ["xclip", "-selection", "clipboard"],
    ["xsel", "--clipboard", "--input"],
)


def copy_to_clipboard(
    text: str,
) -> bool:
    """
    Copy text using whichever clipboard utility is available.

    Returns False when no known utility exists or all of them fail.
    """

    for command in CLIPBOARD_COMMANDS:
        if shutil.which(command[0]) is None:
            continue

        try:
            subprocess.run(
                command,
                input=text,
                text=True,
                check=True,
            )

        except (subprocess.SubprocessError, OSError):
            continue

        return True

    return False


def open_url(
    url: str,
) -> bool:
    """Open a URL in the default browser."""

    return webbrowser.open(url)


def recent_downloads(
    limit: int = 5,
) -> list[tuple[str, int, float]]:
    """
    Return the most recently changed downloads, newest first.

    ``status`` is where a user goes to find out where their files are.
    Before this, it answered that question for the index and the cache
    and then stopped, which left the one directory they actually care
    about -- ``downloads/`` -- reported only as a path they then had to
    go and open by hand.

    Only media is listed: a ``.part`` file is a transfer still in
    flight, and a directory timestamp says when something was written
    into it rather than when the title was fetched, so sorting on it
    would put the most recent *activity* first rather than the most
    recent download.

    Args:
        limit: How many to return at most.

    Returns:
        ``(filename, size_bytes, modified_time)`` per file, newest
        first. Empty when nothing has been downloaded, or when the
        directory does not exist yet.
    """

    from .project import get_downloads_dir

    root = get_downloads_dir()

    if not root.is_dir():
        return []

    found: list[tuple[str, int, float]] = []

    try:
        for entry in root.rglob("*"):
            if not entry.is_file():
                continue

            if entry.suffix in {".part", ".lock"}:
                continue

            try:
                info = entry.stat()

            except OSError:
                # A file removed between listing and stat is not a
                # failure worth reporting.
                continue

            found.append(
                (str(entry.relative_to(root)), info.st_size, info.st_mtime)
            )

    except OSError:
        return []

    found.sort(key=lambda item: item[2], reverse=True)

    return found[:limit]


def _describe_recent(
    downloads: Sequence[tuple[str, int, float]],
) -> None:
    """Print the recent downloads, if there are any."""

    if not downloads:
        print("Recent downloads : none yet")
        return

    print(f"Recent downloads  ({len(downloads)} most recent)")

    for name, size, modified in downloads:
        stamp = datetime.fromtimestamp(modified)

        print(
            f"  {stamp:%Y-%m-%d %H:%M}  "
            f"{_short_size(size):>9}  {name}"
        )


def _short_size(size: int) -> str:
    """Format a size in the fewest characters that stay honest."""

    value = float(size)

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"

            return f"{value:.1f} {unit}"

        value /= 1024

    return f"{value:.1f} TB"


def show_status() -> None:
    """
    Report where the program keeps its data and what it can reach.

    Browser availability is part of this because a crawl without a
    browser silently produces placeholder titles rather than real
    ones, and the only symptom is a worse index. Saying so here is
    cheaper than debugging that later.
    """

    from .browser import browser_status
    from .database import get_media_count, placeholder_title_count
    from .indexer import USE_BROWSER_ENV
    from .link_cache import LinkCache
    from .project import (
        get_database_path,
        get_downloads_dir,
        get_link_cache_path,
    )

    cache_path = get_link_cache_path()

    print()
    print(f"Index            : {get_database_path()}")
    print(
        "Link cache       : "
        f"{cache_path} "
        f"({'present' if cache_path.exists() else 'empty'})"
    )
    print(f"Downloads        : {get_downloads_dir()}")
    print(
        "Indexed items      : "
        f"{get_media_count()}"
    )
    print(
        "Placeholder titles : "
        f"{placeholder_title_count()}"
    )
    print(
        "Cached links     : "
        f"{LinkCache(cache_path).count()}"
    )
    print(f"Browser          : {browser_status()}")
    rendering_on = (
        os.environ.get(USE_BROWSER_ENV, "")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )

    print(
        "Page rendering   : "
        f"{'on' if rendering_on else 'off'} "
        f"(set {USE_BROWSER_ENV}=1)"
    )

    print()
    _describe_recent(recent_downloads())
    print()

"""Terminal rendering, clipboard and browser helpers."""

import os
import shutil
import subprocess
import webbrowser
from collections.abc import Mapping

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()


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


def format_type(
    media_type: str,
) -> str:
    labels = {
        "movie": (
            "Movie",
            "magenta",
        ),
        "webseries": (
            "Web Series",
            "blue",
        ),
        "episode": (
            "Episode",
            "yellow",
        ),
        "watch": (
            "Watch",
            "cyan",
        ),
    }

    label, style = labels.get(
        media_type,
        (
            media_type.title(),
            "white",
        ),
    )

    return (
        f"[{style}]"
        f"{label}"
        f"[/{style}]"
    )


def shorten_title(
    title: str,
    max_length: int = 55,
) -> str:
    title = " ".join(
        title.split()
    )

    if len(title) <= max_length:
        return title

    return (
        title[: max_length - 3].rstrip()
        + "..."
    )


def print_results(
    results: list[dict[str, object]],
) -> None:
    if not results:
        console.print(
            Panel(
                "No relevant results found.",
                border_style="yellow",
                title="Search",
            )
        )
        return

    table = Table(
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
        expand=False,
    )

    table.add_column(
        "#",
        justify="right",
        width=4,
    )

    table.add_column(
        "Title",
        min_width=35,
        max_width=60,
    )

    table.add_column(
        "Type",
        width=14,
    )

    for number, item in enumerate(
        results,
        start=1,
    ):
        title = shorten_title(
            str(item["title"])
        )

        table.add_row(
            str(number),
            title,
            format_type(
                str(item["type"])
            ),
        )

    console.print(table)


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

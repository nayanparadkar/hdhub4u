"""Interactive command-line application."""

from __future__ import annotations

from .browser import browser_status
from .database import (
    get_media_count,
    initialize_database,
    newest_media,
)
from .downloader import (
    DownloadJob,
    clear_progress_line,
    create_download_job,
    download_file,
    print_download_progress,
)
from .errors import (
    DownloadError,
    HdHubError,
)
from .indexer import USE_BROWSER_ENV, _env_flag, build_index
from .inspector import inspect_url
from .link_cache import LinkCache
from .parser import (
    downloadable_options,
    extract_download_options,
)
from .probe import (
    OptionProbe,
    ProbeResult,
    group_by_downloadability,
    summarize_gating,
)
from .project import (
    get_database_path,
    get_downloads_dir,
    get_link_cache_path,
)
from .quality import (
    QualityOption,
    find_quality_options,
    get_quality,
    get_quality_options,
    select_smallest,
)
from .resolver import (
    HostAdapterRegistry,
    ResolveResult,
    build_resolver,
)
from .search import search_media
from .ui import (
    clear_screen,
    copy_to_clipboard,
    open_url,
    pause,
    print_error,
    print_header,
    print_inspection,
    print_results,
    print_selected,
)

APP_TITLE = "HDHUB4U DOWNLOADER"

BACK_KEY = "6"

#: How many items the browse screen lists at once.
BROWSE_LIMIT = 30


def prompt(
    message: str,
) -> str | None:
    """
    Read one line of input.

    Returns None when the user interrupts or closes stdin, so callers
    can unwind instead of crashing with a traceback.
    """

    try:
        return input(message).strip()

    except (EOFError, KeyboardInterrupt):
        return None


def ask_yes_no(
    question: str,
) -> bool:
    """Ask a yes/no question, defaulting to no."""

    answer = prompt(question)

    return bool(answer) and answer.lower() in {
        "y",
        "yes",
    }


def inspect_input_url(
    url: str,
) -> None:
    """Inspect a URL and display its information."""

    result = inspect_url(url)

    error = str(result.get("error", ""))

    if error:
        print_error(error)

    else:
        print_inspection(result)

    pause()


def select_quality() -> QualityOption | None:
    """Display quality options and return the selected quality."""

    while True:
        clear_screen()

        print_header(
            APP_TITLE,
            "Download quality",
        )

        print()

        for option in get_quality_options():
            print(f" {option.key}  {option.label}")

        print(f" {BACK_KEY}  Back")
        print()

        choice = prompt("> ")

        if choice is None or choice == BACK_KEY:
            return None

        quality = get_quality(choice)

        if quality is None:
            print_error("Invalid quality option.")
            pause()

            continue

        return quality


def browse_workflow() -> str:
    """
    List the index and act on a chosen item.

    Returns:
        "back" or "quit"
    """

    while True:
        clear_screen()

        print_header(
            APP_TITLE,
            "Browse the index",
        )

        print()

        results = newest_media(BROWSE_LIMIT)

        if not results:
            print_error(
                "The index is empty. Run "
                "'hdhub4u index' first."
            )

            pause()

            return "back"

        print_results(results)

        print()
        print(" Enter number to act on an item")
        print(" r  Refresh")
        print(" b  Back to menu")
        print(" q  Quit")
        print()

        choice = prompt("> ")

        if choice is None:
            return "quit"

        choice = choice.lower()

        if choice in {"q", "quit", "exit"}:
            return "quit"

        if choice in {"b", "back", ""}:
            return "back"

        if choice in {"r", "refresh"}:
            continue

        if not choice.isdigit():
            print_error("Enter a result number.")
            pause()

            continue

        number = int(choice)

        if not 1 <= number <= len(results):
            print_error("Number is out of range.")
            pause()

            continue

        action = show_selected_result(
            results[number - 1],
            number,
        )

        if action == "quit":
            return "quit"


def choose_option_automatically(
    options: list[dict[str, str]],
) -> dict[str, str] | None:
    """
    Pick the smallest option without prompting.

    Returns None when no option declares a resolution. Some pages,
    notably series with per-episode links, carry no quality marker,
    and a silent guess would defeat the point of asking for the
    smallest file.
    """

    smallest = select_smallest(options)

    if smallest is None:
        return None

    print(
        f"\nSmallest available: "
        f"{smallest['title']}"
    )

    return smallest


def _is_usable(
    option: dict[str, str],
    probes: dict[str, ProbeResult],
) -> bool:
    """
    Return True when a probed option can be downloaded.

    An option with no probe result is kept: an unprobed link is
    untested rather than known bad, and dropping it would hide
    options the resolver never reached.
    """

    result = probes.get(
        str(option.get("url", ""))
    )

    if result is None:
        return True

    return result.is_media


def select_download_option(
    options: list[dict[str, str]],
    quality: QualityOption,
    probes: dict[
        str,
        ProbeResult
    ] | None = None,
) -> dict[str, str] | None:
    """
    Select one download option matching the requested quality.

    Streaming options are excluded: they point at a player page, so
    choosing one cannot produce a file. When probe results are
    supplied, options already known to be unusable are left out of
    the list as well, so the menu only offers links that work.
    """

    options = downloadable_options(options)

    if probes:
        options = [
            option
            for option in options
            if _is_usable(
                option,
                probes,
            )
        ]

    if not options:
        print_error(
            "No downloadable option matches "
            f"{quality.label}. Every candidate "
            "on this page is gated or streaming."
        )

        pause()

        return None

    if quality.value == "minimum":
        chosen = choose_option_automatically(options)

        if chosen is not None:
            return chosen

        # No quality markers to compare, so fall back to the menu
        # rather than refusing the download outright.
        quality = QualityOption(
            key="1",
            label="Best available",
            value="best",
        )

    matches = find_quality_options(
        options,
        quality,
    )

    if not matches:
        print_error(
            f"No {quality.label} option found."
        )

        pause()

        return None

    if len(matches) == 1:
        return matches[0]

    while True:
        clear_screen()

        print_header(
            APP_TITLE,
            f"{quality.label} options",
        )

        print()

        for index, option in enumerate(
            matches,
            start=1,
        ):
            print(f" {index}  {option['title']}")

        print()
        print(" 0  Back")
        print()

        choice = prompt("> ")

        if choice is None or choice == "0":
            return None

        if not choice.isdigit():
            print_error(
                "Please enter a valid option number."
            )

            pause()

            continue

        index = int(choice)

        if not 1 <= index <= len(matches):
            print_error(
                "Option number is out of range."
            )

            pause()

            continue

        return matches[index - 1]


def load_download_options(
    url: str,
) -> tuple[list[dict[str, str]] | None, str]:
    """
    Load and parse the download options for a media page.

    Returns:
        (options, error). On failure options is None and error
        describes what went wrong.
    """

    page_info = inspect_url(url)

    error = str(page_info.get("error", ""))

    if error:
        return (None, error)

    return (
        extract_download_options(
            str(page_info["html"]),
            str(page_info["final_url"]),
        ),
        "",
    )


def print_resolution(
    resolved: ResolveResult,
) -> None:
    """Display what the server actually serves."""

    print()
    print(f"Final URL : {resolved.final_url}")
    print(f"Status    : {resolved.status_code}")
    print(
        "Type      : "
        f"{resolved.content_type or 'unknown'}"
    )
    print(f"Redirects : {resolved.redirects}")
    print(f"Method    : {resolved.strategy}")


def verify_download_link(
    url: str,
) -> tuple[ResolveResult | None, str]:
    """
    Confirm the URL resolves to media before downloading.

    Uses the on-disk cache so a repeated run does not re-pay for
    resolution. The resolved URL is only ever displayed: the
    download must use the original URL, whose signed parameters are
    part of the authorization.

    Returns:
        (result, error). On failure result is None.
    """

    cache = LinkCache(get_link_cache_path())

    cached = cache.get(url)

    if cached is not None:
        print(
            "Using cached resolution: "
            f"{cached.final_url}"
        )

        return (
            ResolveResult(
                original_url=url,
                final_url=cached.final_url,
                status_code=0,
                content_type=cached.content_type,
                is_media=True,
                redirects=0,
                strategy=f"cache ({cached.strategy})",
            ),
            "",
        )

    try:
        resolved = build_resolver().resolve(url)

    except HdHubError as error:
        return (
            None,
            f"Could not reach the link host:\n{error}",
        )

    except Exception as error:
        return (
            None,
            f"URL resolution failed:\n{error}",
        )

    print_resolution(resolved)

    if not resolved.is_media:
        return (
            resolved,
            (
                "This link is gated. The server returned "
                f"{resolved.content_type or 'no content-type'}, "
                "not a media file."
            ),
        )

    cache.put(
        url,
        resolved.final_url,
        resolved.content_type,
        resolved.strategy,
    )

    return (resolved, "")


def print_job_summary(
    job: DownloadJob,
) -> None:
    """Display the prepared download."""

    print()
    print(f"Title   : {job.title}")
    print(f"Quality : {job.quality}")
    print(f"Variant : {job.option_title}")
    print(f"URL     : {job.url}")
    print(f"Folder  : {job.output_directory}")
    print(f"File    : {job.output_filename}")
    print(f"Path    : {job.output_path}")
    print()


def print_probe_report(
    results: list[ProbeResult],
) -> None:
    """
    Display which options can actually be downloaded.

    Shown before the user picks, so a page whose every option is
    gated reports that plainly rather than failing after the
    choice is made.
    """

    usable, blocked = group_by_downloadability(
        results
    )

    print()
    print(summarize_gating(results))
    print()

    for result in usable:
        print(
            f"  ok    {result.describe()}"
        )

    for result in blocked:
        print(
            f"  skip  {result.describe()}"
        )

    print()


def download_selected(
    result: dict[str, object],
) -> None:
    """Run the full download flow for a selected search result."""

    url = str(result["url"])

    quality = select_quality()

    if quality is None:
        return

    options, error = load_download_options(url)

    if options is None:
        print_error(error)
        pause()

        return

    clear_screen()

    print_header(
        APP_TITLE,
        "Checking download links",
    )

    print()

    print(
        "Probing each option so only "
        "downloadable links are offered..."
    )

    probe = OptionProbe(
        build_resolver(),
        registry=HostAdapterRegistry(),
    )

    results = probe.probe_all(options)

    print_probe_report(results)

    if not any(
        result.is_media
        for result in results
    ):
        print_error(
            "None of the options on this page "
            "serve a media file directly.\n\n"
            "These links are hosted on services "
            "that hand the file to a browser "
            "rather than to a download client, "
            "so this tool cannot fetch them."
        )

        pause()

        return

    probes = {
        result.url: result
        for result in results
    }

    option = select_download_option(
        options,
        quality,
        probes,
    )

    if option is None:
        return

    extension = probes[
        str(option.get("url", ""))
    ].extension

    job = create_download_job(
        title=str(result["title"]),
        quality=quality.label,
        option=option,
        extension=extension,
    )

    clear_screen()

    print_header(
        APP_TITLE,
        "Download ready",
    )

    print_job_summary(job)

    print("Checking the download link...")

    _, error = verify_download_link(job.url)

    if error:
        print()
        print_error(error)
        pause()

        return

    print()

    if not ask_yes_no("Start download? [y/N]: "):
        print("\nDownload cancelled.")
        pause()

        return

    print()
    print("Starting download...")

    # The original job URL is used for the download, not the URL the
    # resolver followed. Signed query parameters are part of the
    # authorization and would be lost after a redirect.
    try:
        output_path = download_file(
            job,
            progress_callback=print_download_progress,
        )

    except DownloadError as error:
        clear_progress_line()
        print()
        print_error(f"Download failed:\n{error}")
        pause()

        return

    except OSError as error:
        clear_progress_line()
        print()
        print_error(f"Could not write file:\n{error}")
        pause()

        return

    clear_progress_line()

    print()
    print("Download complete.")
    print(f"Saved to: {output_path}")

    pause()


def copy_result_url(
    result: dict[str, object],
) -> None:
    """Copy a result URL to the clipboard."""

    url = str(result["url"])

    if copy_to_clipboard(url):
        print("\nURL copied to clipboard.")

    else:
        print_error(
            "Could not copy URL. "
            "No clipboard utility was found."
        )

    pause()


def show_selected_result(
    result: dict[str, object],
    number: int,
) -> str:
    """
    Display the selected result and handle actions.

    Returns:
        "back" or "quit"
    """

    while True:
        clear_screen()

        print_header(
            APP_TITLE,
            "Selected result",
        )

        print_selected(result, number)

        print(" 1  Open page")
        print(" 2  Inspect page")
        print(" 3  Copy URL")
        print(" 4  Download")
        print(" 5  Back")
        print(" 6  Quit")
        print()

        choice = prompt("> ")

        if choice is None:
            return "quit"

        choice = choice.lower()

        url = str(result["url"])

        if choice in {"1", "open", "open page"}:
            open_url(url)

        elif choice in {"2", "inspect", "inspect page"}:
            inspect_input_url(url)

        elif choice in {"3", "copy", "copy url"}:
            copy_result_url(result)

        elif choice in {"4", "download"}:
            download_selected(result)

        elif choice in {"5", "back", "b"}:
            return "back"

        elif choice in {"6", "quit", "q", "exit"}:
            return "quit"

        else:
            print_error("Invalid option.")

            pause()


def menu_workflow() -> str:
    """
    Run the top-level menu until the user quits.

    Returns:
        "quit"
    """

    while True:
        clear_screen()

        print_header(
            APP_TITLE,
            "Main menu",
        )

        print()
        print(" 1  Search by title")
        print(" 2  Browse the index")
        print(" 3  Rebuild the index")
        print(" 4  Status")
        print(" 5  Quit")
        print()

        choice = prompt("> ")

        if choice is None:
            return "quit"

        choice = choice.strip().lower()

        if choice in {"5", "quit", "q", "exit"}:
            return "quit"

        if choice in {"1", "search", "s"}:
            action = search_workflow()

        elif choice in {"2", "browse", "b"}:
            action = browse_workflow()

        elif choice in {"3", "index", "i"}:
            clear_screen()

            print_header(
                APP_TITLE,
                "Rebuilding index",
            )

            print()

            if not ask_yes_no(
                "Re-crawl the site now? [y/N]: "
            ):
                continue

            use_browser = _env_flag(
                USE_BROWSER_ENV
            )

            try:
                build_index(
                    use_browser=use_browser,
                )

            except HdHubError as error:
                print_error(f"Indexing failed:\n{error}")
                pause()

                continue

            print()
            print(f"Index now holds "
                  f"{get_media_count()} items.")

            pause()

            continue

        elif choice in {"4", "status", "st"}:
            show_status()
            pause()

            continue

        else:
            print_error("Invalid option.")
            pause()

            continue

        if action == "quit":
            return "quit"


def search_workflow() -> str:
    """Run the search workflow until the user quits."""

    while True:
        clear_screen()

        print_header(
            APP_TITLE,
            "Search media",
        )

        print()

        query = prompt("Search: ")

        if query is None:
            return "quit"

        if not query:
            continue

        if query.lower() in {
            "q",
            "quit",
            "exit",
        }:
            return "quit"

        try:
            results = search_media(query)

        except HdHubError as error:
            print_error(f"Search failed:\n{error}")
            pause()

            continue

        except Exception as error:
            print_error(f"Search failed:\n{error}")
            pause()

            continue

        clear_screen()

        print_header(
            APP_TITLE,
            f"Search: {query}",
        )

        print_results(results)

        if not results:
            pause(
                "\nPress Enter to search again..."
            )

            continue

        print()
        print(" Enter result number")
        print(" b  Back to search")
        print(" q  Quit")
        print()

        choice = prompt("> ")

        if choice is None:
            return "quit"

        choice = choice.lower()

        if choice in {"q", "quit", "exit"}:
            return "quit"

        if choice in {"b", "back"}:
            continue

        if not choice.isdigit():
            print_error(
                "Please enter a valid result number."
            )

            pause()

            continue

        number = int(choice)

        if not 1 <= number <= len(results):
            print_error(
                "Result number is out of range."
            )

            pause()

            continue

        action = show_selected_result(
            results[number - 1],
            number,
        )

        if action == "quit":
            return "quit"


def show_status() -> None:
    """Display index size and where data is stored."""

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
        "Cached links     : "
        f"{LinkCache(cache_path).count()}"
    )

    print(f"Browser          : {browser_status()}")
    print(
        "Page rendering   : "
        f"{'on' if _env_flag(USE_BROWSER_ENV) else 'off'} "
        "(set HDHUB_USE_BROWSER=1)"
    )

    print()


def main() -> None:
    """Application entry point."""

    initialize_database()

    while menu_workflow() != "quit":
        pass

    clear_screen()

    print_header(
        APP_TITLE,
        "Goodbye",
    )


if __name__ == "__main__":
    main()

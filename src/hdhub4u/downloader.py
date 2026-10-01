"""Streaming downloads with resume and integrity checks."""

from __future__ import annotations

import fcntl
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Callable

import httpx

from .errors import DownloadError
from .http_types import (
    DOWNLOADER_USER_AGENT,
    is_media_content_type,
    resolve_extension,
)
from .logging_setup import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = (
    Path(__file__).resolve().parent.parent.parent
)

DOWNLOADS_DIR = PROJECT_ROOT / "downloads"

CHUNK_SIZE = 1024 * 1024

#: Leave the terminal on its own line after a progress bar.
NEWLINE = "\r\033[K"


@dataclass(frozen=True)
class DownloadJob:
    """A prepared, fully resolved download."""

    title: str
    quality: str
    option_title: str
    url: str
    output_directory: Path
    output_filename: str

    @property
    def output_path(self) -> Path:
        """Return the complete output path."""

        return self.output_directory / self.output_filename

    @property
    def partial_path(self) -> Path:
        """Return the in-progress file path."""

        return (
            self.output_path.parent
            / f"{self.output_filename}.part"
        )

    def with_url(
        self,
        url: str,
    ) -> DownloadJob:
        """Return a copy of this job pointing at a different URL."""

        return replace(self, url=url)


def sanitize_filename(
    name: str,
) -> str:
    """Convert a string into a filesystem-safe filename."""

    name = name.strip()

    # Whitespace-like control characters become spaces so that
    # words are not joined together, e.g. "a\nb" -> "a b".
    name = re.sub(
        r"[\t\n\r\f\v\x00-\x1f\x7f]",
        " ",
        name,
    )

    name = re.sub(
        r'[<>:"/\\|?*]',
        "",
        name,
    )

    name = re.sub(r"\s+", " ", name)

    name = name.rstrip(". ")

    if not name:
        return "download"

    return name


def create_output_directory(
    title: str,
    *,
    root: Path = DOWNLOADS_DIR,
) -> Path:
    """Create and return the media output directory."""

    directory = root / sanitize_filename(title)

    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    return directory


def with_extension(
    filename: str,
    extension: str,
) -> str:
    """
    Append a media extension to a filename.

    A filename that already ends with that extension is left alone,
    so re-running a download never produces ``file.mp4.mp4``.
    """

    if not extension:
        return filename

    if filename.lower().endswith(
        extension.lower()
    ):
        return filename

    return f"{filename}{extension}"


def create_output_filename(
    title: str,
    option_title: str,
    extension: str = "",
) -> str:
    """
    Create a filesystem-safe output filename.

    A media extension is appended when one is known, because a
    completed file with no extension cannot be opened by a player
    or recognised by the operating system.
    """

    filename = (
        f"{sanitize_filename(title)} - "
        f"{sanitize_filename(option_title)}"
    )

    return with_extension(
        filename,
        extension,
    )


def create_download_job(
    title: str,
    quality: str,
    option: dict[str, str],
    *,
    root: Path = DOWNLOADS_DIR,
    extension: str = "",
    filename: str = "",
) -> DownloadJob:
    """
    Create a prepared download job.

    ``extension`` is the media extension the resolved link
    advertised. It is threaded through to the filename so the saved
    file is playable.

    ``filename`` overrides the composed name entirely. When a gate
    has been opened the server states the real name of the file, and
    that name already carries the release group and encoding. A name
    composed here would only be a lossy copy of it.
    """

    option_title = option["title"]

    if filename.strip():
        output_filename = with_extension(
            sanitize_filename(filename),
            extension,
        )
    else:
        output_filename = create_output_filename(
            title,
            option_title,
            extension,
        )

    return DownloadJob(
        title=title,
        quality=quality,
        option_title=option_title,
        url=option["url"],
        output_directory=create_output_directory(
            title,
            root=root,
        ),
        output_filename=output_filename,
    )


def download_file(
    job: DownloadJob,
    progress_callback: Callable[
        [int, int | None],
        None,
    ] | None = None,
) -> Path:
    """
    Download an explicitly authorized direct media URL.

    A ``.part`` file is used so an interrupted transfer can resume
    with an HTTP Range request. The file is promoted to its final name
    only after the expected byte count has arrived, so a truncated
    response is never mistaken for a complete movie.

    The original job URL is always used, never a resolved redirect
    target: signed query parameters are part of the authorization.

    Raises:
        DownloadError: on a bad content type, a truncated transfer,
            or a partial file that cannot be resumed.
    """

    job.output_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path = job.output_path
    partial_path = job.partial_path

    if output_path.exists():
        raise DownloadError(
            f"Already downloaded: {output_path.name}"
        )

    # Claim the .part file before looking at it. Two downloads of the
    # same release at once used to read the same offset, each send a
    # Range from it, and each append its own body to the same file,
    # so the result was two films interleaved and one chunk too long.
    # O_EXCL makes the claim atomic; the loser is told what happened
    # rather than corrupting the winner.
    claim = _claim_partial(partial_path)

    try:
        existing_size = (
            partial_path.stat().st_size
            if partial_path.exists()
            else 0
        )

        return _stream_to_file(
            job,
            output_path,
            partial_path,
            existing_size,
            progress_callback,
        )

    finally:
        claim.close()


def _claim_partial(
    partial_path: Path,
) -> IO[bytes]:
    """
    Take an exclusive claim on a job's ``.part`` file.

    An advisory lock on a sidecar file, not an exclusive create: a
    ``.part`` that already exists is precisely the resume case, and
    refusing to open it would make resuming impossible. What has to
    be prevented is a second *live* writer, and a lock says that
    without saying anything about files left behind by a run that
    died.

    The lock is released by the kernel when the process ends, so a
    transfer killed outright does not leave the next one blocked.

    Returns:
        The open lock file, which the caller closes when the
        transfer ends.

    Raises:
        DownloadError: when another transfer already holds it.
    """

    lock_path = partial_path.with_name(
        f"{partial_path.name}.lock"
    )

    handle = lock_path.open("a+b")

    try:
        fcntl.flock(
            handle.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )

    except OSError as error:
        handle.close()

        raise DownloadError(
            f"Another download of "
            f"{partial_path.name[: -len('.part')]} is already "
            f"running in {partial_path.parent}. Wait for it to "
            f"finish, or stop it first."
        ) from error

    return handle


def _stream_to_file(
    job: DownloadJob,
    output_path: Path,
    partial_path: Path,
    existing_size: int,
    progress_callback: Callable[
        [int, int | None],
        None,
    ] | None,
) -> Path:
    """
    Transfer the body, with :func:`download_file` holding the claim.

    Split out so that taking and releasing the claim on the ``.part``
    file is one readable pair around the transfer, rather than the
    claim and its release being interleaved with the body of the
    function that does the work.
    """

    headers = {
        "User-Agent": DOWNLOADER_USER_AGENT,
    }

    if existing_size > 0:
        headers["Range"] = f"bytes={existing_size}-"

    logger.info(
        "downloading %s -> %s (%s bytes so far)",
        job.option_title,
        output_path.name,
        existing_size,
    )

    try:
        with httpx.stream(
            "GET",
            job.url,
            follow_redirects=True,
            timeout=httpx.Timeout(
                30.0,
                connect=10.0,
            ),
            headers=headers,
        ) as response:

            response.raise_for_status()

            content_type = response.headers.get(
                "content-type",
                "",
            )

            if not is_media_content_type(content_type):
                raise DownloadError(
                    "The URL did not return a "
                    "recognized media response. "
                    f"Content-Type: "
                    f"{content_type or 'unknown'}"
                )

            resumed = (
                existing_size > 0
                and response.status_code == 206
            )

            if existing_size > 0 and not resumed:
                logger.info(
                    "server ignored Range; restarting"
                )

                existing_size = 0

            total_bytes = _total_bytes(
                response,
                existing_size if resumed else 0,
            )

            downloaded_bytes = existing_size

            if progress_callback:
                progress_callback(
                    downloaded_bytes,
                    total_bytes,
                )

            with partial_path.open(
                "ab" if resumed else "wb"
            ) as output_file:

                for chunk in response.iter_bytes(
                    chunk_size=CHUNK_SIZE
                ):
                    if not chunk:
                        continue

                    output_file.write(chunk)
                    downloaded_bytes += len(chunk)

                    if progress_callback:
                        progress_callback(
                            downloaded_bytes,
                            total_bytes,
                        )

    except DownloadError:
        raise

    except httpx.HTTPStatusError as error:
        raise DownloadError(
            f"Server returned HTTP "
            f"{error.response.status_code}"
        ) from error

    except httpx.TimeoutException as error:
        raise DownloadError(
            "Transfer timed out"
        ) from error

    except httpx.RequestError as error:
        raise DownloadError(
            f"Transfer failed: {error}"
        ) from error

    except OSError as error:
        raise DownloadError(
            f"Could not write file: {error}"
        ) from error

    _verify_complete(
        partial_path,
        total_bytes,
    )

    # The response itself is the last chance to learn the real
    # container, for hosts that only reveal it on the GET.
    output_path, partial_path = (
        _finalize_extension(
            output_path,
            partial_path,
            response.headers.get(
                "content-type",
                "",
            ),
            str(response.url),
        )
    )

    partial_path.replace(output_path)

    logger.info(
        "saved %s (%d bytes)",
        output_path.name,
        total_bytes or downloaded_bytes,
    )

    return output_path


def _finalize_extension(
    output_path: Path,
    partial_path: Path,
    content_type: str,
    final_url: str,
) -> tuple[Path, Path]:
    """
    Attach a media extension to the paths of a finished transfer.

    A job prepared without a known extension is given one here, so
    a completed transfer always lands as a playable file. An
    extension that is already present is left untouched.

    Returns:
        (output_path, partial_path). Both are returned because
        renaming the partial file moves it, and the caller needs
        the new location to promote it.
    """

    if output_path.suffix:
        return (output_path, partial_path)

    extension = resolve_extension(
        final_url,
        content_type,
    )

    if not extension:
        return (output_path, partial_path)

    stem = partial_path.name[: -len(".part")]

    renamed_partial = partial_path.parent / (
        f"{stem}{extension}.part"
    )

    renamed_output = partial_path.parent / (
        f"{stem}{extension}"
    )

    if renamed_partial != partial_path:
        partial_path.replace(renamed_partial)

    return (renamed_output, renamed_partial)


def _total_bytes(
    response: httpx.Response,
    already_downloaded: int,
) -> int | None:
    """
    Return the expected final size, or None when unknown.

    A resumed response reports only the remaining bytes, so the
    amount already on disk has to be added back.
    """

    raw = response.headers.get("content-length")

    if not raw:
        return None

    try:
        content_length = int(raw)
    except ValueError:
        return None

    if content_length <= 0:
        return None

    return already_downloaded + content_length


def _verify_complete(
    partial_path: Path,
    expected: int | None,
) -> None:
    """
    Confirm the transfer wrote every expected byte.

    A short file is kept so the next run can resume, but the error
    says plainly that the file is incomplete.
    """

    if expected is None:
        return

    actual = partial_path.stat().st_size

    if actual == expected:
        return

    if actual < expected:
        raise DownloadError(
            f"Incomplete download: got {actual} of "
            f"{expected} bytes. Re-run to resume."
        )

    raise DownloadError(
        f"Transfer returned {actual} bytes but only "
        f"{expected} were expected. "
        "The partial file was kept for inspection."
    )


def format_bytes(
    value: int,
) -> str:
    """Format bytes into a human-readable size."""

    units = ("B", "KB", "MB", "GB", "TB")

    size = float(value)

    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"

        size /= 1024

    return f"{value} B"


def format_progress(
    downloaded: int,
    total: int | None,
) -> str:
    """Create a human-readable download progress string."""

    downloaded_text = format_bytes(downloaded)

    if total is None or total <= 0:
        return f"Downloaded: {downloaded_text}"

    percentage = (downloaded / total) * 100

    return (
        f"Downloaded: "
        f"{downloaded_text} / "
        f"{format_bytes(total)} "
        f"({percentage:.1f}%)"
    )


def render_progress_bar(
    downloaded: int,
    total: int | None,
    width: int = 40,
) -> str:
    """Render a terminal progress bar."""

    if total is None or total <= 0:
        return (
            "["
            + "=" * (width - 1)
            + "> ] "
            + format_bytes(downloaded)
        )

    ratio = min(downloaded / total, 1.0)

    filled = int(ratio * width)

    return (
        "["
        + "=" * filled
        + " " * (width - filled)
        + "] "
        + f"{ratio * 100:6.2f}% "
        + f"{format_bytes(downloaded)} / "
        + f"{format_bytes(total)}"
    )


def print_download_progress(
    downloaded: int,
    total: int | None,
) -> None:
    """Print download progress on a single terminal line."""

    print(
        f"\r{render_progress_bar(downloaded, total)}",
        end="",
        flush=True,
    )


def clear_progress_line() -> None:
    """Return the cursor to a clean line after a progress bar."""

    print(NEWLINE, end="", flush=True)

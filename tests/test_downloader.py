"""Tests for hdhub4u.downloader."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from hdhub4u.downloader import (
    DownloadJob,
    TransferProgress,
    _total_bytes,
    _verify_complete,
    create_download_job,
    create_output_directory,
    create_output_filename,
    format_bytes,
    format_duration,
    format_progress,
    render_progress_bar,
    sanitize_filename,
    shorten_directory_name,
)
from hdhub4u.errors import DownloadError


class TestSanitizeFilename:
    def test_removes_unsafe_characters(self) -> None:
        assert (
            sanitize_filename('  <Bad>:Name/\\|?*  ')
            == 'BadName'
        )

    def test_empty_becomes_placeholder(self) -> None:
        assert sanitize_filename("") == "download"
        assert sanitize_filename("   ") == "download"

    def test_collapses_whitespace(self) -> None:
        assert (
            sanitize_filename("a   b   c")
            == "a b c"
        )

    def test_strips_trailing_dots_and_spaces(self) -> None:
        assert sanitize_filename("name. ") == "name"

    def test_control_characters_become_spaces(self) -> None:
        # Control characters must not join words together.
        assert sanitize_filename("a\x00b\x1fc") == "a b c"

    def test_keeps_internal_hyphens(self) -> None:
        assert (
            sanitize_filename("x264-Dual-Audio")
            == "x264-Dual-Audio"
        )


class TestCreateOutputFilename:
    def test_joins_title_and_option(self) -> None:
        assert (
            create_output_filename(
                "The Movie",
                "1080p [2GB]",
            )
            == "The Movie - 1080p [2GB]"
        )

    def test_sanitizes_both_parts(self) -> None:
        result = create_output_filename(
            "A/B",
            "C:D",
        )

        assert "/" not in result
        assert ":" not in result


class TestFormatBytes:
    @pytest.mark.parametrize(
        ("value", "expected_unit"),
        [
            (0, "B"),
            (512, "B"),
            (2048, "KB"),
            (1024**2, "MB"),
            (1024**3, "GB"),
            (1024**4, "TB"),
        ],
    )
    def test_units(
        self,
        value: int,
        expected_unit: str,
    ) -> None:
        assert format_bytes(value).endswith(
            expected_unit
        )


class TestFormatProgress:
    def test_includes_percentage(self) -> None:
        result = format_progress(500, 1000)

        assert "50.0%" in result

    def test_handles_unknown_total(self) -> None:
        for total in (None, 0, -1):
            assert "Downloaded:" in format_progress(
                500,
                total,
            )


class TestRenderProgressBar:
    """
    The bar is laid out against the terminal, so ``width`` is the
    terminal's width rather than the bar's. That is the whole point:
    a progress line that wraps onto a second row is unreadable and
    leaves debris on the terminal as it goes.
    """

    @pytest.mark.parametrize(
        "width",
        [40, 50, 60, 80, 100, 120, 200],
    )
    def test_never_overflows_the_terminal(
        self,
        width: int,
    ) -> None:
        for downloaded, total, speed, eta in (
            (700 * 1024**2, 2 * 1024**3, 3.1 * 1024**2, 95),
            (700 * 1024**2, 2 * 1024**3, None, None),
            (50, 100, 1.0, 1),
            (500, None, None, None),
        ):
            line = render_progress_bar(
                downloaded,
                total,
                speed=speed,
                eta=eta,
                width=width,
            )

            assert len(line) <= width

    def test_the_speed_is_shown_when_there_is_room(
        self,
    ) -> None:
        line = render_progress_bar(
            700 * 1024**2,
            2 * 1024**3,
            speed=3 * 1024**2,
            eta=95,
            width=120,
        )

        assert "MB/s" in line
        assert "left" in line

    def test_the_speed_goes_before_the_bar_does(
        self,
    ) -> None:
        """
        On a terminal too narrow for all of it, the rate and the
        estimate are what get dropped. Both are facts about the
        transfer; the bar is the transfer.
        """

        wide = render_progress_bar(
            700 * 1024**2,
            2 * 1024**3,
            speed=3 * 1024**2,
            eta=95,
            width=120,
        )
        narrow = render_progress_bar(
            700 * 1024**2,
            2 * 1024**3,
            speed=3 * 1024**2,
            eta=95,
            width=45,
        )

        assert "MB/s" in wide
        assert "MB/s" not in narrow
        assert narrow.startswith("[")

    def test_no_estimate_before_the_first_sample(
        self,
    ) -> None:
        """
        An estimate computed from one chunk promises twelve seconds for
        a download that takes ten minutes, which is worse than none.
        """

        assert "left" not in render_progress_bar(
            1024**2,
            2 * 1024**3,
            width=120,
        )

    def test_a_wide_terminal_does_not_stretch_the_bar(
        self,
    ) -> None:
        """
        Past a point a longer bar is just a picture. The line stops
        growing instead of running the full width of a maximised
        window.
        """

        narrow = render_progress_bar(
            700 * 1024**2,
            2 * 1024**3,
            width=100,
        )
        wide = render_progress_bar(
            700 * 1024**2,
            2 * 1024**3,
            width=200,
        )

        assert len(wide) == len(narrow)

    def test_never_exceeds_full(self) -> None:
        result = render_progress_bar(
            200,
            100,
            width=80,
        )

        assert "100.00%" in result

    def test_unknown_total_shows_bytes(self) -> None:
        result = render_progress_bar(
            10,
            None,
            width=80,
        )

        assert "B" in result


class TestShortenDirectoryName:
    def test_a_short_name_is_untouched(self) -> None:
        assert shorten_directory_name("Dune") == "Dune"

    def test_a_long_title_is_cut(self) -> None:
        name = shorten_directory_name(
            "Dune: Prophecy (Season 1) WEB-DL "
            "[Hindi (ORG 2.0) & English] 4K 1080p 720p "
            "& 480p [x264/10Bit-HEVC] | [ALL Episodes]"
        )

        assert len(name) <= 60

    def test_it_cuts_on_a_word_boundary(self) -> None:
        """
        A cut through the middle of a word identifies nothing, so the
        character after the cut has to be the space that was there.
        """

        full = (
            "Dune: Prophecy Season One WEB-DL Hindi English "
            "4K 1080p 720p 480p x264 Ten-Bit HEVC All Episodes"
        )

        name = shorten_directory_name(full)

        assert len(name) <= 60
        assert full[: len(name)] == name
        assert full[len(name)] == " "

    def test_the_identifying_part_survives(self) -> None:
        """
        The point of a shorter directory is that the beginning is kept.
        Every file manager that truncated the old 150-character names
        kept this end too.
        """

        name = shorten_directory_name(
            "Dune: Part Two (2024) WEB-DL [Hindi (ORG 5.1) "
            "+ English] 4K 1080p 720p & 480p Dual Audio"
        )

        assert name.startswith("Dune: Part Two (2024)")

    def test_an_unbroken_word_is_cut_rather_than_lost(self) -> None:
        long_word = "A" * 80

        assert shorten_directory_name(long_word) == long_word[:60]

    def test_it_never_leaves_a_trailing_space(self) -> None:
        name = shorten_directory_name(
            "A B C D E F G H I J K L M N O P Q R S T "
            "U V W X Y Z"
        )

        assert name == name.rstrip()


class TestFormatDuration:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0.4, "<1s"),
            (1, "1s"),
            (59, "59s"),
            (60, "1m"),
            (3600, "1.0h"),
            (7200, "2.0h"),
            (86400, "1d"),
        ],
    )
    def test_it_says_what_a_person_would(
        self,
        seconds: float,
        expected: str,
    ) -> None:
        assert format_duration(seconds) == expected


class TestTransferProgress:
    """
    The reporter is exercised with an injected clock, so the arithmetic
    is checked without waiting for it to happen in real time.
    """

    def _reporter(self, total, start=0.0, step=1.0):
        now = [start]

        def clock() -> float:
            value = now[0]
            now[0] += step

            return value

        return TransferProgress(total, clock=clock), now

    def test_it_measures_speed_over_a_window(
        self,
        capsys,
    ) -> None:
        """
        Averaged since the start, the first chunk's burst would set a
        rate nobody ever saw and an estimate that finished long before
        the download did.
        """

        progress, _ = self._reporter(4 * 1024)

        # Two samples a second apart, one kilobyte apart: 1 KB/s.
        for _ in range(6):
            progress.update(1024)

        assert 0 < progress._speed <= 2048

    def test_no_estimate_before_anything_is_known(
        self,
        capsys,
    ) -> None:
        """
        A reporter that has not seen a byte has no rate to divide by,
        so it says nothing rather than guessing.
        """

        progress, _ = self._reporter(4 * 1024)

        assert progress._remaining(0) is None

    def test_an_estimate_follows_the_rate(
        self,
        capsys,
    ) -> None:
        progress, _ = self._reporter(4 * 1024)

        for _ in range(4):
            progress.update(1024)

        progress.update(1024)

        remaining = progress._remaining(1024)

        assert remaining is not None
        assert remaining > 0

    def test_a_stalled_transfer_does_not_promise_an_end(
        self,
        capsys,
    ) -> None:
        """
        A speed of zero is the absence of one. Dividing by it would be
        an exception rather than a number.
        """

        progress, _ = self._reporter(1024)

        progress._speed = 0.0

        assert progress._remaining(1024) is None

    def test_the_size_is_picked_up_mid_transfer(
        self,
        capsys,
    ) -> None:
        """
        A server that sends no Content-Length leaves the total unknown
        at the start and names it in a later chunk.
        """

        progress, _ = self._reporter(None)

        progress.update(10, total=100)

        assert progress.total == 100

    def test_it_draws_at_most_so_often(
        self,
        capsys,
    ) -> None:
        """
        A fast link on a small file otherwise prints hundreds of lines
        a second, and the cost of writing them becomes part of what is
        being waited for.
        """

        progress, _ = self._reporter(1024, step=0.001)

        for _ in range(200):
            progress.update(512)

        drawn = capsys.readouterr().out.count("\r")

        assert drawn <= 3


class TestDownloadJob:
    def test_output_path_joins_parts(
        self,
        tmp_path: Path,
    ) -> None:
        job = DownloadJob(
            title="Title",
            quality="1080p",
            option_title="Option",
            url="https://example.com/file",
            output_directory=tmp_path,
            output_filename="file.mp4",
        )

        assert job.output_path == (
            tmp_path / "file.mp4"
        )

    def test_partial_path_sits_beside_output(
        self,
        tmp_path: Path,
    ) -> None:
        job = DownloadJob(
            title="Title",
            quality="1080p",
            option_title="Option",
            url="https://example.com/file",
            output_directory=tmp_path,
            output_filename="file.mp4",
        )

        assert job.partial_path.name == "file.mp4.part"

    def test_with_url_returns_a_copy(
        self,
        tmp_path: Path,
    ) -> None:
        job = DownloadJob(
            title="Title",
            quality="1080p",
            option_title="Option",
            url="https://example.com/file",
            output_directory=tmp_path,
            output_filename="file.mp4",
        )

        updated = job.with_url("https://example.com/other")

        assert job.url == "https://example.com/file"
        assert updated.url == "https://example.com/other"
        assert updated.output_path == job.output_path


class TestCreateOutputDirectory:
    def test_uses_given_root(
        self,
        tmp_path: Path,
    ) -> None:
        directory = create_output_directory(
            "Big Boss",
            root=tmp_path / "downloads",
        )

        assert directory == tmp_path / "downloads" / "Big Boss"
        assert directory.is_dir()

    def test_sanitizes_the_directory_name(
        self,
        tmp_path: Path,
    ) -> None:
        directory = create_output_directory(
            "A/B: C",
            root=tmp_path,
        )

        assert directory.name == "AB C"


class TestCreateDownloadJob:
    def test_carries_option_fields(
        self,
        tmp_path: Path,
    ) -> None:
        job = create_download_job(
            title="Big Boss",
            quality="480p",
            option={
                "title": "S01E01 480p [180MB]",
                "url": "https://cdn.test/480p",
            },
            root=tmp_path,
        )

        assert job.url == "https://cdn.test/480p"
        assert job.option_title == "S01E01 480p [180MB]"
        assert job.output_directory.is_dir()


class TestTotalBytes:
    def _response(
        self,
        length: str | None,
    ) -> httpx.Response:
        headers = (
            {"content-length": length}
            if length is not None
            else {}
        )

        return httpx.Response(
            200,
            headers=headers,
            request=httpx.Request(
                "GET",
                "https://cdn.test/f",
            ),
        )

    def test_adds_already_downloaded(self) -> None:
        assert _total_bytes(
            self._response("500"),
            100,
        ) == 600

    def test_missing_header_is_unknown(self) -> None:
        assert _total_bytes(
            self._response(None),
            0,
        ) is None

    def test_invalid_header_is_unknown(self) -> None:
        assert _total_bytes(
            self._response("abc"),
            0,
        ) is None

    def test_zero_length_is_unknown(self) -> None:
        assert _total_bytes(
            self._response("0"),
            0,
        ) is None


class TestVerifyComplete:
    def test_exact_size_passes(
        self,
        tmp_path: Path,
    ) -> None:
        partial = tmp_path / "f.part"
        partial.write_bytes(b"x" * 100)

        _verify_complete(partial, 100)

    def test_unknown_total_skips_check(
        self,
        tmp_path: Path,
    ) -> None:
        partial = tmp_path / "f.part"
        partial.write_bytes(b"x" * 10)

        _verify_complete(partial, None)

    def test_short_transfer_is_reported(
        self,
        tmp_path: Path,
    ) -> None:
        partial = tmp_path / "f.part"
        partial.write_bytes(b"x" * 40)

        with pytest.raises(DownloadError) as info:
            _verify_complete(partial, 100)

        assert "Incomplete" in str(info.value)
        assert "40" in str(info.value)

        # The partial file is kept so a rerun can resume it.
        assert partial.exists()

    def test_oversized_transfer_is_reported(
        self,
        tmp_path: Path,
    ) -> None:
        partial = tmp_path / "f.part"
        partial.write_bytes(b"x" * 150)

        with pytest.raises(DownloadError) as info:
            _verify_complete(partial, 100)

        assert "150" in str(info.value)

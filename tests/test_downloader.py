"""Tests for hdhub4u.downloader."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from hdhub4u.downloader import (
    DownloadJob,
    _total_bytes,
    _verify_complete,
    create_download_job,
    create_output_directory,
    create_output_filename,
    format_bytes,
    format_progress,
    render_progress_bar,
    sanitize_filename,
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
    def test_respects_width(self) -> None:
        result = render_progress_bar(
            50,
            100,
            width=20,
        )

        inner = result[1:result.index("]")]

        assert len(inner) == 20

    def test_never_exceeds_full(self) -> None:
        result = render_progress_bar(
            200,
            100,
            width=10,
        )

        assert "100.00%" in result

    def test_unknown_total_shows_bytes(self) -> None:
        result = render_progress_bar(
            10,
            None,
            width=10,
        )

        assert "B" in result


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

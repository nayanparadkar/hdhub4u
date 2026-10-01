"""Tests for the interactive flow helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from hdhub4u import cli
from hdhub4u.link_cache import LinkCache
from hdhub4u.quality import MINIMUM_QUALITY, QualityOption
from hdhub4u.resolver import ResolveResult

BEST = QualityOption(
    key="1",
    label="Best available",
    value="best",
)

P_720 = QualityOption(
    key="3",
    label="720p",
    value="720p",
)


class TestAskYesNo:
    def test_yes(self, monkeypatch) -> None:
        monkeypatch.setattr(cli, "prompt", lambda _: "y")

        assert cli.ask_yes_no("? ")

    def test_yes_spelled(self, monkeypatch) -> None:
        monkeypatch.setattr(cli, "prompt", lambda _: "YES")

        assert cli.ask_yes_no("? ")

    def test_no(self, monkeypatch) -> None:
        monkeypatch.setattr(cli, "prompt", lambda _: "n")

        assert not cli.ask_yes_no("? ")

    def test_blank_defaults_to_no(
        self,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(cli, "prompt", lambda _: "")

        assert not cli.ask_yes_no("? ")

    def test_none_defaults_to_no(
        self,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(cli, "prompt", lambda _: None)

        assert not cli.ask_yes_no("? ")


class TestChooseOptionAutomatically:
    def test_picks_smallest(self, capsys) -> None:
        chosen = cli.choose_option_automatically(
            [
                {"title": "1080p [2GB]", "url": "a"},
                {"title": "360p [90MB]", "url": "b"},
            ]
        )

        assert chosen is not None
        assert chosen["url"] == "b"
        assert "360p" in capsys.readouterr().out

    def test_returns_none_without_markers(self) -> None:
        assert (
            cli.choose_option_automatically(
                [{"title": "Download", "url": "a"}]
            )
            is None
        )


class TestSelectDownloadOption:
    def test_single_match_is_chosen(
        self,
        monkeypatch,
    ) -> None:
        chosen = cli.select_download_option(
            [
                {"title": "A 720p", "url": "a"},
                {"title": "B 1080p", "url": "b"},
            ],
            P_720,
        )

        assert chosen is not None
        assert chosen["url"] == "a"

    def test_no_match_reports_error(
        self,
        monkeypatch,
    ) -> None:
        errors: list[str] = []

        monkeypatch.setattr(
            cli,
            "print_error",
            lambda message: errors.append(message),
        )
        monkeypatch.setattr(cli, "pause", lambda *_: None)

        assert (
            cli.select_download_option(
                [{"title": "A 1080p", "url": "a"}],
                P_720,
            )
            is None
        )

        assert any("720p" in message for message in errors)

    def test_multiple_matches_prompt(
        self,
        monkeypatch,
    ) -> None:
        answers = iter(["2"])

        monkeypatch.setattr(
            cli,
            "prompt",
            lambda _: next(answers),
        )
        monkeypatch.setattr(cli, "clear_screen", lambda: None)

        chosen = cli.select_download_option(
            [
                {"title": "A 720p", "url": "a"},
                {"title": "B 720p", "url": "b"},
            ],
            P_720,
        )

        assert chosen is not None
        assert chosen["url"] == "b"

    def test_prompt_back_returns_none(
        self,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            cli,
            "prompt",
            lambda _: "0",
        )
        monkeypatch.setattr(cli, "clear_screen", lambda: None)

        assert (
            cli.select_download_option(
                [
                    {"title": "A 720p", "url": "a"},
                    {"title": "B 720p", "url": "b"},
                ],
                P_720,
            )
            is None
        )

    def test_prompt_non_numeric_retries(
        self,
        monkeypatch,
    ) -> None:
        answers = iter(["x", "1"])
        errors: list[str] = []

        monkeypatch.setattr(
            cli,
            "prompt",
            lambda _: next(answers),
        )
        monkeypatch.setattr(cli, "clear_screen", lambda: None)
        monkeypatch.setattr(cli, "pause", lambda *_: None)
        monkeypatch.setattr(
            cli,
            "print_error",
            lambda message: errors.append(message),
        )

        chosen = cli.select_download_option(
            [
                {"title": "A 720p", "url": "a"},
                {"title": "B 720p", "url": "b"},
            ],
            P_720,
        )

        assert chosen is not None
        assert chosen["url"] == "a"
        assert errors

    def test_prompt_out_of_range_retries(
        self,
        monkeypatch,
    ) -> None:
        answers = iter(["9", "1"])

        monkeypatch.setattr(
            cli,
            "prompt",
            lambda _: next(answers),
        )
        monkeypatch.setattr(cli, "clear_screen", lambda: None)
        monkeypatch.setattr(cli, "pause", lambda *_: None)
        monkeypatch.setattr(cli, "print_error", lambda _: None)

        chosen = cli.select_download_option(
            [
                {"title": "A 720p", "url": "a"},
                {"title": "B 720p", "url": "b"},
            ],
            P_720,
        )

        assert chosen is not None

    def test_prompt_interrupt_returns_none(
        self,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(cli, "prompt", lambda _: None)
        monkeypatch.setattr(cli, "clear_screen", lambda: None)

        assert (
            cli.select_download_option(
                [
                    {"title": "A 720p", "url": "a"},
                    {"title": "B 720p", "url": "b"},
                ],
                P_720,
            )
            is None
        )

    def test_minimum_falls_back_when_unmarked(
        self,
        monkeypatch,
    ) -> None:
        """
        Series pages with per-episode links carry no resolution, so
        the automatic pick falls back to the menu instead of
        refusing outright.
        """

        answers = iter(["1"])

        monkeypatch.setattr(
            cli,
            "prompt",
            lambda _: next(answers),
        )
        monkeypatch.setattr(cli, "clear_screen", lambda: None)

        chosen = cli.select_download_option(
            [{"title": "EPiSODE 1", "url": "a"}],
            MINIMUM_QUALITY,
        )

        assert chosen is not None
        assert chosen["url"] == "a"

    def test_best_returns_first_without_prompting(
        self,
        monkeypatch,
    ) -> None:
        def boom(_: str) -> str:
            raise AssertionError(
                "should not prompt for a single option"
            )

        monkeypatch.setattr(cli, "prompt", boom)

        chosen = cli.select_download_option(
            [{"title": "EPiSODE 1", "url": "a"}],
            BEST,
        )

        assert chosen is not None


class TestLoadDownloadOptions:
    def test_returns_parsed_options(
        self,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            cli,
            "inspect_url",
            lambda url: {
                "error": "",
                "html": (
                    '<a href="https://cdn.test/a-1080p">'
                    "1080p [2GB]</a>"
                ),
                "final_url": "https://site.test/page",
            },
        )

        options, error = cli.load_download_options(
            "https://site.test/page"
        )

        assert error == ""
        assert options is not None
        assert options[0]["url"] == "https://cdn.test/a-1080p"

    def test_propagates_fetch_error(
        self,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            cli,
            "inspect_url",
            lambda url: {"error": "timed out"},
        )

        options, error = cli.load_download_options(
            "https://site.test/page"
        )

        assert options is None
        assert error == "timed out"


class TestVerifyDownloadLink:
    def _cache(
        self,
        isolated_home: Path,
    ) -> LinkCache:
        return LinkCache(
            isolated_home / "data" / "links.json"
        )

    def test_gated_link_is_reported(
        self,
        isolated_home: Path,
        capsys,
        monkeypatch,
    ) -> None:
        class _R:
            def resolve(self, url):
                return ResolveResult(
                    original_url=url,
                    final_url="https://gate.test/f",
                    status_code=200,
                    content_type="text/html",
                    is_media=False,
                    redirects=1,
                    strategy="direct",
                )

        monkeypatch.setattr(
            cli,
            "build_resolver",
            lambda: _R(),
        )

        result, error = cli.verify_download_link(
            "https://gate.test/a"
        )

        assert result is not None
        assert not result.is_media
        assert "gated" in error
        assert "text/html" in error

    def test_media_link_is_cached(
        self,
        isolated_home: Path,
        monkeypatch,
    ) -> None:
        class _R:
            def resolve(self, url):
                return ResolveResult(
                    original_url=url,
                    final_url="https://cdn.test/f.mp4",
                    status_code=200,
                    content_type="video/mp4",
                    is_media=True,
                    redirects=1,
                    strategy="direct",
                )

        monkeypatch.setattr(
            cli,
            "build_resolver",
            lambda: _R(),
        )

        result, error = cli.verify_download_link(
            "https://cdn.test/a"
        )

        assert error == ""
        assert result is not None

        assert self._cache(isolated_home).count() == 1

    def test_cached_link_skips_the_network(
        self,
        isolated_home: Path,
        monkeypatch,
    ) -> None:
        cache = self._cache(isolated_home)

        cache.put(
            "https://cdn.test/a",
            "https://cdn.test/f.mp4",
            "video/mp4",
            "direct",
        )

        def boom():
            raise AssertionError(
                "should not resolve a cached link"
            )

        monkeypatch.setattr(
            cli,
            "build_resolver",
            boom,
        )

        result, error = cli.verify_download_link(
            "https://cdn.test/a"
        )

        assert error == ""
        assert result is not None
        assert "cache" in result.strategy

    def test_network_failure_is_reported(
        self,
        isolated_home: Path,
        monkeypatch,
    ) -> None:
        def raising() -> None:
            raise RuntimeError("no route")

        monkeypatch.setattr(
            cli,
            "build_resolver",
            raising,
        )

        result, error = cli.verify_download_link(
            "https://cdn.test/a"
        )

        assert result is None
        assert "no route" in error

    def test_resolution_failure_is_reported(
        self,
        isolated_home: Path,
        monkeypatch,
    ) -> None:
        class _R:
            def resolve(self, url):
                raise ValueError("bad host")

        monkeypatch.setattr(
            cli,
            "build_resolver",
            lambda: _R(),
        )

        result, error = cli.verify_download_link(
            "https://cdn.test/a"
        )

        assert result is None
        assert "bad host" in error


class TestPrintJobSummary:
    def test_includes_path(
        self,
        tmp_path: Path,
        capsys,
    ) -> None:
        from hdhub4u.downloader import DownloadJob

        cli.print_job_summary(
            DownloadJob(
                title="T",
                quality="auto",
                option_title="O",
                url="https://x.test/a",
                output_directory=tmp_path,
                output_filename="f.mp4",
            )
        )

        output = capsys.readouterr().out

        assert "T" in output
        assert "f.mp4" in output


def test_browse_workflow_returns_on_empty_index(
    isolated_home: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "prompt", lambda _: None)
    monkeypatch.setattr(cli, "pause", lambda *_: None)

    assert cli.browse_workflow() == "back"


def test_browse_workflow_quits_on_interrupt(
    isolated_home: Path,
    monkeypatch,
) -> None:
    """
    With rows present, an interrupt at the prompt exits the app
    rather than looping.
    """

    from hdhub4u.database import (
        add_media_batch,
        initialize_database,
    )

    initialize_database()

    add_media_batch(
        [
            (
                "Some Movie",
                "https://site.test/a",
                "movie",
                "",
            ),
        ]
    )

    monkeypatch.setattr(cli, "prompt", lambda _: None)
    monkeypatch.setattr(cli, "pause", lambda *_: None)

    assert cli.browse_workflow() == "quit"


def test_browse_workflow_reports_empty_index(
    isolated_home: Path,
    monkeypatch,
) -> None:
    errors: list[str] = []

    monkeypatch.setattr(cli, "prompt", lambda _: None)
    monkeypatch.setattr(cli, "pause", lambda *_: None)
    monkeypatch.setattr(
        cli,
        "print_error",
        lambda message: errors.append(message),
    )

    assert cli.browse_workflow() == "back"
    assert any("index" in message for message in errors)


def test_search_workflow_quits_on_interrupt(
    isolated_home: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "prompt", lambda _: None)

    assert cli.search_workflow() == "quit"


def test_search_workflow_reports_failure(
    isolated_home: Path,
    monkeypatch,
    capsys,
) -> None:
    answers = iter(["query", None])
    errors: list[str] = []

    def fake_search(query: str) -> list[dict]:
        raise RuntimeError("index locked")

    monkeypatch.setattr(
        cli,
        "prompt",
        lambda _: next(answers),
    )
    monkeypatch.setattr(cli, "search_media", fake_search)
    monkeypatch.setattr(cli, "pause", lambda *_: None)
    monkeypatch.setattr(
        cli,
        "print_error",
        lambda message: errors.append(message),
    )

    assert cli.search_workflow() == "quit"
    assert any("index locked" in m for m in errors)


def test_menu_workflow_quits(
    isolated_home: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "prompt", lambda _: "5")

    assert cli.menu_workflow() == "quit"


def test_menu_workflow_rejects_bad_option(
    isolated_home: Path,
    monkeypatch,
) -> None:
    answers = iter(["9", "5"])
    errors: list[str] = []

    monkeypatch.setattr(
        cli,
        "prompt",
        lambda _: next(answers),
    )
    monkeypatch.setattr(cli, "pause", lambda *_: None)
    monkeypatch.setattr(
        cli,
        "print_error",
        lambda message: errors.append(message),
    )

    assert cli.menu_workflow() == "quit"
    assert errors


def test_menu_status_option(
    isolated_home: Path,
    monkeypatch,
    capsys,
) -> None:
    answers = iter(["4", "5"])

    monkeypatch.setattr(
        cli,
        "prompt",
        lambda _: next(answers),
    )
    monkeypatch.setattr(cli, "pause", lambda *_: None)

    assert cli.menu_workflow() == "quit"
    assert "Index" in capsys.readouterr().out


def test_show_status_reports_locations(
    isolated_home: Path,
    capsys,
) -> None:
    cli.show_status()

    output = capsys.readouterr().out

    assert "Index" in output
    assert "Downloads" in output
    assert "Cached links" in output


@pytest.mark.parametrize("value", ["", "   "])
def test_prompt_strips_whitespace(
    monkeypatch,
    value: str,
) -> None:
    monkeypatch.setattr(
        "builtins.input",
        lambda _: f"{value}\n",
    )

    assert cli.prompt("> ") == value.strip()


def test_prompt_returns_none_on_eof(
    monkeypatch,
) -> None:
    def raising(_: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", raising)

    assert cli.prompt("> ") is None


def test_prompt_returns_none_on_interrupt(
    monkeypatch,
) -> None:
    def raising(_: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", raising)

    assert cli.prompt("> ") is None

"""Tests for the terminal rendering helpers."""

from __future__ import annotations

import io
import json
import shutil
import subprocess

import pytest
from rich.console import Console

from hdhub4u import ui
from hdhub4u.ui import (
    ResultRow,
    copy_to_clipboard,
    print_error,
    print_json_results,
    print_plain_results,
    print_results,
    show_status,
    terminal_width,
)

_BOX_CHARS = "┏┓┗┛━┳┻╋┃│┡┩└┘├┤─"


def _rows() -> list[ResultRow]:
    return [
        ResultRow(
            title="Big Boss Full Series",
            url="https://site.test/a",
            kind="series",
            quality="1080P 720P",
            year="2020",
        ),
        ResultRow(
            title="Dune",
            url="https://site.test/b",
            kind="movie",
            quality="1080P",
            year="2024",
        ),
    ]


def _wide_rows() -> list[ResultRow]:
    """Rows with the detail real search results carry."""

    return [
        ResultRow(
            title="Dune: Part Two",
            url="https://site.test/dune-2",
            kind="movie",
            quality="2160p HDRip WEB-DL x264 5.1 - DDP5 1",
            year="2024",
        ),
        ResultRow(
            title=(
                "Big Boss Season 1 Complete "
                "Hindi S01E01-S25 1080p WEB-DL"
            ),
            url="https://site.test/big-boss",
            kind="series",
            quality="1080P 720P",
            year="2010",
        ),
        ResultRow(
            title="Avengers: Endgame",
            url="https://site.test/avengers",
            kind="movie",
        ),
    ]


def _rows_as_plain(rows) -> str:
    """Return the plain rendering of rows, without Rich."""

    return "\n".join(
        f"{number:3}. [{row.kind or 'item'}] {row.title}\n"
        f"     {row.url}"
        for number, row in enumerate(rows, start=1)
    ) + "\n"


def _make_console(
    monkeypatch,
    *,
    terminal: bool,
) -> io.StringIO:
    """
    Swap in a real Console with the wanted terminal-ness.

    A real Console rather than a stub, because ``is_terminal`` is a
    read-only property derived from the file, the environment and
    ``FORCE_COLOR`` -- a fake attribute would pin the test to
    whatever this implementation does rather than to the behaviour
    being promised.

    Returns:
        The buffer the console now writes to.
    """

    buffer = io.StringIO()

    monkeypatch.setattr(
        ui,
        "console",
        Console(
            file=buffer,
            force_terminal=terminal or None,
            width=100,
        ),
    )

    return buffer


def test_print_error_shows_message(capsys) -> None:
    print_error("something failed")

    assert "something failed" in capsys.readouterr().out


def test_terminal_width_is_positive() -> None:
    assert terminal_width() > 0


class TestPlainResults:
    """
    A pipe gets plain records. The table is made of box-drawing
    characters, and a script reading a redirect has to strip all of
    them before it can match anything.
    """

    def test_no_row_uses_a_box_character(
        self,
        capsys,
    ) -> None:
        print_plain_results(
            _rows(),
            query="big boss",
        )

        printed = capsys.readouterr().out

        for character in _BOX_CHARS:
            assert character not in printed

    def test_the_trailer_is_absent(
        self,
        capsys,
    ) -> None:
        """
        "page 1 - 2 shown" is a fact about a rendering, and says
        nothing a reader of records can act on.
        """

        print_plain_results(
            _rows(),
            query="big boss",
        )

        printed = capsys.readouterr().out

        assert "page" not in printed
        assert "shown" not in printed

    def test_each_row_prints_a_title_and_an_address(
        self,
        capsys,
    ) -> None:
        print_plain_results(
            _rows(),
            query="big boss",
        )

        printed = capsys.readouterr().out

        assert "Big Boss Full Series" in printed
        assert "https://site.test/a" in printed

    def test_the_address_is_on_a_line_of_its_own(
        self,
        capsys,
    ) -> None:
        """
        So a reader can take every address without the title text in
        the way, using cut or grep rather than a regular expression.
        """

        print_plain_results(
            _rows(),
            query="x",
        )

        lines = capsys.readouterr().out.splitlines()

        assert lines[1] == "     https://site.test/a"

    def test_nothing_matches_is_reported_on_stderr(
        self,
        capsys,
    ) -> None:
        """
        An empty result is not data. Stderr keeps stdout parseable,
        and the exit code is 1 either way.
        """

        print_plain_results([], query="nothing")

        captured = capsys.readouterr()

        assert captured.out == ""
        assert "nothing" in captured.err


class TestJsonResults:
    def test_the_shape_is_stable(
        self,
        capsys,
    ) -> None:
        print_json_results(
            _rows(),
            query="big boss",
        )

        payload = json.loads(capsys.readouterr().out)

        assert payload["query"] == "big boss"
        assert payload["count"] == 2
        assert payload["total"] is None
        assert len(payload["results"]) == 2

    def test_a_reported_total_is_carried_through(
        self,
        capsys,
    ) -> None:
        """
        count is the page; total is the search. Reporting both, and
        null when the service counted nothing, is what lets a script
        page without ever guessing.
        """

        print_json_results(
            _rows(),
            query="big boss",
            total=240,
        )

        payload = json.loads(capsys.readouterr().out)

        assert payload["count"] == 2
        assert payload["total"] == 240

    def test_every_field_a_table_hides_is_present(
        self,
        capsys,
    ) -> None:
        """
        The table drops Type and Quality for width. A script must
        still get them, or dropping the columns would be losing data
        rather than rearranging it.
        """

        print_json_results(
            _rows(),
            query="big boss",
        )

        payload = json.loads(capsys.readouterr().out)
        first = payload["results"][0]

        assert first["type"] == "series"
        assert first["quality"] == "1080P 720P"
        assert first["year"] == "2020"
        assert first["url"] == "https://site.test/a"

    def test_no_matches_is_still_valid_json(
        self,
        capsys,
    ) -> None:
        """
        Emitting nothing at all, or a sentence, is what a consumer
        cannot read. An empty result set is a fact with a shape.
        """

        print_json_results([], query="nothing")

        payload = json.loads(capsys.readouterr().out)

        assert payload["count"] == 0
        assert payload["results"] == []

    def test_a_title_full_of_markup_stays_a_title(
        self,
        capsys,
    ) -> None:
        """
        Site-controlled text reaches JSON as data. It is not markup
        here, so nothing is swallowed and nothing is restyled.
        """

        print_json_results(
            [ResultRow(title="Dune [/] [red]", url="u")],
            query="dune",
        )

        payload = json.loads(capsys.readouterr().out)

        assert (
            payload["results"][0]["title"]
            == "Dune [/] [red]"
        )


class TestPrintResultsRouting:
    def test_json_wins_over_a_terminal(
        self,
        monkeypatch,
        capsys,
    ) -> None:
        _make_console(
            monkeypatch,
            terminal=True,
        )

        print_results(
            _rows(),
            query="big boss",
            as_json=True,
        )

        printed = capsys.readouterr().out

        assert printed.lstrip().startswith("{")
        assert "┏" not in printed

    def test_a_terminal_gets_a_table(
        self,
        monkeypatch,
    ) -> None:
        buffer = _make_console(
            monkeypatch,
            terminal=True,
        )

        print_results(
            _rows(),
            query="big boss",
        )

        printed = buffer.getvalue()

        assert "┏" in printed
        assert "Big Boss Full Series" in printed

    def test_a_pipe_gets_plain_text(
        self,
        monkeypatch,
        capsys,
    ) -> None:
        _make_console(
            monkeypatch,
            terminal=False,
        )

        print_results(
            _rows(),
            query="big boss",
        )

        printed = capsys.readouterr().out

        assert printed == _rows_as_plain(_rows())


class TestWantsTable:
    def test_a_pipe_is_not_a_terminal(
        self,
        monkeypatch,
    ) -> None:
        _make_console(
            monkeypatch,
            terminal=False,
        )

        assert ui.wants_table() is False

    def test_a_terminal_is_a_terminal(
        self,
        monkeypatch,
    ) -> None:
        _make_console(
            monkeypatch,
            terminal=True,
        )

        assert ui.wants_table() is True

    def test_no_color_keeps_the_table(
        self,
        monkeypatch,
        capsys,
    ) -> None:
        """
        NO_COLOR is about colour, not shape. Dropping to plain text
        because someone muted their terminal would answer a
        different question than the one they asked.
        """

        monkeypatch.setenv("NO_COLOR", "1")

        _make_console(
            monkeypatch,
            terminal=True,
        )

        assert ui.wants_table() is True

    def test_force_color_opts_a_pipe_back_into_the_table(
        self,
        monkeypatch,
    ) -> None:
        """
        A redirect asked for in colour should still get a table.
        Rich reads FORCE_COLOR into is_terminal, so the gate needs no
        separate handling.
        """

        monkeypatch.setenv("FORCE_COLOR", "1")
        monkeypatch.delenv("NO_COLOR", raising=False)

        _make_console(
            monkeypatch,
            terminal=False,
        )

        assert ui.wants_table() is True


class TestPageFooter:
    def test_it_reports_the_page_and_the_page_size(self) -> None:
        assert (
            ui.page_footer(_rows(), page=1)
            == "[dim]page 1 \u00b7 2 shown[/dim]"
        )

    def test_an_unreported_total_is_left_out(self) -> None:
        """
        The offline index cannot count a fuzzy match, so saying "0
        matches" beside a full page would be a lie. The count is the
        one thing here that has to be real.
        """

        assert "matches" not in ui.page_footer(
            _rows(),
            total=0,
        )

    def test_a_reported_total_is_shown(self) -> None:
        assert ui.page_footer(_rows(), total=137).endswith(
            "\u00b7 137 matches[/dim]"
        )

    def test_a_single_match_is_not_plural(self) -> None:
        assert ui.page_footer(_rows(), total=1).endswith(
            "· 1 match[/dim]"
        )


class TestTableResults:
    """
    The table is the first thing a user sees and the easiest place to
    get something quietly wrong, because a table that overflows looks
    like a table that simply has a lot in it.
    """

    def _render(self, monkeypatch, rows, width, **kwargs) -> str:
        monkeypatch.setattr(
            ui,
            "console",
            Console(
                file=(buffer := io.StringIO()),
                force_terminal=False,
                width=width,
                no_color=True,
            ),
        )

        ui.print_table_results(rows, **kwargs)

        return buffer.getvalue()

    @pytest.mark.parametrize(
        "width",
        [60, 70, 80, 100, 120, 200],
    )
    def test_the_table_fits_the_terminal(
        self,
        monkeypatch,
        width: int,
    ) -> None:
        printed = self._render(
            monkeypatch,
            _wide_rows(),
            width,
            query="dune",
        )

        too_long = [
            line
            for line in printed.splitlines()
            if len(line) > width
        ]

        assert too_long == []

    @pytest.mark.parametrize(
        "width",
        [60, 70, 80, 100, 120, 200],
    )
    def test_every_row_is_framed(
        self,
        monkeypatch,
        width: int,
    ) -> None:
        """
        The old five-column table summed to about 106 characters with
        its borders. Rich never clipped it -- the right border walked
        off the screen at 80 columns, so the frame was simply gone.
        """

        printed = self._render(
            monkeypatch,
            _wide_rows(),
            width,
            query="dune",
        )

        table_lines = [
            line
            for line in printed.splitlines()
            if "Dune" in line or "Big Boss" in line
        ]

        assert table_lines
        assert all(
            line.startswith("\u2502") and line.endswith("\u2502")
            for line in table_lines
        )

    def test_only_three_columns_are_drawn(
        self,
        monkeypatch,
    ) -> None:
        """
        Quality and kind still reach a script through --json. They do
        not need to compete with the title for a terminal.
        """

        printed = self._render(
            monkeypatch,
            _rows(),
            100,
            query="big boss",
        )

        header = printed.splitlines()[1]

        assert "Title" in header
        assert "Year" in header
        assert "Type" not in header
        assert "Quality" not in header
        assert "1080P 720P" not in printed

    def test_a_long_title_is_kept_whole(
        self,
        monkeypatch,
    ) -> None:
        printed = self._render(
            monkeypatch,
            _wide_rows(),
            60,
            query="big boss",
        )

        squeezed = printed.replace("\n", "")

        # Wrapped across lines, not truncated.
        assert "Big Boss Season 1 Complete Hindi" in squeezed

    def test_a_row_with_no_year_shows_a_dash(
        self,
        monkeypatch,
    ) -> None:
        printed = self._render(
            monkeypatch,
            [
                ResultRow(
                    title="Untitled Documentary",
                    url="https://site.test/c",
                ),
            ],
            100,
            query="doc",
        )

        assert "Untitled Documentary" in printed

    def test_the_footer_carries_a_reported_total(
        self,
        monkeypatch,
    ) -> None:
        printed = self._render(
            monkeypatch,
            _rows(),
            100,
            query="big boss",
            page=3,
            total=240,
        )

        assert "page 3 \u00b7 2 shown \u00b7 240 matches" in printed

    def test_the_footer_omits_an_unknown_total(
        self,
        monkeypatch,
    ) -> None:
        printed = self._render(
            monkeypatch,
            _rows(),
            100,
            query="big boss",
        )

        assert "page 1 \u00b7 2 shown" in printed
        assert "matches" not in printed

    def test_no_results_is_a_panel_not_an_empty_table(
        self,
        monkeypatch,
    ) -> None:
        printed = self._render(
            monkeypatch,
            [],
            100,
            query="asdfgh",
        )

        assert "\u250f" not in printed
        assert "Nothing matched" in printed


class TestCopyToClipboard:
    def test_without_utility(
        self,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            shutil,
            "which",
            lambda _: None,
        )

        assert copy_to_clipboard("text") is False


def test_copy_to_clipboard_uses_first_available(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: (
            "/usr/bin/wl-copy"
            if name == "wl-copy"
            else None
        ),
    )

    calls: list[list[str]] = []

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: calls.append(command),
    )

    assert copy_to_clipboard("text")
    assert calls == [["wl-copy"]]


def test_copy_to_clipboard_skips_failures(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: f"/usr/bin/{name}",
    )

    def failing(command, **kwargs):
        raise OSError("no display")

    monkeypatch.setattr(
        subprocess,
        "run",
        failing,
    )

    assert copy_to_clipboard("text") is False


def test_print_error_does_not_read_site_text_as_markup(
    capsys,
) -> None:
    """
    Error messages carry server names, page titles and exception text
    naming URLs. Interpolated into a Rich markup string, a title of
    "[red]" was applied as a style and "[/]" was swallowed, so the
    panel showed the program's words and dropped the site's.
    """

    print_error("Dune [/] [red]not found[/red]")

    printed = capsys.readouterr().out

    assert "Dune" in printed
    assert "[red]" in printed
    assert "[/]" in printed


def test_clear_screen_does_not_shell_out(
    monkeypatch,
    capsys,
) -> None:
    """
    It used to run ``os.system("clear")``, which put a command
    through the shell, forked a process, and on a system without the
    program printed "sh: 1: clear: not found" into the middle of the
    output.
    """

    def forbidden(*args, **kwargs):
        raise AssertionError("shelled out")

    monkeypatch.setattr(ui.os, "system", forbidden)

    ui.clear_screen()

    capsys.readouterr()


def test_show_status_reports_the_data_locations(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    """
    Moved here from the removed offline CLI, which was the only thing
    importing it. It is the one command that tells a user where their
    files are, so it has to survive the deletion of the module it came
    from.
    """

    monkeypatch.setenv("HDHUB_HOME", str(tmp_path))

    from hdhub4u.database import initialize_database

    initialize_database()

    show_status()

    printed = capsys.readouterr().out

    assert "media.db" in printed
    assert "Downloads" in printed
    assert "Browser" in printed
    assert "Page rendering" in printed

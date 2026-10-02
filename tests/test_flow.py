"""
Tests for hdhub4u.flow.

The flow is what a user actually walks, and it was the one part of
the live path with no tests at all: nothing in the suite imported
this module, so the interactive logic could be changed freely and
only a real session would find out. Most of what follows is about
input handling and about not letting site text be read as terminal
markup, both of which fail in ways a user sees immediately.
"""

from __future__ import annotations

import io
import os
import pty
import sys
import threading
import time
from pathlib import Path
from typing import IO

import httpx
import pytest
from rich.console import Console

from hdhub4u import flow
from hdhub4u import keys as keys_mod
from hdhub4u import ui
from hdhub4u.catalog import (
    CatalogItem,
    MediaOption,
    SearchPage,
)
from hdhub4u.errors import DownloadError, NetworkError, ResolutionError
from hdhub4u.flow import (
    AGAIN,
    BACK,
    QUIT,
)


def _item(
    title: str = "Dune",
    url: str = "https://hdhub.test/dune",
) -> CatalogItem:
    return CatalogItem(title=title, url=url, year="2021")


def _option(
    label: str = "1080p",
    url: str = "https://hubcdn.club/f",
    kind: str = "download",
    host: str = "hubcdn.club",
    resolution: str = "1080p",
    size_label: str = "1.2 GB",
) -> MediaOption:
    return MediaOption(
        label=label,
        url=url,
        kind=kind,
        host=host,
        resolution=resolution,
        size_label=size_label,
    )


def _as_chunks(pressed: bytes) -> list[bytes]:
    """
    Split a key sequence into whole keypresses.

    One write per keypress with a pause between, which is both what a
    keyboard does and what keeps a lone Escape distinguishable from
    the start of an arrow key. Splitting per byte instead would deliver
    an arrow key as three presses, which is the bug the real-terminal
    tests exist to catch -- so this deliberately does not do that.
    """

    chunks: list[bytes] = []
    rest = pressed

    while rest:
        if rest[:1] == b"\x1b" and len(rest) > 1:
            length = 3 if rest[1:2] in (b"[", b"O") else 1
        else:
            length = 1

        chunks.append(rest[:length])
        rest = rest[length:]

    return chunks


def _pty_stream() -> tuple[int, IO]:
    """Return a real terminal as a text stream, plus the other end."""

    controller, terminal = pty.openpty()

    return controller, os.fdopen(terminal, "r", encoding="utf-8")


@pytest.fixture
def screen(
    monkeypatch: pytest.MonkeyPatch,
) -> io.StringIO:
    """
    Capture what the flow prints.

    Returns the buffer, with markup disabled so that a cell built as
    a ``Text`` and a string with no tags are indistinguishable -- a
    test asserting on markup would pass either way, which is the bug
    being guarded against.
    """

    buffer = io.StringIO()

    console = Console(
        file=buffer,
        force_terminal=False,
        width=120,
        no_color=True,
    )

    monkeypatch.setattr(flow, "console", console)

    # print_error and print_header are imported names that resolve
    # ui.console at call time, so ui's own global has to be captured
    # too. Both now hold the one Console the application uses; the
    # two attributes are patched separately only because rebinding a
    # module global does not reach a name already imported from it.
    monkeypatch.setattr(ui, "console", console)

    monkeypatch.setattr(
        flow,
        "clear_screen",
        lambda: None,
    )

    # Every prompt and every "press enter" is answered from the test,
    # since nothing here should ever read the real stdin.
    monkeypatch.setattr(flow, "pause", lambda *a, **k: None)

    return buffer


class TestParseChoice:
    def test_a_number_within_range(self) -> None:
        assert flow._parse_choice("3", 5) == "3"

    def test_a_number_past_the_end(self) -> None:
        assert flow._parse_choice("9", 5) == AGAIN

    def test_zero_is_not_a_choice(self) -> None:
        """Options are numbered from one."""

        assert flow._parse_choice("0", 5) == AGAIN

    def test_quit_and_back_words(self) -> None:
        assert flow._parse_choice("q", 5) == QUIT
        assert flow._parse_choice("b", 5) == BACK

    def test_end_of_input_quits(self) -> None:
        """
        A closed stdin is how a piped session ends, and it should
        leave rather than loop asking for a number that can never
        arrive.
        """

        assert flow._parse_choice(None, 5) == QUIT

    def test_a_superscript_does_not_crash(self) -> None:
        """
        ``"²".isdigit()`` is True and ``int("²")`` raises, so the
        old check turned one keystroke into a ValueError and ended
        the session. ``isdecimal`` is the test that matches what
        ``int`` accepts.
        """

        assert flow._parse_choice("²", 5) == AGAIN

    def test_an_arabic_indic_digit_selects_its_number(self) -> None:
        """
        ``isdecimal`` accepts these and ``int`` reads them as the
        number they denote, so a user typing in another script gets
        the option they asked for rather than a complaint.
        """

        assert flow._parse_choice("٢", 5) == "٢"


class TestSuffix:
    def test_keeps_a_known_media_extension(self) -> None:
        assert flow._suffix("dune.mkv", "matroska") == ".mkv"

    def test_is_case_insensitive(self) -> None:
        assert flow._suffix("Dune.MP4", "mp4") == ".mp4"

    def test_replaces_a_page_extension(self) -> None:
        """
        The filename comes from a server. Taken literally it saved
        the file as ``.php`` or ``.html``, which misdescribes the
        content and is a good way to be talked into running it.
        """

        assert flow._suffix("gate.php", "matroska") == ".mkv"
        assert flow._suffix("index.html", "mp4") == ".mp4"

    def test_replaces_an_executable_extension(self) -> None:
        assert flow._suffix("setup.exe", "mp4") == ".mp4"

    def test_a_double_extension_does_not_win(self) -> None:
        assert flow._suffix("archive.tar.gz", "mp3") == ".mp3"

    def test_uses_the_container_when_there_is_no_extension(
        self,
    ) -> None:
        assert flow._suffix("stream", "mpeg-ts") == ".ts"

    def test_a_dotless_name(self) -> None:
        assert flow._suffix("dune", "avi") == ".avi"


class TestRenderResults:
    def test_a_site_title_is_not_read_as_markup(
        self,
        screen: io.StringIO,
    ) -> None:
        """
        A title containing markup used to be swallowed by Rich, so
        the row showed nothing at all and the user could not tell
        which entry they were looking at.
        """

        flow.render_results(
            [_item(title="Movie [/] [red]Evil[/red]")],
            "dune",
            1,
        )

        printed = screen.getvalue()

        assert "Evil" in printed
        assert "[/]" in printed

    def test_no_results_says_so(self, screen: io.StringIO) -> None:
        flow.render_results([], "nothing", 1)

        assert "No results" in screen.getvalue()

    def test_the_query_is_shown_verbatim(
        self,
        screen: io.StringIO,
    ) -> None:
        flow.render_results([], "[red]q[/red]", 1)

        assert "[red]q[/red]" in screen.getvalue()

    def test_the_page_number_is_reported(
        self,
        screen: io.StringIO,
    ) -> None:
        flow.render_results([_item()], "dune", 3)

        assert "page 3" in screen.getvalue()


class TestRenderOptions:
    def test_a_title_full_of_markup_survives(
        self,
        screen: io.StringIO,
    ) -> None:
        flow.render_options(
            _item(title="Dune [/] [bold]"),
            [_option()],
        )

        printed = screen.getvalue()

        assert "Dune" in printed
        assert "[/]" in printed

    def test_a_scrape_label_survives(self, screen: io.StringIO) -> None:
        flow.render_options(
            _item(),
            [_option(label="[/] 1080p [x]")],
        )

        printed = screen.getvalue()

        assert "1080p" in printed
        assert "[x]" in printed

    def test_a_title_with_no_options_says_so(
        self,
        screen: io.StringIO,
    ) -> None:
        flow.render_options(_item(), [])

        assert "No options" in screen.getvalue()


class TestOptionTable:
    def test_it_shows_four_columns(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        """
        Number, quality, size, kind. Host went because nobody chooses
        a download on which CDN carries it.
        """

        flow.render_options(_item(), [_option()])

        printed = screen.getvalue()

        for column in ("Quality", "Size", "Kind"):
            assert column in printed

        assert "Host" not in printed

    def test_a_long_label_is_not_cut(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        """
        The old cell sliced the label at 34 characters whatever the
        terminal was, so a 140-column window showed the same truncated
        text as an 80-column one.
        """

        label = "1080p Hindi WEB-DL x264 AAC 2.0 Chapter 12 (2024)"

        flow.render_options(_item(), [_option(label=label)])

        assert label in screen.getvalue()

    def test_the_cursor_row_is_marked(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        table = flow.option_table(
            [_option(label="480p"), _option(label="720p")],
            cursor=1,
        )

        assert [row.style for row in table.rows] == [
            None,
            "bold reverse",
        ]

    def test_no_cursor_marks_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        table = flow.option_table([_option()])

        assert [row.style for row in table.rows] == [None]

    def test_the_help_line_says_what_the_keys_do(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        flow.render_options(_item(), [_option()])

        printed = screen.getvalue()

        for key in ("move", "choose", "downloads", "streams", "back"):
            assert key in printed


class TestChooseOptionByKey:
    """
    The arrow-key picker, driven through a real terminal.

    pytest's stdin is not a tty, which is correct -- it is how every
    other picker test reaches the typed-number path -- and it is also
    why this class swaps in a pty. Handing a key sequence to
    ``keys.read_key`` directly would skip the half that breaks: raw
    mode, and whether the terminal delivers an arrow key as one
    keypress or three.
    """

    def _pick(
        self,
        monkeypatch: pytest.MonkeyPatch,
        options: list[MediaOption],
        keys_pressed: bytes,
    ) -> MediaOption | None:
        controller, terminal = _pty_stream()

        monkeypatch.setattr(sys, "stdin", terminal)

        buffer = io.StringIO()

        monkeypatch.setattr(
            ui,
            "console",
            Console(
                file=buffer,
                force_terminal=False,
                width=100,
                no_color=True,
            ),
        )
        monkeypatch.setattr(flow, "console", ui.console)

        def press() -> None:
            for chunk in _as_chunks(keys_pressed):
                os.write(controller, chunk)
                time.sleep(keys_mod.SEQUENCE_TIMEOUT * 4)

        reader = threading.Thread(target=press)
        reader.start()

        try:
            chosen = flow.choose_option(options)

        finally:
            reader.join(timeout=5)
            os.close(controller)
            terminal.close()

        return chosen

    def test_down_then_enter_takes_the_second(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        options = [_option(label="480p"), _option(label="720p")]

        chosen = self._pick(
            monkeypatch,
            options,
            b"\x1b[B\r",
        )

        assert chosen is not None
        assert chosen.label == "720p"

    def test_up_wraps_to_the_last(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Wrapping is what makes a long list usable: the cursor can stop
        where it likes without the ends being dead space.
        """

        options = [_option(label="a"), _option(label="b")]

        chosen = self._pick(monkeypatch, options, b"\x1b[A\r")

        assert chosen is not None
        assert chosen.label == "b"

    def test_escape_goes_back(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assert self._pick(
            monkeypatch,
            [_option()],
            b"\x1b",
        ) is None

    def test_quit_unwinds(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with pytest.raises(flow.QuitFlow):
            self._pick(monkeypatch, [_option()], b"q")

    def test_d_narrows_to_downloads(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        options = [
            _option(label="stream-a", kind="streaming"),
            _option(label="stream-b", kind="streaming"),
            _option(label="file", kind="download"),
        ]

        chosen = self._pick(
            monkeypatch,
            options,
            b"d\r",
        )

        assert chosen is not None
        assert chosen.label == "file"

    def test_a_typed_number_still_works(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A terminal can be driven by typing as well as by pointing. If
        the arrow keys were the only way in, the picker would be
        unusable for anyone whose terminal does not send them.
        """

        options = [_option(label="480p"), _option(label="720p")]

        chosen = self._pick(monkeypatch, options, b"2")

        assert chosen is not None
        assert chosen.label == "720p"

    def test_a_filter_with_nothing_to_show_keeps_the_list(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        "s" on a downloads-only post used to leave an empty table with
        quit as the only way out.
        """

        chosen = self._pick(
            monkeypatch,
            [_option(label="file", kind="download")],
            b"s\r",
        )

        assert chosen is not None
        assert chosen.label == "file"


class TestChooseOption:
    def _answer(
        self,
        monkeypatch: pytest.MonkeyPatch,
        reply: str,
    ) -> None:
        monkeypatch.setattr(
            flow,
            "_prompt",
            lambda *args, **kwargs: reply,
        )

    def test_picks_by_number(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        options = [_option(label="480p"), _option(label="720p")]

        self._answer(monkeypatch, "2")

        chosen = flow.choose_option(options)

        assert chosen is not None
        assert chosen.label == "720p"

    def test_picks_the_first_download_by_letter(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        options = [
            _option(label="stream", kind="streaming"),
            _option(label="480p"),
            _option(label="720p"),
        ]

        self._answer(monkeypatch, "d")

        chosen = flow.choose_option(options)

        assert chosen is not None
        assert chosen.label == "480p"

    def test_back_returns_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        self._answer(monkeypatch, "b")

        assert flow.choose_option([_option()]) is None

    def test_a_superscript_does_not_crash(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        """
        Same defect as in the results list, reached through the other
        prompt, and it ended the session rather than asking again.
        """

        replies = iter(["²", "1"])

        monkeypatch.setattr(
            flow,
            "_prompt",
            lambda *args, **kwargs: next(replies),
        )

        chosen = flow.choose_option([_option()])

        assert chosen is not None
        assert "Enter an option number" in screen.getvalue()

    def test_a_number_past_the_end_asks_again(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        replies = iter(["9", "1"])

        monkeypatch.setattr(
            flow,
            "_prompt",
            lambda *args, **kwargs: next(replies),
        )

        chosen = flow.choose_option([_option()])

        assert chosen is not None
        assert "Enter an option number" in screen.getvalue()

    def test_asks_again_when_no_download_is_offered(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        """
        The shortcut has to fall back to the list rather than
        choosing a stream, and the user then picks one by number.
        """

        replies = iter(["d", "1"])

        monkeypatch.setattr(
            flow,
            "_prompt",
            lambda *a, **k: next(replies),
        )

        chosen = flow.choose_option([_option(kind="streaming")])

        assert chosen is not None
        assert "No download options" in screen.getvalue()

    def test_asks_again_when_no_stream_is_offered(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        replies = iter(["s", "1"])

        monkeypatch.setattr(
            flow,
            "_prompt",
            lambda *a, **k: next(replies),
        )

        chosen = flow.choose_option([_option(kind="download")])

        assert chosen is not None
        assert "No streaming options" in screen.getvalue()

    def test_quitting_raises_to_leave(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        """
        Quitting from the option list is not "go back to the
        results"; it is leaving the program, so it has to be
        distinguishable from a back.
        """

        self._answer(monkeypatch, "q")

        with pytest.raises(flow.QuitFlow):
            flow.choose_option([_option()])


class TestStartDownload:
    def _unlocked(
        self,
        monkeypatch: pytest.MonkeyPatch,
        filename: str = "Dune 2021 1080p.mkv",
    ) -> None:
        from hdhub4u.unlock import UnlockedLink

        monkeypatch.setattr(
            flow.unlock,
            "unlock",
            lambda option, client=None, cache=None: UnlockedLink(
                url="https://cdn.test/real.mkv",
                filename=filename,
                host="cdn.test",
                container="matroska",
                size_bytes=1024,
                strategy="direct",
            ),
        )

    def _decline(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(flow, "_ask", lambda question: False)

    def test_declining_writes_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
        tmp_path: Path,
    ) -> None:
        self._unlocked(monkeypatch)
        self._decline(monkeypatch)

        assert (
            flow.start_download(_item(), _option()) is False
        )

    def test_a_resolution_failure_is_reported_not_raised(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        """
        A dead gate is an ordinary outcome. Letting it escape ended
        the whole session over one bad option.
        """

        def failing(option, client=None, cache=None):
            raise ResolutionError("the site deleted this file")

        monkeypatch.setattr(flow.unlock, "unlock", failing)

        assert flow.start_download(_item(), _option()) is False
        assert "Could not open" in screen.getvalue()

    def test_a_network_failure_is_reported_not_raised(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        def failing(option, client=None, cache=None):
            raise NetworkError("the site did not answer")

        monkeypatch.setattr(flow.unlock, "unlock", failing)

        assert flow.start_download(_item(), _option()) is False
        assert "Could not open" in screen.getvalue()

    def test_a_filename_full_of_markup_is_shown(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        self._unlocked(monkeypatch, filename="Dune [/] [red].mkv")
        self._decline(monkeypatch)

        flow.start_download(_item(), _option())

        printed = screen.getvalue()

        assert "[red]" in printed
        assert "[/]" in printed

    def test_a_page_extension_is_not_saved(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
        tmp_path: Path,
    ) -> None:
        """
        A server-named file ending in .php is a page, whatever the
        probe found in its bytes.
        """

        self._unlocked(monkeypatch, filename="gate.php")
        monkeypatch.setattr(
            flow,
            "_ask",
            lambda question: True,
        )
        monkeypatch.setattr(
            flow,
            "get_downloads_dir",
            lambda: tmp_path,
        )

        seen: dict[str, object] = {}

        def spy(**kwargs):
            seen.update(kwargs)
            raise DownloadError("stop here")

        monkeypatch.setattr(flow, "create_download_job", spy)

        with pytest.raises(DownloadError):
            flow.start_download(_item(), _option())

        assert seen["extension"] == ".mkv"


class TestRunSearch:
    def test_no_results_is_a_failure_exit_code(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        """
        A script driving this needs to be able to tell an empty
        result from a successful one. Returning 0 for both means
        telling it a download is available when none is.
        """

        monkeypatch.setattr(
            flow,
            "search_page",
            lambda *a, **k: SearchPage(),
        )

        assert (
            flow.run_search(
                "nothing",
                interactive=False,
                client=httpx.Client(),
            )
            == 1
        )

    def test_results_are_a_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        monkeypatch.setattr(
            flow,
            "search_page",
            lambda *a, **k: SearchPage([_item()]),
        )

        assert (
            flow.run_search(
                "dune",
                interactive=False,
                client=httpx.Client(),
            )
            == 0
        )

    def test_a_search_failure_exits_nonzero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        def failing(*args, **kwargs):
            raise NetworkError("the site did not answer")

        monkeypatch.setattr(flow, "search_page", failing)

        assert (
            flow.run_search(
                "dune",
                interactive=False,
                client=httpx.Client(),
            )
            == 1
        )

    def test_quitting_is_a_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        """
        The user leaving on purpose is the session finishing
        normally, and reporting failure for it would make a wrapper
        think something went wrong.
        """

        monkeypatch.setattr(
            flow,
            "browse_results",
            lambda *a, **k: QUIT,
        )

        assert (
            flow.run_search(
                "dune",
                client=httpx.Client(),
            )
            == 0
        )

    def test_a_search_error_in_the_browser_exits_nonzero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        monkeypatch.setattr(
            flow,
            "browse_results",
            lambda *a, **k: BACK,
        )

        assert (
            flow.run_search(
                "dune",
                client=httpx.Client(),
            )
            == 1
        )

    def test_an_interrupt_exits_by_convention(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(flow, "browse_results", interrupted)

        assert (
            flow.run_search(
                "dune",
                client=httpx.Client(),
            )
            == 130
        )


class TestShowTitle:
    def test_no_options_returns_to_the_results(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        monkeypatch.setattr(flow, "pause", lambda *a, **k: None)

        assert flow.show_title(_item(), []) == "back"

    def test_going_back_returns_to_the_results(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        monkeypatch.setattr(
            flow,
            "choose_option",
            lambda options: None,
        )

        assert flow.show_title(_item(), [_option()]) == "back"

    def test_a_stream_is_refused_with_its_address(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        """
        A stream is not a file this program can transfer, and
        saying so with the address is the useful answer. The address
        is server text, so it must not be read as markup.
        """

        monkeypatch.setattr(
            flow,
            "choose_option",
            lambda options: _option(
                label="Stream",
                url="https://stream.test/watch[/]?x=1",
                kind="streaming",
            ),
        )
        monkeypatch.setattr(flow, "pause", lambda *a, **k: None)

        assert flow.show_title(_item(), [_option()]) == AGAIN

        printed = screen.getvalue()

        assert "stream" in printed.lower()
        assert "[/]" in printed

    def test_a_download_is_attempted(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        monkeypatch.setattr(
            flow,
            "choose_option",
            lambda options: _option(),
        )
        monkeypatch.setattr(
            flow,
            "start_download",
            lambda item, option, client=None: True,
        )
        monkeypatch.setattr(flow, "pause", lambda *a, **k: None)

        assert flow.show_title(_item(), [_option()]) == AGAIN


class TestOneConsole:
    def test_the_flow_and_the_ui_share_one_console(self) -> None:
        """
        Two Console objects meant two width decisions, and Rich
        measures width per console -- so a table could be laid out for
        one width while the panel above it was drawn for another.
        """

        assert flow.console is ui.console

    def test_the_flow_prints_nothing_else(
        self,
        capsys,
    ) -> None:
        assert flow.console.file is sys.stdout


class TestBrowseResults:
    def test_paging_asks_the_catalog_for_the_next_page(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        pages: list[int] = []

        def fake_search(query, limit=20, page=1, client=None):
            pages.append(page)
            return SearchPage([_item()])

        monkeypatch.setattr(flow, "search_page", fake_search)

        replies = iter(["n", "q"])
        monkeypatch.setattr(
            flow,
            "_prompt",
            lambda *a, **k: next(replies),
        )

        flow.browse_results(
            "dune",
            client=httpx.Client(),
        )

        assert pages == [1, 2]

    def test_a_new_search_is_requested(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        monkeypatch.setattr(
            flow,
            "search_page",
            lambda *a, **k: SearchPage([_item()]),
        )

        replies = iter(["b", "q"])
        monkeypatch.setattr(
            flow,
            "_prompt",
            lambda *a, **k: next(replies),
        )

        assert (
            flow.browse_results(
                "dune",
                client=httpx.Client(),
            )
            == BACK
        )

    def test_a_search_failure_returns_to_the_prompt(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        def failing(*args, **kwargs):
            raise NetworkError("the site did not answer")

        monkeypatch.setattr(flow, "search_page", failing)

        assert (
            flow.browse_results(
                "dune",
                client=httpx.Client(),
            )
            == BACK
        )

    def test_quitting_stops_the_loop(
        self,
        monkeypatch: pytest.MonkeyPatch,
        screen: io.StringIO,
    ) -> None:
        monkeypatch.setattr(
            flow,
            "search_page",
            lambda *a, **k: SearchPage([_item()]),
        )
        monkeypatch.setattr(
            flow,
            "_prompt",
            lambda *a, **k: "q",
        )

        assert (
            flow.browse_results(
                "dune",
                client=httpx.Client(),
            )
            == QUIT
        )


class TestUserAgent:
    def test_names_a_browser(self) -> None:
        """
        The search endpoint answers a browser and does not answer
        httpx, so the user agent is part of making the request work
        at all rather than a nicety.
        """

        agent = flow._user_agent()

        assert agent
        assert "python" not in agent.lower()
        assert "httpx" not in agent.lower()

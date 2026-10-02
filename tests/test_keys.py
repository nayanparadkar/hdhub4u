"""
Tests for hdhub4u.keys.

The parsing is the easy half to get wrong and the easy half to test.
What is not testable with a string is the half that actually breaks:
entering raw mode, and whether the terminal hands over an arrow key as
one keypress or three. That needs a pty, and the pty tests at the
bottom of this file are the ones that matter.
"""

from __future__ import annotations

import io
import os
import pty
import threading
import time
from typing import IO

import pytest

from hdhub4u import keys


class TestDecode:
    @pytest.mark.parametrize(
        ("chars", "expected"),
        [
            ("\r", "enter"),
            ("\n", "enter"),
            ("\t", "tab"),
            ("\x7f", "backspace"),
            ("\x03", "ctrl-c"),
            ("a", "a"),
            ("q", "q"),
            ("1", "1"),
        ],
    )
    def test_named_and_printable_keys(
        self,
        chars: str,
        expected: str,
    ) -> None:
        assert keys.decode(chars) == expected

    @pytest.mark.parametrize(
        ("chars", "expected"),
        [
            ("\x1b[A", "up"),
            ("\x1b[B", "down"),
            ("\x1b[C", "right"),
            ("\x1b[D", "left"),
            ("\x1bOA", "up"),
            ("\x1bOD", "left"),
            ("\x1b[5~", "pageup"),
            ("\x1b[6~", "pagedown"),
            ("\x1b[H", "home"),
            ("\x1b[1~", "home"),
            ("\x1b[F", "end"),
            ("\x1b[4~", "end"),
            ("\x1b[3~", "delete"),
            ("\x1b[Z", "shifttab"),
        ],
    )
    def test_every_sequence_the_terminal_sends(
        self,
        chars: str,
        expected: str,
    ) -> None:
        assert keys.decode(chars) == expected

    def test_a_lone_escape_is_escape(self) -> None:
        """
        An Escape and the first byte of an arrow key are the same
        byte. decode() is handed the rest already, so a lone one is
        unambiguous here -- read_key is where the ambiguity lives.
        """

        assert keys.decode("\x1b") == "esc"

    def test_escape_then_a_key_is_that_key(self) -> None:
        assert keys.decode("\x1bq") == "q"
        assert keys.decode("\x1b\r") == "enter"

    def test_nothing_is_nothing(self) -> None:
        assert keys.decode("") is None

    def test_the_longest_sequence_wins(self) -> None:
        """
        "[1~" must not be read as the first two bytes of something
        else, which a short-prefix match would do.
        """

        assert keys.decode("\x1b[1~") == "home"
        assert keys.decode("\x1b[6~") == "pagedown"

    def test_an_unrecognised_sequence_says_so(self) -> None:
        assert keys.decode("\x1b[9~") == "unknown"

    def test_a_longer_sequence_falls_back_to_its_prefix(self) -> None:
        """
        "[Z~" is not a sequence, but "[Z" is. Reporting the tab rather
        than "unknown" keeps a keypress from being dropped.
        """

        assert keys.decode("\x1b[Z~") == "shifttab"

    def test_every_listed_sequence_is_reachable(self) -> None:
        for sequence, name in keys.SEQUENCES.items():
            assert keys.decode("\x1b" + sequence) == name


class TestCanReadKeys:
    def test_a_pipe_cannot(self) -> None:
        assert keys.can_read_keys(io.StringIO()) is False

    def test_a_closed_stream_cannot(self) -> None:
        """
        pytest replaces stdin with an object that has no usable
        descriptor, and the guard is what keeps every picker test
        from trying to enter raw mode.
        """

        assert keys.can_read_keys(_NoFileno()) is False

    def test_a_stream_raising_is_not_a_crash(self) -> None:
        assert keys.can_read_keys(_Exploding()) is False

    def test_an_empty_name_is_not_a_sequence_part(self) -> None:
        assert keys._is_sequence_char("[") is True
        assert keys._is_sequence_char("a") is False


class TestRawMode:
    def test_it_declines_a_pipe_rather_than_raising(self) -> None:
        with keys.raw_mode(io.StringIO()) as entered:
            assert entered is False

    def test_it_declines_a_closed_stream(self) -> None:
        with keys.raw_mode(_NoFileno()) as entered:
            assert entered is False


class _NoFileno:
    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        raise ValueError("I/O operation on closed file")


class _Exploding:
    def isatty(self) -> bool:
        raise OSError("no")


def _pty_stream() -> tuple[int, IO]:
    """
    Return a real terminal as a text stream, plus the other end.

    ``pty.openpty`` gives a genuine tty pair, so ``isatty`` and raw
    mode mean what they mean for a user's terminal. Nothing about this
    can be faked convincingly: the failure it guards against was
    invisible to any test that did not have one.
    """

    controller, terminal = pty.openpty()

    return controller, os.fdopen(terminal, "r", encoding="utf-8")


class TestOnARealTerminal:
    """
    The tests that would have caught the bug this module was written
    for.
    """

    @pytest.mark.skipif(
        not hasattr(os, "openpty"),
        reason="no pty on this platform",
    )
    def test_an_arrow_key_arrives_as_one_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Reading one character at a time from a text stream takes the
        whole arrow key into Python's buffer, and ``select`` -- which
        watches the descriptor -- then reports an idle terminal and
        times out. The result is Escape, "[", "A": three keypresses
        where the user pressed once.
        """

        controller, terminal = _pty_stream()

        def press() -> None:
            os.write(controller, b"\x1b[B")
            os.write(controller, b"\r")

        reader = threading.Thread(target=press)
        reader.start()

        assert keys.read_key(terminal) == "down"
        assert keys.read_key(terminal) == "enter"

        reader.join()

        os.close(controller)

    @pytest.mark.skipif(
        not hasattr(os, "openpty"),
        reason="no pty on this platform",
    )
    def test_a_lone_escape_is_not_an_arrow_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        controller, terminal = _pty_stream()

        def press() -> None:
            os.write(controller, b"\x1b")
            time.sleep(keys.SEQUENCE_TIMEOUT * 4)
            os.write(controller, b"d")

        reader = threading.Thread(target=press)
        reader.start()

        assert keys.read_key(terminal) == "esc"
        assert keys.read_key(terminal) == "d"

        reader.join()

        os.close(controller)

    @pytest.mark.skipif(
        not hasattr(os, "openpty"),
        reason="no pty on this platform",
    )
    def test_end_of_input_is_not_an_exception(self) -> None:
        controller, terminal = _pty_stream()

        os.close(controller)

        assert keys.read_key(terminal) is None

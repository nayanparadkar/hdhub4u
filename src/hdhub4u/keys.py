"""
Single-keypress input, without a dependency.

Arrow keys need the terminal in raw mode: in cooked mode the Enter
key is what delivers a line, and nothing arrives until it is pressed.
`prompt_toolkit` and `questionary` do this well, and each one brings a
dependency tree with it -- for a program whose whole point is being a
single script someone can run with one `pip install`, that is a poor
trade.

What is actually needed is small: put the tty in raw mode, read one
character at a time, and put the terminal back exactly as it was. That
is `termios` and `sys.stdin`, both in the standard library. `termios` is
POSIX, so it is imported defensively and :func:`can_read_keys` reports
False wherever it is missing rather than the import failing.

The parsing is split from the reading on purpose. :func:`decode` maps a
string of raw characters to a key name and touches nothing else, so the
part that is easy to get wrong -- telling a lone Escape from the start
of an arrow key, and an application-mode sequence from a normal one --
is testable without a pty.
"""

from __future__ import annotations

import contextlib
import os
import select
import sys
from types import ModuleType
from typing import IO, Iterator


def _posix_modules() -> tuple[ModuleType, ModuleType] | None:
    """
    Return ``termios`` and ``tty``, or None off POSIX.

    Imported per call rather than bound at module scope, so that a
    Windows build gets a None here instead of a module-level name that
    is sometimes a module and sometimes None -- a shape that hides its
    own type from anything reading this. Both are already in
    ``sys.modules`` after the first call, so this is a dict lookup.
    """

    try:
        import termios
        import tty

    except ImportError:
        return None

    return termios, tty


#: Raw bytes for the keys that do not spell themselves.
ESC = "\x1b"
ENTER = "\r"
NEWLINE = "\n"
TAB = "\t"
BACKSPACE = "\x7f"
CTRL_C = "\x03"
CTRL_D = "\x04"

#: Escape sequences, as the terminal actually sends them. Both the
#: normal ``[A`` and the application-mode ``OA`` forms are here because
#: the same terminal sends different ones depending on what the program
#: last told it to do, and nothing here sets that mode deliberately.
SEQUENCES = {
    "[A": "up",
    "[B": "down",
    "[C": "right",
    "[D": "left",
    "OA": "up",
    "OB": "down",
    "OC": "right",
    "OD": "left",
    "[H": "home",
    "[F": "end",
    "OH": "home",
    "OF": "end",
    "[1~": "home",
    "[4~": "end",
    "[3~": "delete",
    "[5~": "pageup",
    "[6~": "pagedown",
    "[Z": "shifttab",
}

#: Characters that may continue an escape sequence. Reading stops at
#: anything else, so a sequence never swallows the key after it.
SEQUENCE_CHARS = frozenset("[O~0123456789;")

#: How long to wait for the rest of an escape sequence, in seconds. An
#: Escape the user pressed alone has nothing after it, and the only way
#: to tell those apart is to see whether more turns up.
#:
#: The cost of that timeout is that an Escape followed by another key
#: within it reads as that key instead: a byte-identical ``\x1bd`` is
#: what Alt+d sends too. Half a fiftieth of a second is below human
#: reaction time, so it only shows up in a test that writes both bytes
#: at once -- which is exactly the shape of test that was wrong about
#: this module.
SEQUENCE_TIMEOUT = 0.05

#: Single characters that get their own name rather than being
#: returned as themselves.
NAMED = {
    ESC: "esc",
    ENTER: "enter",
    NEWLINE: "enter",
    TAB: "tab",
    BACKSPACE: "backspace",
    CTRL_C: "ctrl-c",
    CTRL_D: "ctrl-d",
}


def can_read_keys(
    stream: IO[str] | None = None,
) -> bool:
    """
    Return True when this terminal can deliver one key at a time.

    False for a pipe, a file, a Windows build, or a stdin that has been
    closed -- every case where :func:`read_key` would either raise or
    block. Callers are expected to ask first and fall back to typing.

    Args:
        stream: The stream to test. Defaults to stdin.

    Returns:
        Whether raw single-key input is available.
    """

    modules = _posix_modules()

    if modules is None:
        return False

    target = sys.stdin if stream is None else stream

    try:
        return bool(target.isatty() and target.fileno() >= 0)

    except (AttributeError, ValueError, OSError):
        return False


@contextlib.contextmanager
def raw_mode(
    stream: IO[str] | None = None,
) -> Iterator[bool]:
    """
    Put a terminal into raw mode for the length of the block.

    Echo and line buffering are both turned off, which is the point:
    with echo on, every arrow press would also print a ``^[[A``.

    The original settings are restored in a ``finally``, because an
    interrupt mid-menu must not leave a shell with no echo. A terminal
    that refuses is reported as False rather than raised, so the caller
    can fall back rather than crash on an exotic tty.

    Args:
        stream: The terminal to change. Defaults to stdin.

    Yields:
        Whether raw mode was actually entered.
    """

    target = sys.stdin if stream is None else stream

    modules = _posix_modules()

    if modules is None or not can_read_keys(target):
        yield False
        return

    termios, tty = modules

    try:
        fd = target.fileno()

        saved = termios.tcgetattr(fd)

    except (termios.error, ValueError, OSError):
        yield False
        return

    try:
        tty.setraw(fd, termios.TCSANOW)

        yield True

    finally:
        with contextlib.suppress(
            termios.error,
            ValueError,
            OSError,
        ):
            termios.tcsetattr(
                fd,
                termios.TCSADRAIN,
                saved,
            )


def _is_sequence_char(char: str) -> bool:
    """Return True while a character can continue an escape sequence."""

    return char in SEQUENCE_CHARS


def decode(chars: str) -> str | None:
    """
    Turn raw characters from a terminal into a key name.

    The whole of the parsing, and none of the reading. What makes it
    worth separating: a lone Escape and the first byte of an arrow key
    are the same byte, and the only thing that tells them apart is what
    follows.

    Args:
        chars: One or more characters as the terminal sent them.

    Returns:
        A key name such as ``up`` or ``enter``, a single printable
        character as itself, or None when there was nothing to read.
    """

    if not chars:
        return None

    if chars[0] != ESC:
        return NAMED.get(chars[0], chars[0])

    rest = chars[1:]

    if not rest:
        return "esc"

    # A lone Escape followed by a real key, as Alt+key arrives.
    if rest[0] not in SEQUENCE_CHARS:
        return NAMED.get(rest[0], rest[0])

    # Longest match wins: "[1~" must not be read as "[1".
    for size in (3, 2):
        candidate = rest[:size]

        if candidate in SEQUENCES:
            return SEQUENCES[candidate]

    return "unknown"


def read_key(
    stream: IO[str] | None = None,
) -> str | None:
    """
    Read one keypress and return its name.

    Reads from the file descriptor rather than from the stream, and
    that is not a detail. ``sys.stdin.read(1)`` on a text stream pulls
    a whole block off the terminal into Python's buffer and hands back
    one character; the rest of that arrow key then sits in the buffer,
    where ``select`` -- which watches the descriptor -- reports nothing
    to read and waits out its timeout. The Escape comes back as a lone
    Escape and the next two calls return ``[`` and ``A`` separately,
    which on a real terminal is three keypresses where there was one.

    Args:
        stream: The terminal to read. Defaults to stdin.

    Returns:
        A key name, a printable character as itself, or None when
        input ended -- which is how a closed stdin unwinds the flow
        instead of raising at the user.
    """

    target = sys.stdin if stream is None else stream

    with raw_mode(target) as entered:
        if not entered:
            return None

        fd = target.fileno()

        first = _read_byte(fd)

        if first is None:
            return None

        if first != ESC:
            return decode(first)

        rest = ""

        while len(rest) < 3:
            more = _read_optional(fd)

            if more is None:
                break

            rest += more

            if not _is_sequence_char(more):
                break

        return decode(first + rest)


def _read_byte(fd: int) -> str | None:
    """
    Read exactly one character from a descriptor.

    Returns None at end of input, which is how a closed terminal is
    reported rather than raising EOFError at the user.
    """

    try:
        raw = os.read(fd, 1)

    except (BlockingIOError, InterruptedError):
        # Interrupted by a signal: whoever called us will come back.
        return ""

    except OSError:
        return None

    if not raw:
        return None

    return raw.decode("utf-8", errors="replace")


def _read_optional(
    fd: int,
    timeout: float = SEQUENCE_TIMEOUT,
) -> str | None:
    """
    Read one character from a descriptor, or None if it stays quiet.

    This is what separates a lone Escape from the start of an arrow
    key: both begin with the same byte, and only the arrival of more
    input says which one was meant.
    """

    try:
        ready, _, _ = select.select(
            [fd],
            [],
            [],
            timeout,
        )

    except (OSError, ValueError):
        return None

    if not ready:
        return None

    return _read_byte(fd)

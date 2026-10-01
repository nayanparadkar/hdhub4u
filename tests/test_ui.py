"""Tests for the terminal rendering helpers."""

from __future__ import annotations

import shutil
import subprocess

from hdhub4u.ui import (
    copy_to_clipboard,
    format_type,
    print_error,
    print_results,
    shorten_title,
    terminal_width,
)


def test_shorten_title_keeps_short_text() -> None:
    assert shorten_title("Big Boss") == "Big Boss"


def test_shorten_title_truncates_long_text() -> None:
    result = shorten_title(
        "A" * 80,
        max_length=20,
    )

    assert result.endswith("...")
    assert len(result) <= 20


def test_shorten_title_collapses_whitespace() -> None:
    assert (
        shorten_title("a   b\n c")
        == "a b c"
    )


def test_format_type_known_label() -> None:
    assert "Movie" in format_type("movie")
    assert "Web Series" in format_type("webseries")


def test_format_type_unknown_label_is_untitled() -> None:
    assert "Trailer" in format_type("trailer")


def test_print_results_handles_empty(capsys) -> None:
    print_results([])

    assert "No relevant results" in capsys.readouterr().out


def test_print_results_lists_items(capsys) -> None:
    print_results(
        [
            {
                "title": "Big Boss Full Series",
                "url": "https://site.test/a",
                "type": "webseries",
            },
        ]
    )

    output = capsys.readouterr().out

    assert "Big Boss" in output


def test_print_error_shows_message(capsys) -> None:
    print_error("something failed")

    assert "something failed" in capsys.readouterr().out


def test_terminal_width_is_positive() -> None:
    assert terminal_width() > 0


def test_copy_to_clipboard_without_utility(
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

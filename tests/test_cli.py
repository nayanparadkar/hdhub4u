"""Tests for the command-line entry point."""

from __future__ import annotations

from pathlib import Path

import pytest

from hdhub4u.__main__ import (
    build_parser,
    main,
    run_links,
    run_search,
)
from hdhub4u.database import add_media_batch, initialize_database
from hdhub4u.link_cache import LinkCache


def _seed() -> None:
    initialize_database()

    add_media_batch(
        [
            (
                "Big Boss Full Series",
                "https://site.test/big-boss/",
                "webseries",
                "",
            ),
            (
                "Batman & Robin Full Movie",
                "https://site.test/batman-robin/",
                "movie",
                "",
            ),
        ]
    )


def test_parser_defaults_to_interactive() -> None:
    args = build_parser().parse_args([])

    assert args.command is None


def test_parser_reads_search_query() -> None:
    args = build_parser().parse_args(
        ["search", "boss", "--limit", "5"]
    )

    assert args.command == "search"
    assert args.query == "boss"
    assert args.limit == 5


def test_parser_rejects_unknown_command() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["nope"])


def test_search_prints_matches(
    isolated_home: Path,
    capsys,
) -> None:
    _seed()

    code = run_search("boss", 10)
    output = capsys.readouterr().out

    assert code == 0
    assert "Big Boss" in output
    assert "https://site.test/" in output


def test_search_without_matches(
    isolated_home: Path,
    capsys,
) -> None:
    initialize_database()

    code = run_search("zzzzz", 10)
    output = capsys.readouterr().out

    assert code == 1
    assert "No matches" in output


def test_status_command(
    isolated_home: Path,
    capsys,
) -> None:
    _seed()

    assert main(["status"]) == 0

    output = capsys.readouterr().out

    assert "Indexed items" in output
    assert "2" in output


def test_links_prune_command(
    isolated_home: Path,
    capsys,
) -> None:
    assert main(["links", "prune"]) == 0

    output = capsys.readouterr().out

    assert "Removed 0" in output


def test_links_clear_command(
    isolated_home: Path,
    capsys,
) -> None:
    cache = LinkCache(
        isolated_home / "data" / "links.json"
    )

    cache.put(
        "https://gate.test/a",
        "https://cdn.test/f.mp4",
        "video/mp4",
        "direct",
    )

    assert run_links("clear") == 0

    assert "cleared" in capsys.readouterr().out
    assert not cache.path.exists()

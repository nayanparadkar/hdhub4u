"""
Tests for hdhub4u.__main__.

The entry point is what a shell actually runs, and it was the one
module with no tests: an argparse conflict that made
``hdhub4u --browser index`` unrunnable, and an escaping exception
that printed a traceback over a message written to be read, both
shipped unnoticed.
"""

from __future__ import annotations

import json

import pytest

from hdhub4u import __main__ as entry
from hdhub4u import ui
from hdhub4u.errors import HdHubError, NetworkError


class TestParser:
    def test_no_command_opens_the_live_flow(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            entry, "live_interactive", lambda *a, **k: 0
        )

        assert entry.main([]) == 0

    def test_search_reaches_the_live_catalog(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: dict[str, object] = {}

        def fake(query, **kwargs):
            seen["query"] = query
            return 0

        monkeypatch.setattr(entry, "live_search", fake)

        assert entry.main(["search", "dune"]) == 0
        assert seen["query"] == "dune"

    def test_search_local_uses_the_offline_index(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: dict[str, object] = {}

        def fake(query, limit=20, as_json=False):
            seen["query"] = query
            seen["as_json"] = as_json
            return 0

        monkeypatch.setattr(entry, "run_search", fake)

        assert (
            entry.main(["search", "dune", "--local"]) == 0
        )
        assert seen["query"] == "dune"

    def test_browse_starts_with_a_query(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: dict[str, str] = {}

        def fake(query=""):
            seen["query"] = query
            return 0

        monkeypatch.setattr(entry, "live_interactive", fake)

        assert entry.main(["browse", "dune"]) == 0
        assert seen["query"] == "dune"

    def test_index_takes_its_own_browser_flag(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The flag used to be defined on both the root parser and the
        ``index`` subparser, which is an argparse conflict, and the
        root one made ``hdhub4u --browser index`` unrunnable with
        "unrecognized arguments".
        """

        seen: dict[str, object] = {}

        monkeypatch.setattr(
            entry,
            "build_index",
            lambda **kwargs: seen.update(kwargs),
        )

        assert entry.main(["index", "--browser"]) == 0
        assert seen["use_browser"] is True

    def test_index_defaults_to_no_browser(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: dict[str, object] = {}

        monkeypatch.setattr(
            entry,
            "build_index",
            lambda **kwargs: seen.update(kwargs),
        )

        assert entry.main(["index"]) == 0
        assert seen["use_browser"] is False

    def test_the_browser_flag_lives_on_one_parser(self) -> None:
        """
        Declared once, on the subcommand that acts on it. Defining it
        on the root parser as well was an argparse conflict, and it
        left the root one understood by no code path.
        """

        parser = entry.build_parser()

        actions = [
            action
            for action in parser._actions
            if "--browser" in getattr(
                action, "option_strings", []
            )
        ]

        assert actions == []

    def test_status_runs(self) -> None:
        assert entry.main(["status"]) == 0

    def test_two_bare_words_are_rejected(self) -> None:
        """
        One bare word is a query; two are an unquoted query that was
        never quoted. Guessing that an unknown word was meant to be
        the start of one would swallow a typo'd subcommand and search
        for it instead of reporting the mistake.
        """

        with pytest.raises(SystemExit):
            entry.main(["the", "big", "boss"])

    def test_one_unknown_word_is_a_query(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The flip side of the rule above, and the reason the rule stops
        at one word: a search nobody typed in full is still a search.
        """

        seen: dict[str, str] = {}

        monkeypatch.setattr(
            entry,
            "live_interactive",
            lambda query="": seen.update(q=query) or 0,
        )

        assert entry.main(["nonsense"]) == 0
        assert seen["q"] == "nonsense"


class TestBareQuery:
    """
    A bare word is the search, because searching is what the program
    is for and a subcommand in front of it is ceremony.
    """

    def test_a_bare_word_starts_the_interactive_flow(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: dict[str, str] = {}

        monkeypatch.setattr(
            entry,
            "live_interactive",
            lambda query="": seen.update(q=query) or 0,
        )

        assert entry.main(["dune"]) == 0
        assert seen["q"] == "dune"

    def test_a_bare_word_is_the_same_as_browse(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from_bare: dict[str, str] = {}
        from_browse: dict[str, str] = {}

        monkeypatch.setattr(
            entry,
            "live_interactive",
            lambda query="": from_bare.update(q=query) or 0,
        )

        entry.main(["dune"])

        monkeypatch.setattr(
            entry,
            "live_interactive",
            lambda query="": from_browse.update(q=query) or 0,
        )

        entry.main(["browse", "dune"])

        assert from_bare == from_browse

    def test_a_bare_word_past_a_global_flag_is_a_query(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        ``--log-level`` takes a value, and that value is a word. Read
        as the query instead, ``hdhub4u --log-level DEBUG dune``
        searches for "DEBUG".
        """

        seen: dict[str, str] = {}

        monkeypatch.setattr(
            entry,
            "live_interactive",
            lambda query="": seen.update(q=query) or 0,
        )

        entry.main(["--log-level", "DEBUG", "dune"])

        assert seen["q"] == "dune"

    def test_a_bare_word_past_a_valueless_flag_is_a_query(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: dict[str, str] = {}

        monkeypatch.setattr(
            entry,
            "live_interactive",
            lambda query="": seen.update(q=query) or 0,
        )

        entry.main(["--log-level", "DEBUG", "dune"])

        assert seen["q"] == "dune"

    def test_no_arguments_at_all_still_opens_the_flow(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: dict[str, object] = {}

        monkeypatch.setattr(
            entry,
            "live_interactive",
            lambda query="": seen.update(q=query) or 0,
        )

        assert entry.main([]) == 0
        assert seen["q"] == ""

    def test_a_command_name_is_never_read_as_a_query(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A word that names a command is left for argparse, so a real
        subcommand cannot be swallowed by the bare-query shortcut.
        """

        monkeypatch.setattr(entry, "run_status", lambda: 0)

        assert entry.main(["status"]) == 0

    def test_a_hidden_command_is_never_read_as_a_query(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: dict[str, object] = {}

        monkeypatch.setattr(
            entry,
            "run_links",
            lambda action: seen.update(a=action) or 0,
        )

        assert entry.main(["links", "clear"]) == 0
        assert seen["a"] == "clear"


class TestNormaliseArgv:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ([], []),
            (["status"], ["status"]),
            (["search", "dune"], ["search", "dune"]),
            (["dune"], ["browse", "dune"]),
            (["--log-level", "DEBUG"], ["--log-level", "DEBUG"]),
            (
                ["--log-level", "DEBUG", "dune"],
                ["browse", "dune", "--log-level", "DEBUG"],
            ),
            (["-x", "dune"], ["browse", "dune", "-x"]),
        ],
    )
    def test_shapes(
        self,
        given: list[str],
        expected: list[str],
    ) -> None:
        assert entry.normalise_argv(given) == expected

    def test_flags_keep_the_order_they_were_typed_in(
        self,
    ) -> None:
        """
        The query moves to the front so argparse sees a command name;
        the flags around it are not reordered, so nothing is
        duplicated and nothing is silently dropped.
        """

        assert entry.normalise_argv(
            ["--log-level", "DEBUG", "dune"]
        ) == ["browse", "dune", "--log-level", "DEBUG"]

    def test_several_bare_words_are_refused_with_advice(
        self,
        capsys,
    ) -> None:
        """
        A title with spaces lost its quotes. Saying so is the useful
        answer; joining the words would instead make any typo'd
        subcommand a search for the typo.
        """

        with pytest.raises(SystemExit):
            entry.normalise_argv(["the", "big", "boss"])

        message = capsys.readouterr().err

        assert "quotes" in message
        assert "the big boss" in message


class TestHiddenCommands:
    def _listed_commands(self) -> list[str]:
        """
        Return the subcommands ``--help`` offers.

        Read out of the listing rather than searched for as substrings
        of the whole help, because "index" is also an English word
        that appears in the description of ``status``.
        """

        lines = entry.build_parser().format_help().splitlines()

        start = lines.index("positional arguments:") + 1

        block = []
        for line in lines[start:]:
            if line and not line.startswith(" "):
                break
            block.append(line)

        return [
            line.split()[0]
            for line in block
            if line.strip() and not line.startswith("{")
        ]

    @pytest.mark.parametrize(
        "command",
        ["index", "render", "links"],
    )
    def test_a_maintenance_command_is_not_advertised(
        self,
        command: str,
    ) -> None:
        """
        They still work, they are just not in the front door. Six
        commands of which three exist to maintain a cache or build an
        index is a wall of choices where there should be one.
        """

        assert command not in self._listed_commands()

    @pytest.mark.parametrize(
        "command",
        ["search", "browse", "status"],
    )
    def test_a_download_command_is_advertised(
        self,
        command: str,
    ) -> None:
        assert command in self._listed_commands()

    @pytest.mark.parametrize(
        "command",
        ["index", "render", "links"],
    )
    def test_a_maintenance_command_still_parses(
        self,
        command: str,
    ) -> None:
        required = {
            "index": [],
            "render": ["https://site.test/a"],
            "links": ["prune"],
        }[command]

        args = entry.build_parser().parse_args(
            [command, *required]
        )

        assert args.command == command


class TestLogLevel:
    """
    The option is declared twice, on the root parser and on every
    subcommand, so it works on either side of the command name.
    """

    @pytest.mark.parametrize(
        "argv",
        [
            ["--log-level", "DEBUG", "status"],
            ["status", "--log-level", "DEBUG"],
        ],
    )
    def test_it_is_accepted_on_either_side(
        self,
        argv: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: dict[str, object] = {}

        monkeypatch.setattr(
            entry,
            "configure_logging",
            lambda level: seen.update(level=level),
        )
        monkeypatch.setattr(
            entry,
            "run_status",
            lambda: 0,
        )

        entry.main(argv)

        assert seen["level"] == "DEBUG"

    def test_it_is_accepted_on_every_command(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: dict[str, object] = {}

        monkeypatch.setattr(
            entry,
            "configure_logging",
            lambda level: seen.update(level=level),
        )
        monkeypatch.setattr(
            entry,
            "build_index",
            lambda **kwargs: None,
        )
        monkeypatch.setattr(
            entry,
            "run_links",
            lambda action: 0,
        )

        entry.main(["index", "--log-level", "DEBUG"])
        entry.main(["links", "prune", "--log-level", "DEBUG"])

        assert seen["level"] == "DEBUG"

    def test_a_level_before_the_command_is_not_overwritten(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A subparser writes its own defaults into the shared namespace
        as it parses. With an ordinary default of None the subcommand
        would wipe a level given before it, and the flag would
        silently do nothing in the position people actually type it.
        """

        seen: dict[str, object] = {}

        monkeypatch.setattr(
            entry,
            "configure_logging",
            lambda level: seen.update(level=level),
        )
        monkeypatch.setattr(
            entry,
            "run_status",
            lambda: 0,
        )

        entry.main(["--log-level", "DEBUG", "status"])

        assert seen["level"] == "DEBUG"

    def test_no_level_at_all_is_left_to_the_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        None means "fall back to HDHUB_LOG_LEVEL, then WARNING", so
        the flag must not invent a value of its own.
        """

        seen: dict[str, object] = {}

        monkeypatch.setattr(
            entry,
            "configure_logging",
            lambda level: seen.update(level=level),
        )
        monkeypatch.setattr(
            entry,
            "run_status",
            lambda: 0,
        )

        entry.main(["status"])

        assert seen["level"] is None


class TestLocalSearchOutput:
    def test_a_pipe_gets_plain_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """
        The live and the local paths are the same command, so they
        print the same shape. The local path used to be the only one
        producing plain text, which meant the two disagreed.
        """

        monkeypatch.setattr(
            ui,
            "wants_table",
            lambda: False,
        )
        monkeypatch.setattr(
            entry,
            "search_media",
            lambda query, limit=20: [
                {
                    "title": "Big Boss Full Series",
                    "url": "https://site.test/a",
                    "type": "webseries",
                },
            ],
        )

        assert entry.run_search("big boss", 10) == 0

        printed = capsys.readouterr().out

        assert "┏" not in printed
        assert "https://site.test/a" in printed

    def test_json_carries_every_field(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(
            ui,
            "wants_table",
            lambda: True,
        )
        monkeypatch.setattr(
            entry,
            "search_media",
            lambda query, limit=20: [
                {
                    "title": (
                        "Dune (2024) 1080p WEB-DL"
                    ),
                    "url": "https://site.test/a",
                    "type": "movie",
                },
            ],
        )

        assert entry.run_search("dune", 10, as_json=True) == 0

        payload = json.loads(capsys.readouterr().out)
        first = payload["results"][0]

        assert first["type"] == "movie"
        assert first["quality"] == "1080P"
        assert first["year"] == "2024"

    def test_no_matches_exits_nonzero(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            entry,
            "search_media",
            lambda query, limit=20: [],
        )

        assert entry.run_search("nothing", 10) == 1

    def test_the_json_flag_is_forwarded(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: dict[str, object] = {}

        monkeypatch.setattr(
            entry,
            "run_search",
            lambda query, limit, as_json=False: seen.update(
                json=as_json,
            )
            or 0,
        )

        entry.main(["search", "dune", "--local", "--json"])

        assert seen["json"] is True

    def test_the_json_flag_is_forwarded_to_the_live_search(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: dict[str, object] = {}

        def fake(query, **kwargs):
            seen.update(kwargs)

            return 0

        monkeypatch.setattr(
            entry,
            "live_search",
            fake,
        )

        entry.main(["search", "dune", "--json"])

        assert seen["as_json"] is True


class TestRun:
    def test_an_expected_error_is_printed_not_traced(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """
        Every deliberate failure in this program carries a message
        written for a person. Letting one escape printed a Python
        traceback over it, which is the least useful way to say the
        site was unreachable.
        """

        def failing() -> int:
            raise NetworkError("the site did not answer")

        monkeypatch.setattr(entry, "main", failing)

        assert entry.run() == 1
        assert "the site did not answer" in capsys.readouterr().out

    def test_any_project_error_is_caught(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def failing() -> int:
            raise HdHubError("something went wrong")

        monkeypatch.setattr(entry, "main", failing)

        assert entry.run() == 1

    def test_an_interrupt_exits_by_convention(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def failing() -> int:
            raise KeyboardInterrupt

        monkeypatch.setattr(entry, "main", failing)

        assert entry.run() == 130

    def test_a_success_is_passed_through(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(entry, "main", lambda: 0)

        assert entry.run() == 0

    def test_a_failure_code_is_passed_through(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(entry, "main", lambda: 1)

        assert entry.run() == 1

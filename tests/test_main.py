"""
Tests for hdhub4u.__main__.

The entry point is what a shell actually runs, and it was the one
module with no tests: an argparse conflict that made
``hdhub4u --browser index`` unrunnable, and an escaping exception
that printed a traceback over a message written to be read, both
shipped unnoticed.
"""

from __future__ import annotations

import pytest

from hdhub4u import __main__ as entry
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

        def fake(query, limit=20):
            seen["query"] = query
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

    def test_an_unknown_command_is_rejected(self) -> None:
        with pytest.raises(SystemExit):
            entry.main(["nonsense"])


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

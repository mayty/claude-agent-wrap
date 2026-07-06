# This file has been created with the assistance of an AI tool.
"""Tests for bash completion — unused_flags() and each verb's complete()."""

from __future__ import annotations

import argparse
import sys

import pytest

from agent_wrap.__main__ import _complete
from agent_wrap.cli.commands import COMMANDS
from agent_wrap.cli.create.complete import complete as create_complete
from agent_wrap.cli.logs.complete import complete as logs_complete
from agent_wrap.cli.rebuild.complete import complete as rebuild_complete
from agent_wrap.cli.run.complete import complete as run_complete
from agent_wrap.cli.stats.complete import complete as stats_complete
from agent_wrap.cli.update.complete import complete as update_complete
from agent_wrap.constants import TELEGRAM_SIDECAR_NAME
from agent_wrap.containers import services
from agent_wrap.lib.argparsing import unused_flags

# ---------------------------------------------------------------------------
# unused_flags tests
# ---------------------------------------------------------------------------


@pytest.fixture
def parser() -> argparse.ArgumentParser:
    """Build a test parser with known flags for unused_flags testing."""
    p = argparse.ArgumentParser()
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-f", "--from", dest="from_date")
    p.add_argument("-u", "--until", dest="until_date")
    p.add_argument("-d", "--days", type=int)
    p.add_argument("--hidden", help=argparse.SUPPRESS)
    p.add_argument("positional")
    return p


class TestUnusedFlags:
    def test_all_flags_on_bare_tab(self, parser: argparse.ArgumentParser) -> None:
        """All non-hidden flags returned when nothing consumed."""
        result = unused_flags(parser, ["agent", "test", ""], 2)
        assert "-v" in result
        assert "--verbose" in result
        assert "-f" in result
        assert "--from" in result
        assert "-u" in result
        assert "--until" in result
        assert "-d" in result
        assert "--days" in result
        assert "-h" in result
        assert "--help" in result
        assert "--hidden" not in result

    def test_consumed_flag_excluded(self, parser: argparse.ArgumentParser) -> None:
        """A used flag is removed from candidates."""
        result = unused_flags(parser, ["agent", "test", "--verbose", ""], 3)
        assert "-v" not in result
        assert "--verbose" not in result
        assert "-f" in result  # still available

    def test_shorthand_excludes_long_form(self, parser: argparse.ArgumentParser) -> None:
        """Using -v excludes --verbose (same action)."""
        result = unused_flags(parser, ["agent", "test", "-v", ""], 3)
        assert "-v" not in result
        assert "--verbose" not in result

    def test_long_form_excludes_shorthand(self, parser: argparse.ArgumentParser) -> None:
        """Using --from excludes -f (same action)."""
        result = unused_flags(parser, ["agent", "test", "--from", "x", ""], 4)
        assert "-f" not in result
        assert "--from" not in result

    def test_value_flag_prev_returns_empty(self, parser: argparse.ArgumentParser) -> None:
        """Previous word is a value-accepting flag → no flags returned."""
        result = unused_flags(parser, ["agent", "test", "-d", ""], 3)
        assert result == []

    def test_value_flag_with_value_then_tab_returns_unused(
        self, parser: argparse.ArgumentParser
    ) -> None:
        """After the value is typed, unused flags are returned."""
        result = unused_flags(parser, ["agent", "test", "-d", "14", ""], 4)
        assert "-d" not in result
        assert "--days" not in result
        assert "-v" in result

    def test_equals_split_word_consumed(self, parser: argparse.ArgumentParser) -> None:
        """--from= (split by COMP_WORDBREAKS) still consumes --from/-f."""
        result = unused_flags(parser, ["agent", "test", "--from=", "-14d", ""], 4)
        assert "-f" not in result
        assert "--from" not in result

    def test_store_true_flag_not_value_accepting(self, parser: argparse.ArgumentParser) -> None:
        """Previous word is --verbose (store_true) → flags returned."""
        result = unused_flags(parser, ["agent", "test", "--verbose", ""], 3)
        assert result != []

    def test_prev_not_a_flag_returns_all_unused(self, parser: argparse.ArgumentParser) -> None:
        """Previous word is positional value → all unused flags returned."""
        result = unused_flags(parser, ["agent", "test", "foo", ""], 3)
        assert "-v" in result


# ---------------------------------------------------------------------------
# Per-verb complete() tests
# ---------------------------------------------------------------------------


class TestCreateComplete:
    def test_no_completions(self) -> None:
        assert create_complete(2, ["agent", "create", ""]) == []


class TestUpdateComplete:
    def test_no_completions(self) -> None:
        assert update_complete(2, ["agent", "update", ""]) == []


class TestRebuildComplete:
    def test_bare_tab_shows_flags(self) -> None:
        result = rebuild_complete(2, ["agent", "rebuild", ""])
        assert "--full" in result
        assert "-h" in result

    def test_flag_partial(self) -> None:
        result = rebuild_complete(2, ["agent", "rebuild", "--f"])
        assert "--full" in result
        # compgen filters, but we return all candidates

    def test_flag_consumed(self) -> None:
        result = rebuild_complete(3, ["agent", "rebuild", "--full", ""])
        assert "--full" not in result


class TestLogsComplete:
    def test_bare_tab_shows_flags(self) -> None:
        result = logs_complete(2, ["agent", "logs", ""])
        assert "--port" in result
        assert "--stop" in result
        assert "--foreground" not in result  # hidden

    def test_port_consumed(self) -> None:
        result = logs_complete(3, ["agent", "logs", "--stop", ""])
        assert "--stop" not in result
        assert "--port" in result

    def test_port_value_position_returns_empty(self) -> None:
        """--port takes a value; tabbing right after shows nothing."""
        result = logs_complete(3, ["agent", "logs", "--port", ""])
        assert result == []

    def test_after_port_value(self) -> None:
        result = logs_complete(4, ["agent", "logs", "--port", "8765", ""])
        assert "--stop" in result


class TestRunComplete:
    def test_bare_tab_shows_flags(self) -> None:
        result = run_complete(2, ["agent", "run", ""])
        assert "--base" in result

    def test_flag_consumed(self) -> None:
        result = run_complete(3, ["agent", "run", "--base", ""])
        assert "--base" not in result
        # all flags consumed → [] → bash file completion

    def test_all_flags_exhausted(self) -> None:
        """When all flags are used, returns [] for file passthrough."""
        result = run_complete(4, ["agent", "run", "--base", "--help", ""])
        assert result == []


class TestStatsComplete:
    def test_bare_tab_shows_all_flags(self) -> None:
        result = stats_complete(2, ["agent", "stats", ""])
        assert "-v" in result
        assert "--verbose" in result
        assert "-f" in result
        assert "--from" in result

    def test_verbose_consumed(self) -> None:
        result = stats_complete(3, ["agent", "stats", "-v", ""])
        assert "-v" not in result
        assert "--verbose" not in result
        assert "-f" in result  # still available

    def test_shorthand_excludes_long(self) -> None:
        result = stats_complete(3, ["agent", "stats", "-u", ""])
        assert "-u" not in result
        assert "--until" not in result

    def test_value_flag_prev_returns_empty(self) -> None:
        result = stats_complete(3, ["agent", "stats", "-d", ""])
        assert result == []

    def test_after_date_value(self) -> None:
        result = stats_complete(4, ["agent", "stats", "-d", "14", ""])
        assert "-v" in result


class TestSecretsComplete:
    def test_cword_2_shows_subcommands(self) -> None:
        result = _run_complete("secrets", 2, ["agent", "secrets", ""])
        assert "check" in result
        assert "set" in result
        assert "clear" in result
        assert "cleanup" in result

    def test_cword_3_cleanup_no_sidecar(self) -> None:
        result = _run_complete("secrets", 3, ["agent", "secrets", "cleanup", ""])
        assert result == []

    def test_cword_3_check_shows_sidecars(self, mocker) -> None:  # type: ignore[no-untyped-def]
        """Verify sidecar names include 'telegram' (always present)."""
        mocker.patch.object(
            services.secrets_service,
            "known_sidecars",
            return_value=["litellm-bedrock", TELEGRAM_SIDECAR_NAME],
        )
        result = _run_complete("secrets", 3, ["agent", "secrets", "check", ""])
        assert TELEGRAM_SIDECAR_NAME in result
        assert "litellm-bedrock" in result
        assert len(result) == 2


def _run_complete(verb: str, cword: int, words: list[str]) -> list[str]:
    """Call the registered complete() for *verb*."""
    _run_fn, complete_fn = COMMANDS[verb]
    return complete_fn(cword, words)


# ---------------------------------------------------------------------------
# Dispatcher tests (via __main__._complete)
# ---------------------------------------------------------------------------


class TestDispatcher:
    def test_verb_completion(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Verify cword=1 prints all COMMANDS keys (verb completion)."""
        old_argv = sys.argv
        try:
            sys.argv = ["agent_wrap", "1", "agent", ""]
            _complete()
        finally:
            sys.argv = old_argv

        out = capsys.readouterr().out
        for name in COMMANDS:
            assert name in out

    def test_unknown_verb_no_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Verify cword > 1 with unknown verb produces no output."""
        old_argv = sys.argv
        try:
            sys.argv = ["agent_wrap", "2", "agent", "no-such-verb", ""]
            _complete()
        finally:
            sys.argv = old_argv

        assert capsys.readouterr().out == ""

    def test_known_verb_delegates_to_complete(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Verify cword=2 with 'rebuild' delegates to rebuild's complete()."""
        old_argv = sys.argv
        try:
            sys.argv = ["agent_wrap", "2", "agent", "rebuild", ""]
            _complete()
        finally:
            sys.argv = old_argv

        out = capsys.readouterr().out
        assert "--full" in out


# ---------------------------------------------------------------------------
# Consistency / regression tests
# ---------------------------------------------------------------------------


class TestConsistency:
    def test_every_command_has_complete_function(self) -> None:
        """Every registered verb maps to a callable complete()."""
        for name, (_run_fn, complete_fn) in COMMANDS.items():
            assert callable(complete_fn), f"{name} complete() is not callable"

    def test_commands_dict_matches_registered_verbs(self) -> None:
        """COMMANDS keys match the set of known verbs."""
        expected = {"create", "logs", "rebuild", "run", "secrets", "stats", "update"}
        assert set(COMMANDS) == expected

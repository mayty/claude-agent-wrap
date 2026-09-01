# This file has been created with the assistance of an AI tool.
"""CLI-layer tests for agent_wrap.cli.update — completion."""

from agent_wrap.cli.update.complete import complete as update_complete


def test_update_complete_no_completions() -> None:
    assert update_complete(2, ["agent", "update", ""]) == []

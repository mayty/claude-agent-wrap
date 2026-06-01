# This file has been created with the assistance of an AI tool.
"""
Drift tests between agent-wrap.bashrc completion metadata and command modules.

USAGE strings on each command module are the source of truth: anyone adding a
flag must update its module's USAGE (because `agent` with no args prints it),
which then forces the bashrc case-branch to be updated too.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_wrap.__main__ import _discover_commands

_BASHRC = Path(__file__).resolve().parents[2] / "agent-wrap.bashrc"
_FLAG_RE = re.compile(r"--[a-z][a-z0-9-]*")


def _bashrc_text() -> str:
    return _BASHRC.read_text()


def _bashrc_flags_for(verb: str) -> set[str] | None:
    """
    Extract flags from the `case` branch for ``verb`` in agent-wrap.bashrc.

    Returns None if no branch matches the verb (covers explicit empty branches
    like `create|update) flags="" ;;` by returning an empty set, distinct from
    None which means "verb missing entirely").
    """
    text = _bashrc_text()
    pattern = re.compile(
        r"^\s*([A-Za-z0-9_|]+)\)\s*flags=\"([^\"]*)\"\s*;;",
        re.MULTILINE,
    )
    for match in pattern.finditer(text):
        verbs = match.group(1).split("|")
        if verb in verbs:
            return set(_FLAG_RE.findall(match.group(2)))
    return None


def test_bashrc_verbs_match_discovered_commands() -> None:
    """Every command module must have a case-branch entry in the bashrc."""
    discovered = {c.name for c in _discover_commands()}
    text = _bashrc_text()
    pattern = re.compile(r"^\s*([A-Za-z0-9_|]+)\)\s*flags=", re.MULTILINE)
    bashrc_verbs: set[str] = set()
    for match in pattern.finditer(text):
        bashrc_verbs.update(match.group(1).split("|"))

    missing = discovered - bashrc_verbs
    extra = bashrc_verbs - discovered
    assert not missing, (
        f"agent-wrap.bashrc is missing case-branch(es) for: {sorted(missing)}. "
        f'Add a `<verb>) flags="..." ;;` line to _agent_complete().'
    )
    assert not extra, (
        f"agent-wrap.bashrc has stale case-branch(es) for non-existent commands: "
        f"{sorted(extra)}. Remove them from _agent_complete()."
    )


def test_bashrc_flags_match_command_usage() -> None:
    """Per-verb flag lists in the bashrc must match each module's USAGE string."""
    mismatches: list[str] = []
    for cmd in _discover_commands():
        usage_flags = set(_FLAG_RE.findall(cmd.usage))
        bashrc_flags = _bashrc_flags_for(cmd.name)
        if bashrc_flags is None:
            mismatches.append(f"{cmd.name}: no case-branch in agent-wrap.bashrc")
            continue
        only_usage = usage_flags - bashrc_flags
        only_bashrc = bashrc_flags - usage_flags
        if only_usage:
            mismatches.append(f"{cmd.name}: flags in USAGE but not in bashrc: {sorted(only_usage)}")
        if only_bashrc:
            mismatches.append(
                f"{cmd.name}: flags in bashrc but not in USAGE: {sorted(only_bashrc)}"
            )
    assert not mismatches, "Bashrc/USAGE drift:\n  " + "\n  ".join(mismatches)

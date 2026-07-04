# This file has been created with the assistance of an AI tool.
"""
Shared argparse helpers for the agent-wrap subcommands.

`argparse` signals completion by raising ``SystemExit`` — code 0 for ``--help``
and code 2 for a parse error. The CLI dispatcher (``agent_wrap/__main__.py``)
expects each command's ``run()`` to *return* an int, and the project convention
is 0 for help / success and 1 for any parse error. ``parse_or_code`` bridges the
two so commands never have to re-derive an exit code from the raw argv.
"""

from __future__ import annotations

import argparse

# A parsed namespace, or an exit code to return verbatim from run().
ParseResult = argparse.Namespace | int


def make_parser(
    name: str,
    *,
    usage_summary: str = "",
    description: str | None = None,
    add_help: bool = True,
) -> argparse.ArgumentParser:
    """
    Build an ``ArgumentParser`` wired for the ``agent <name>`` convention.

    ``prog`` is set to ``agent <name>`` so generated usage/help reads naturally.
    ``allow_abbrev`` is disabled so callers can't rely on prefix matching (and so
    pass-through commands don't swallow the inner tool's flags). ``description``
    is rendered verbatim via ``RawDescriptionHelpFormatter`` to preserve the
    hand-written multi-line help blocks.
    """
    return argparse.ArgumentParser(
        prog=f"agent {name}",
        usage=f"agent {name} {usage_summary}".rstrip(),
        description=description,
        add_help=add_help,
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def parse_or_code(parser: argparse.ArgumentParser, argv: list[str]) -> ParseResult:
    """
    Parse ``argv``; return the populated namespace, or an int exit code.

    Returns 0 when ``--help`` was printed (or a no-error exit), and 1 on any
    parse error — collapsing argparse's 0/2 convention into the project's 0/1.
    Callers should ``if isinstance(result, int): return result`` before use.
    """
    try:
        return parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 1


# COMP_WORDS index of the first word after the verb (verb at index 1).
_FIRST_ARG_INDEX = 2


def _takes_value(action: argparse.Action) -> bool:
    """Return whether *action* consumes a value argument (not store_true/false/count/const)."""
    return action.nargs != 0


def unused_flags(parser: argparse.ArgumentParser, words: list[str], cword: int) -> list[str]:
    """
    Return flags from *parser* that aren't already on the command line.

    - Respects shorthand groups: ``-u`` present → ``--until`` excluded.
    - Strips trailing ``=`` from words (COMP_WORDBREAKS splits ``--from=val``).
    - Returns [] when *prev* is a value-accepting flag.
    """
    # option_string → action
    opt_to_action: dict[str, argparse.Action] = {}
    for action in parser._actions:  # noqa: SLF001
        for opt in action.option_strings:
            opt_to_action[opt] = action

    # Prev word is a value-accepting flag → user is typing the value
    prev = words[cword - 1] if cword > _FIRST_ARG_INDEX else ""
    prev_action = opt_to_action.get(prev)
    if prev_action is not None and _takes_value(prev_action):
        return []

    # Actions whose option_strings already appear on the command line
    consumed: set[int] = set()
    for i in range(_FIRST_ARG_INDEX, cword):
        w = words[i]
        w = w.removesuffix("=")  # --from= → --from
        action = opt_to_action.get(w)
        if action is not None:
            consumed.add(id(action))

    # Return option_strings from non-consumed, non-hidden actions
    flags: list[str] = []
    seen: set[int] = set()
    for action in parser._actions:  # noqa: SLF001
        if action.help is argparse.SUPPRESS:
            continue
        aid = id(action)
        if aid in seen:
            continue
        seen.add(aid)
        if aid not in consumed:
            flags.extend(action.option_strings)
    return flags

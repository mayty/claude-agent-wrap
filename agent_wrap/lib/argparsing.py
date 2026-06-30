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

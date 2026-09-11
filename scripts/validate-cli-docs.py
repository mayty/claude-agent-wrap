# This file has been created with the assistance of an AI tool.
#
# Validates docs/shell-commands.md against the click command tree.
#
# The doc is the hand-written CLI reference and CLAUDE.md routes "adding/editing an
# `agent` verb or its flags" to it, so the two drift silently. This asserts the
# mechanical half of that obligation -- that every verb and every flag is covered --
# and leaves the prose alone. Nothing here compares the doc's wording to click's help
# text: the doc is deliberately richer, and the verb table's `Purpose` column is
# deliberately better than the docstring summaries.
#
# Rules enforced:
#   CD001 - every registered verb has exactly one `## \`agent <verb>\`` section, and
#           every such section is a registered verb
#   CD002 - every registered verb has a row in the intro verb table
#   CD003 - a non-group verb's synopsis block equals click's own usage line
#   CD004 - every one of a non-hidden option's opt strings (short AND long) appears
#           backticked in its verb's section
#   CD005 - every flag named in a bold definition-position mention is a real opt of
#           that verb (catches a renamed or deleted flag left documented)
#   CD006 - every subcommand of a group is named in the group's section
#   CD007 - every non-hidden option declares a non-empty help=
#
# Usage: python3 scripts/validate-cli-docs.py
#
# Exit codes:
#   0 - the doc covers the CLI
#   1 - one or more violations found
#   2 - usage / doc file not found

import re
import sys
from pathlib import Path

import click

ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = ROOT / "docs" / "shell-commands.md"

# Unlike the other two linters in this directory, this one imports its subject rather
# than reading it as text or AST. That is deliberate and confined to this script: the
# subject here is not the source text but the *parser's resolved behaviour* -- the usage
# line, which opt strings a decorator actually produced, whether a param is hidden.
# Only click can report those, and re-deriving them from AST would reimplement click's
# formatting and then drift from it, which is the very failure this script exists to
# catch. `scripts/` is not on sys.path as a package root, and `agent_wrap` is not
# installed (`package = false`), so the repo root has to be added by hand.
sys.path.insert(0, str(ROOT))

from agent_wrap.__main__ import cli_root  # noqa: E402

SECTION_HEADING = re.compile(r"^## `agent ([a-z-]+)`\s*$")
TABLE_ROW = re.compile(r"^\| `([a-z-]+)` \|")
BACKTICKED = re.compile(r"`([^`]+)`")
BOLD_SPAN = re.compile(r"\*\*([^*]+)\*\*")
FENCE = re.compile(r"^```")

# An opening fence and its closing partner: fewer than this and there is no block.
FENCE_PAIR = 2

# Click adds its help option lazily, so it is absent from `Command.params` and needs no
# filtering there. It is named here only so a future explicit declaration stays exempt:
# `-h`/`--help` comes from CLI_CONTEXT_SETTINGS on the root group and is documented once
# in the doc's intro, not per verb.
HELP_OPTS = frozenset({"-h", "--help"})

# One violation: the rule code, and the message shown to the operator.
Violation = tuple[str, str]

RULE_CODES = ("CD001", "CD002", "CD003", "CD004", "CD005", "CD006", "CD007")


def strip_fences(text: str) -> str:
    """
    Drop fenced code blocks from *text*.

    Required before any inline-code scanning: a ``` fence is an odd run of backticks, so
    it desynchronises the inline-code pairing and makes the scanner read prose as code.
    Dropping them is also the right reading -- a flag is documented by the prose or the
    bullet that describes it, not by appearing in a synopsis or an example command.
    """
    kept: list[str] = []
    inside = False
    for line in text.split("\n"):
        if FENCE.match(line):
            inside = not inside
        elif not inside:
            kept.append(line)
    return "\n".join(kept)


def backticked_tokens(text: str) -> set[str]:
    """
    Return every backticked span in *text*, reduced to its first whitespace-delimited word.

    This is what makes CD004 tolerant of the two flag-documentation conventions in live
    use -- bullets (``- **`-f`/`--full`** -- ...``) and prose (``The **`-v`/`--verbose`**
    flag is ...``) -- and of a metavar riding inside the backticks (``--from D``).
    """
    tokens: set[str] = set()
    for span in BACKTICKED.findall(text):
        words = span.split()
        if words:
            tokens.add(words[0])
    return tokens


def bold_flags(text: str) -> set[str]:
    """
    Return the flags named inside a bold span -- the doc's flag-definition position.

    Restricted to bold on purpose. Prose legitimately names flags that belong to other
    commands (the `run` section lists Claude Code's own ``-p``/``--print``/``--bare``/
    ``--safe-mode``, and mentions ``agent logs --stop``), and none of those are bolded,
    so CD005 would false-positive on every one of them if it read plain backticks.
    """
    flags: set[str] = set()
    for span in BOLD_SPAN.findall(text):
        flags |= {token for token in backticked_tokens(span) if token.startswith("-")}
    return flags


def documented_options(command: click.Command) -> list[click.Option]:
    return [
        param
        for param in command.params
        if isinstance(param, click.Option)
        and not param.hidden
        and not HELP_OPTS.intersection(param.opts)
    ]


def all_options(command: click.Command) -> list[click.Option]:
    options = documented_options(command)
    if isinstance(command, click.Group):
        for subcommand in command.commands.values():
            options.extend(documented_options(subcommand))
    return options


def usage_line(name: str, command: click.Command) -> str:
    parent = click.Context(cli_root, info_name="agent")
    with click.Context(command, info_name=name, parent=parent) as ctx:
        return " ".join(["agent", name, *command.collect_usage_pieces(ctx)])


def synopsis_block(section: str) -> list[str]:
    lines = section.split("\n")
    fences = [i for i, line in enumerate(lines) if FENCE.match(line)]
    if len(fences) < FENCE_PAIR:
        return []
    return [line for line in lines[fences[0] + 1 : fences[1]] if line.strip()]


def parse_sections(text: str) -> dict[str, tuple[int, str]]:
    lines = text.split("\n")
    starts: list[tuple[int, str]] = [
        (i, match.group(1))
        for i, line in enumerate(lines)
        if (match := SECTION_HEADING.match(line))
    ]

    sections: dict[str, tuple[int, str]] = {}
    for position, (index, verb) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        sections[verb] = (index + 1, "\n".join(lines[index + 1 : end]))
    return sections


def table_verbs(text: str) -> set[str]:
    intro = text.split("\n## ", 1)[0]
    return {match.group(1) for line in intro.split("\n") if (match := TABLE_ROW.match(line))}


def check_structure(text: str, sections: dict[str, tuple[int, str]]) -> list[Violation]:
    """Return CD001 and CD002: every verb has one section and one table row, and nothing extra."""
    registered = set(cli_root.commands)
    return [
        *(
            ("CD001", f"`agent {verb}` is registered but has no `## \\`agent {verb}\\`` section")
            for verb in sorted(registered - set(sections))
        ),
        *(
            (
                "CD001",
                (
                    f"{DOC_PATH.name}:{sections[verb][0]}: "
                    f"`agent {verb}` is documented but not registered"
                ),
            )
            for verb in sorted(set(sections) - registered)
        ),
        *(
            ("CD002", f"`{verb}` is missing from the intro verb table")
            for verb in sorted(registered - table_verbs(text))
        ),
    ]


def check_help_declared(name: str, options: list[click.Option]) -> list[Violation]:
    """Return a CD007 violation for every option that declares no help text."""
    return [
        ("CD007", f"`agent {name}`: option {'/'.join(option.opts)} declares no help=")
        for option in options
        if not option.help
    ]


def check_synopsis(name: str, command: click.Command, section: str, where: str) -> list[Violation]:
    """
    Return a CD003 violation when the synopsis block is not click's own usage line.

    Skipped for a group: click's usage for one is the generic ``[OPTIONS] COMMAND
    [ARGS]...``, which the doc deliberately replaces with a per-subcommand synopsis.
    CD006 covers a group's section instead.
    """
    if isinstance(command, click.Group):
        return []

    expected = usage_line(name, command)
    block = synopsis_block(section)
    if not block:
        return [("CD003", f"{where}: no synopsis code block")]
    if block != [expected]:
        return [("CD003", f"{where}: synopsis is {block!r}, click says [{expected!r}]")]
    return []


def check_option_coverage(
    options: list[click.Option], tokens: set[str], where: str
) -> list[Violation]:
    """Return a CD004 violation for every opt string the section never names."""
    return [
        (
            "CD004",
            f"{where}: option {'/'.join(option.opts)}: `{opt}` does not appear in the section",
        )
        for option in options
        for opt in sorted(set(option.opts) | set(option.secondary_opts))
        if opt not in tokens
    ]


def check_stale_flags(
    name: str, options: list[click.Option], prose: str, where: str
) -> list[Violation]:
    """Return a CD005 violation for every documented flag that is not a real opt."""
    real = {opt for option in options for opt in (*option.opts, *option.secondary_opts)}
    return [
        ("CD005", f"{where}: documents `{flag}`, which is not an option of `agent {name}`")
        for flag in sorted(bold_flags(prose) - real - HELP_OPTS)
    ]


def check_subcommands(command: click.Command, tokens: set[str], where: str) -> list[Violation]:
    """Return a CD006 violation for every subcommand of a group the section never names."""
    if not isinstance(command, click.Group):
        return []
    return [
        ("CD006", f"{where}: subcommand `{subcommand}` is not documented")
        for subcommand in sorted(command.commands)
        if subcommand not in tokens
    ]


def check_command(name: str, command: click.Command, section: str, line: int) -> list[Violation]:
    """Return CD003 to CD007 for one registered verb against its doc section."""
    where = f"{DOC_PATH.name}:{line}: `agent {name}`"
    prose = strip_fences(section)
    tokens = backticked_tokens(prose)
    options = all_options(command)

    return [
        *check_help_declared(name, options),
        *check_synopsis(name, command, section, where),
        *check_subcommands(command, tokens, where),
        *check_option_coverage(options, tokens, where),
        *check_stale_flags(name, options, prose, where),
    ]


def main() -> None:
    if not DOC_PATH.is_file():
        print(f"ERROR: {DOC_PATH} not found", file=sys.stderr)
        sys.exit(2)

    text = DOC_PATH.read_text(encoding="utf-8")
    sections = parse_sections(text)

    violations = check_structure(text, sections)
    for name, command in sorted(cli_root.commands.items()):
        if name in sections:
            line, section = sections[name]
            violations.extend(check_command(name, command, section, line))

    if violations:
        for code, message in violations:
            print(f"error: {code}: {message}", file=sys.stderr)

        counts = [
            f"{sum(1 for violation in violations if violation[0] == code)} {code}"
            for code in RULE_CODES
            if any(violation[0] == code for violation in violations)
        ]
        print(
            f"\n{len(violations)} CLI documentation violations found ({', '.join(counts)})",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"docs/shell-commands.md covers all {len(cli_root.commands)} verbs and their flags.")
    sys.exit(0)


if __name__ == "__main__":
    main()

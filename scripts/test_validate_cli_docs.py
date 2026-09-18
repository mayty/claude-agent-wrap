# This file has been created with the assistance of an AI tool.
"""Tests for scripts/validate-cli-docs.py."""

import importlib.util
import sys
from pathlib import Path
from textwrap import dedent

import click
import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent / "validate-cli-docs.py"
_spec = importlib.util.spec_from_file_location("validate_cli_docs", _SCRIPT_PATH)
assert _spec is not None
_module = importlib.util.module_from_spec(_spec)
sys.modules["validate_cli_docs"] = _module
_spec.loader.exec_module(_module)  # pyrefly: ignore [missing-attribute]

# Convenience aliases
strip_fences = _module.strip_fences
backticked_tokens = _module.backticked_tokens
bold_flags = _module.bold_flags
documented_options = _module.documented_options
usage_line = _module.usage_line
synopsis_block = _module.synopsis_block
parse_sections = _module.parse_sections
table_verbs = _module.table_verbs
check_structure = _module.check_structure
check_command = _module.check_command

# A section that documents `-f`/`--full` exactly the way the real doc's bullets do.
REBUILD_SECTION = dedent("""
    ```
    agent rebuild [OPTIONS]
    ```

    Rebuilds the resolved image.

    - **`-f`/`--full`** — rebuilds the base image first.
""")

# The 1-based line number of the `## `agent run`` heading in PARSE_SECTIONS_DOC.
RUN_HEADING_LINE = 3
PARSE_SECTIONS_DOC = (
    "# Shell Commands\n\n## `agent run`\n\nbody one\n\n## `agent logs`\n\nbody two\n"
)


def codes(violations: list[tuple[str, str]]) -> list[str]:
    return [code for code, _ in violations]


def test_strip_fences_drops_block_content() -> None:
    text = "before\n```\nagent run [OPTIONS]\n```\nafter"
    assert strip_fences(text) == "before\nafter"


def test_strip_fences_drops_labelled_fence() -> None:
    text = "before\n```python\nx = 1\n```\nafter"
    assert strip_fences(text) == "before\nafter"


def test_strip_fences_is_what_keeps_inline_scanning_aligned() -> None:
    """A ``` fence is an odd backtick run, so it would pair with the next inline span."""
    text = "```\nagent rebuild [OPTIONS]\n```\n\nPasses `HOST_UID`/`HOST_GID` args."
    assert backticked_tokens(strip_fences(text)) == {"HOST_UID", "HOST_GID"}
    assert "Passes" in backticked_tokens(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("`-f`/`--full`", {"-f", "--full"}),
        ("`--from D`", {"--from"}),
        ("`--port N` and `-s`", {"--port", "-s"}),
        ("`check SIDECAR`", {"check"}),
        ("no code here", set()),
    ],
)
def test_backticked_tokens(text: str, expected: set[str]) -> None:
    assert backticked_tokens(text) == expected


def test_bold_flags_reads_only_bold_spans() -> None:
    text = "- **`-f`/`--full`** — see `--no-cache` and `agent logs --stop`."
    assert bold_flags(text) == {"-f", "--full"}


def test_bold_flags_ignores_non_flag_bold_tokens() -> None:
    assert bold_flags("- **`check SIDECAR`** — reports secrets.") == set()


def test_bold_flags_reads_prose_convention() -> None:
    assert bold_flags("The **`-v`/`--verbose`** flag adds a table.") == {"-v", "--verbose"}


def test_documented_options_excludes_hidden() -> None:
    command = click.Command(
        "logs",
        params=[
            click.Option(["-s", "--stop"], is_flag=True, help="Stop it."),
            click.Option(["--foreground"], is_flag=True, hidden=True),
        ],
    )
    assert [option.opts for option in documented_options(command)] == [["-s", "--stop"]]


def test_usage_line_matches_click() -> None:
    command = click.Command("rebuild", params=[click.Option(["-f", "--full"], is_flag=True)])
    assert usage_line("rebuild", command) == "agent rebuild [OPTIONS]"


def test_usage_line_includes_arguments() -> None:
    command = click.Command(
        "run",
        params=[click.Argument(["claude_args"], nargs=-1, type=click.UNPROCESSED)],
    )
    assert usage_line("run", command) == "agent run [OPTIONS] [CLAUDE_ARGS]..."


def test_synopsis_block_returns_fenced_lines() -> None:
    assert synopsis_block(REBUILD_SECTION) == ["agent rebuild [OPTIONS]"]


def test_synopsis_block_empty_without_a_fence() -> None:
    assert synopsis_block("Just prose about `agent rebuild`.") == []


def test_parse_sections_splits_on_verb_headings() -> None:
    sections = parse_sections(PARSE_SECTIONS_DOC)
    assert sorted(sections) == ["logs", "run"]
    assert sections["run"][0] == RUN_HEADING_LINE
    assert "body one" in sections["run"][1]
    assert "body one" not in sections["logs"][1]


def test_table_verbs_reads_only_the_intro() -> None:
    text = (
        "| Verb | Purpose |\n| --- | --- |\n| `run` | Launch |\n\n## `agent run`\n\n| `x` | y |\n"
    )
    assert table_verbs(text) == {"run"}


def test_check_structure_flags_undocumented_verb() -> None:
    text = "| `run` | Launch |\n"
    violations = check_structure(text, {})
    assert "CD001" in codes(violations)
    assert "CD002" in codes(violations)


def test_check_structure_flags_section_for_unregistered_verb() -> None:
    violations = check_structure("", {"bogus": (10, "body")})
    assert (
        "CD001",
        "shell-commands.md:10: `agent bogus` is documented but not registered",
    ) in violations


def test_check_command_accepts_a_complete_section() -> None:
    command = click.Command(
        "rebuild",
        params=[click.Option(["-f", "--full"], is_flag=True, help="Rebuild the base first.")],
    )
    assert check_command("rebuild", command, REBUILD_SECTION, 53) == []


def test_check_command_cd003_on_wrong_synopsis() -> None:
    command = click.Command(
        "rebuild",
        params=[click.Option(["-f", "--full"], is_flag=True, help="h")],
    )
    section = REBUILD_SECTION.replace("agent rebuild [OPTIONS]", "agent rebuild")
    assert codes(check_command("rebuild", command, section, 53)) == ["CD003"]


def test_check_command_cd003_on_missing_synopsis() -> None:
    command = click.Command("rebuild")
    assert codes(check_command("rebuild", command, "Just prose.", 53)) == ["CD003"]


def test_check_command_cd004_on_undocumented_short_flag() -> None:
    """The live drift this script was written for: a short flag the doc never names."""
    command = click.Command(
        "rebuild",
        params=[click.Option(["-f", "--full"], is_flag=True, help="h")],
    )
    long_form_only = REBUILD_SECTION.replace("**`-f`/`--full`**", "**`--full`**")
    violations = check_command("rebuild", command, long_form_only, 53)
    assert codes(violations) == ["CD004"]
    assert "`-f`" in violations[0][1]


def test_check_command_cd004_and_cd005_on_a_renamed_short_flag() -> None:
    """Renaming a short in the code alone is both an uncovered opt and a stale mention."""
    command = click.Command(
        "rebuild",
        params=[click.Option(["-x", "--full"], is_flag=True, help="h")],
    )
    assert codes(check_command("rebuild", command, REBUILD_SECTION, 53)) == ["CD004", "CD005"]


def test_check_command_cd005_on_flag_that_no_longer_exists() -> None:
    """The doc still documents `-f`/`--full` after the option was deleted from the code."""
    violations = check_command("rebuild", click.Command("rebuild"), REBUILD_SECTION, 53)
    assert codes(violations) == ["CD005", "CD005"]


def test_check_command_cd007_on_option_without_help() -> None:
    command = click.Command("rebuild", params=[click.Option(["-f", "--full"], is_flag=True)])
    assert codes(check_command("rebuild", command, REBUILD_SECTION, 53)) == ["CD007"]


def test_check_command_skips_synopsis_for_a_group() -> None:
    """A group's click usage is generic, so the doc's hand-written synopsis is left alone."""
    group = click.Group("secrets", commands={"check": click.Command("check")})
    section = (
        "```\nagent secrets check [OPTIONS] SIDECAR\n```\n\n- **`check SIDECAR`** — reports.\n"
    )
    assert check_command("secrets", group, section, 187) == []


def test_check_command_cd006_on_undocumented_subcommand() -> None:
    group = click.Group(
        "secrets",
        commands={"check": click.Command("check"), "cleanup": click.Command("cleanup")},
    )
    section = "- **`check SIDECAR`** — reports.\n"
    violations = check_command("secrets", group, section, 187)
    assert codes(violations) == ["CD006"]
    assert "`cleanup`" in violations[0][1]


def test_check_command_covers_a_subcommands_options() -> None:
    group = click.Group(
        "secrets",
        commands={
            "check": click.Command(
                "check",
                params=[click.Option(["-q", "--quiet"], is_flag=True, help="h")],
            )
        },
    )
    section = "- **`check SIDECAR`** — reports.\n"
    violations = check_command("secrets", group, section, 187)
    assert codes(violations) == ["CD004", "CD004"]

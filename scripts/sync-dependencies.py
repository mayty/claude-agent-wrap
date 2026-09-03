# This file has been created with the assistance of an AI tool.
#
# Syncs the `>=` floors in pyproject.toml to the versions uv actually resolved.
#
# Reads a `uv tree` listing on stdin and rewrites one dependency array in
# pyproject.toml so every floor names the version currently in uv.lock. The
# operator is normalized to `>=` and everything after the version is dropped:
# uv.lock is what pins, so a declared requirement states a floor and nothing more.
#
# Usage:
#   uv tree --frozen --no-dev   --depth 1 | python3 scripts/sync-dependencies.py prod
#   uv tree --frozen --only-dev --depth 1 | python3 scripts/sync-dependencies.py dev
#
# Exit codes:
#   0 - the array is in step with the tree (rewritten if it was not)
#   1 - usage error, an empty tree, or the named array is not in pyproject.toml

import re
import sys
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

# `uv tree` draws every dependency with one of these connectors and indents each
# level below the first. Matching them at column zero therefore selects exactly the
# direct dependencies -- the ones declared in pyproject.toml -- and drops both the
# project's own root line and any transitive line, whatever `--depth` was passed.
TREE_CONNECTORS = ("├──", "└──")

# The two arrays this script knows how to rewrite, each captured in three pieces:
# the header through the opening bracket, the body, and the closing bracket. Both
# are anchored on their owning table, so an array of the same name elsewhere in the
# file cannot be hit by accident.
SECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "prod": re.compile(r"(?ms)^(\[project\].*?^dependencies = \[\n)(.*?)(^\])"),
    "dev": re.compile(r"(?ms)^(\[dependency-groups\].*?^dev = \[\n)(.*?)(^\])"),
}

# One declared requirement: indent and opening quote, name, optional extras, the
# version specifier, then the closing quote and whatever trails it.
REQUIREMENT_PATTERN = re.compile(
    r'^(?P<prefix>\s*")(?P<name>[A-Za-z0-9._-]+)(?P<extras>\[[^\]]*\])?'
    r'(?P<spec>[^"]*)"(?P<trail>.*)$'
)

# The version in the first constraint of a specifier, e.g. `9.1.1` in `>=9.1.1,<10`.
FLOOR_PATTERN = re.compile(r"^[<>=!~]=?\s*(?P<version>[^,\s]+)")


def canonical(name: str) -> str:
    """Return the PEP 503 canonical form of a package name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_tree(text: str) -> dict[str, str]:
    """Map canonical package name to resolved version, for the tree's direct deps."""
    versions: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith(TREE_CONNECTORS):
            continue
        fields = line.split()
        if len(fields) < 3:  # noqa: PLR2004 -- connector, name, version
            continue
        name = fields[1].split("[")[0]  # `uvicorn[standard]` -> `uvicorn`
        versions[canonical(name)] = fields[2].removeprefix("v")
    return versions


def rewrite_body(body: str, versions: dict[str, str]) -> tuple[str, list[tuple[str, str, str]]]:
    """
    Rewrite each known requirement in *body* to `>=` its resolved version.

    Returns the new body and the (name, old, new) triples whose version moved.
    Lines that are not requirements -- comments, blanks -- pass through untouched,
    as do requirements the tree says nothing about.
    """
    changes: list[tuple[str, str, str]] = []
    lines: list[str] = []
    for line in body.split("\n"):
        match = REQUIREMENT_PATTERN.match(line)
        version = versions.get(canonical(match["name"])) if match else None
        if match is None or version is None:
            lines.append(line)
            continue
        floor = FLOOR_PATTERN.match(match["spec"])
        old = floor["version"] if floor else ""
        if old != version:
            changes.append((match["name"], old, version))
        extras = match["extras"] or ""
        lines.append(f'{match["prefix"]}{match["name"]}{extras}>={version}"{match["trail"]}')
    return "\n".join(lines), changes


def update_dependencies(section: str, tree: str) -> int:
    """Sync one pyproject.toml array against *tree*. Returns a process exit code."""
    versions = parse_tree(tree)
    if not versions:
        print(f"no dependencies found in the {section} tree", file=sys.stderr)
        return 1

    text = PYPROJECT.read_text(encoding="utf-8")
    match = SECTION_PATTERNS[section].search(text)
    if match is None:
        print(f"no {section} dependency array in {PYPROJECT}", file=sys.stderr)
        return 1

    body, changes = rewrite_body(match[2], versions)
    if body == match[2]:
        print(f"{section} dependencies up to date")
        return 0

    PYPROJECT.write_text(text[: match.start(2)] + body + text[match.end(2) :], encoding="utf-8")
    print(f"{section} dependencies updated")
    for name, old, new in changes:
        print(f"  {name} {old or '(none)'} -> {new}")
    return 0


def main(argv: list[str]) -> int:
    sections = "|".join(SECTION_PATTERNS)
    if len(argv) != 1 or argv[0] not in SECTION_PATTERNS:
        print(f"usage: uv tree ... | {Path(__file__).name} <{sections}>", file=sys.stderr)
        return 1
    return update_dependencies(argv[0], sys.stdin.read())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

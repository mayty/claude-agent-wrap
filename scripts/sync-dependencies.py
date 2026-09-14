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

import sys
from pathlib import Path

import tomlkit
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

# `uv tree` draws every dependency with one of these connectors and indents each
# level below the first. Matching them at column zero therefore selects exactly the
# direct dependencies -- the ones declared in pyproject.toml -- and drops both the
# project's own root line and any transitive line, whatever `--depth` was passed.
TREE_CONNECTORS = ("├──", "└──")

# The two arrays this script knows how to rewrite, as the path to each through the
# document. Anchored on the owning table, so an array of the same name elsewhere in
# the file cannot be hit by accident.
SECTION_PATHS: dict[str, tuple[str, str]] = {
    "prod": ("project", "dependencies"),
    "dev": ("dependency-groups", "dev"),
}


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
        versions[canonicalize_name(name)] = fields[2].removeprefix("v")
    return versions


def floor_of(requirement: Requirement) -> str:
    """Return the version in the requirement's first constraint, or "" when it states none."""
    return next((spec.version for spec in requirement.specifier), "")


def rewrite(entry: str, versions: dict[str, str]) -> tuple[str, tuple[str, str, str] | None]:
    """
    Restate one declared requirement as `>=` its resolved version.

    Returns the new text and, when the floor moved, the (name, old, new) triple naming
    the move. A requirement the tree says nothing about is returned untouched, and so is
    anything that does not parse as a requirement at all -- this script rewrites floors,
    and is not the place a malformed declaration should first be reported.
    """
    try:
        requirement = Requirement(entry)
    except InvalidRequirement:
        return entry, None
    version = versions.get(canonicalize_name(requirement.name))
    if version is None:
        return entry, None
    old = floor_of(requirement)
    change = None if old == version else (requirement.name, old, version)
    extras = f"[{','.join(sorted(requirement.extras))}]" if requirement.extras else ""
    return f"{requirement.name}{extras}>={version}", change


def update_dependencies(section: str, tree: str) -> int:
    versions = parse_tree(tree)
    if not versions:
        print(f"no dependencies found in the {section} tree", file=sys.stderr)
        return 1

    # tomlkit rather than a regex splice: it round-trips the document, so the comments,
    # blank lines and array formatting around the entries come back byte-identical and
    # the diff is exactly the floors that moved.
    document = tomlkit.parse(PYPROJECT.read_text(encoding="utf-8"))
    table, key = SECTION_PATHS[section]
    array = document.get(table, {}).get(key)
    if array is None:
        print(f"no {section} dependency array in {PYPROJECT}", file=sys.stderr)
        return 1

    changes: list[tuple[str, str, str]] = []
    for index, entry in enumerate(array):
        replacement, change = rewrite(str(entry), versions)
        if change is not None:
            changes.append(change)
        if replacement != str(entry):
            array[index] = replacement

    if not changes:
        print(f"{section} dependencies up to date")
        return 0

    PYPROJECT.write_text(tomlkit.dumps(document), encoding="utf-8")
    print(f"{section} dependencies updated")
    for name, old, new in changes:
        print(f"  {name} {old or '(none)'} -> {new}")
    return 0


def main(argv: list[str]) -> int:
    sections = "|".join(SECTION_PATHS)
    if len(argv) != 1 or argv[0] not in SECTION_PATHS:
        print(f"usage: uv tree ... | {Path(__file__).name} <{sections}>", file=sys.stderr)
        return 1
    return update_dependencies(argv[0], sys.stdin.read())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

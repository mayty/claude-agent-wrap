# This file has been created with the assistance of an AI tool.
#
# Validates markdown files for broken internal links and doc reachability.
#
# Rules enforced:
#   1. Every internal link resolves to an existing file.
#   2. Every .md file (except those under ops/, and CLAUDE.md) is reachable
#      from the root README.md via one or more link hops. Orphaned docs rot
#      silently, so an unreachable doc is a hard error.
#
# Search paths:
#   Project root (non-recursive)
#   docs (recursive)  # noqa: ERA001
#   agent_wrap (recursive)  # noqa: ERA001
#   ops (recursive, with special absolute path handling)
#
# Usage: python3 validate-markdown-links.py
#
# Exit codes:
#   0 - all internal links resolve and all docs are reachable
#   1 - one or more broken links or unreachable docs found
#   2 - usage / directory not found

import re
import sys
from pathlib import Path

# Regex to match markdown links: [text](target) or [`text`](target)
# Captures: group 1 = link text, group 2 = target path
LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

EXTERNAL_PREFIXES = ("http://", "https://")


def get_md_files(root_dir: Path) -> list[Path]:
    """Find all .md files in the specified directories."""
    md_files: list[Path] = []

    # Project root (non-recursive), excluding CLAUDE.md which we add separately
    root_md_files = [f for f in root_dir.iterdir() if f.is_file() and f.suffix == ".md"]
    md_files.extend(root_md_files)

    # Recursive directories
    for subdir in ("docs", "agent_wrap", "ops", "releases"):
        subdir_path = root_dir / subdir
        if subdir_path.is_dir():
            md_files.extend(subdir_path.rglob("*.md"))

    return md_files


def resolve_target(source_file: Path, target: str, root_dir: Path) -> Path | None:
    """
    Resolve a link target to an absolute filesystem path.

    Returns None for targets that don't map to a local file: external URLs,
    fragment-only / empty links, and absolute paths outside the ops special
    case (those can't be resolved against the repo and are warned about by
    the caller).
    """
    if target.startswith(EXTERNAL_PREFIXES):
        return None

    if target.startswith("#") or target == "":
        return None

    file_target = target.split("#", 1)[0]

    try:
        source_file.relative_to(root_dir / "ops")
        in_ops = True
    except ValueError:
        in_ops = False

    if in_ops and file_target.startswith("/opt/agent-wrap/"):
        relative_path = file_target.removeprefix("/opt/agent-wrap/")
        return (root_dir / "ops" / relative_path).resolve()

    if file_target.startswith("/"):
        return None

    return (source_file.parent / file_target).resolve()


def validate_link(source_file: Path, target: str, root_dir: Path) -> tuple[bool, str]:
    """
    Validate a single link target from a source markdown file.

    Returns:
        (is_valid, error_message)

    """
    if target.startswith(EXTERNAL_PREFIXES) or target.startswith("#") or target == "":
        return True, ""

    resolved_path = resolve_target(source_file, target, root_dir)

    if resolved_path is None:
        # Reaches here only for an absolute path outside the ops special case.
        file_target = target.split("#", 1)[0]
        return (
            True,
            f"WARNING: Absolute path '{file_target}' in {source_file.relative_to(root_dir)}",
        )

    if not resolved_path.exists():
        return (
            False,
            f"ERROR: {source_file.relative_to(root_dir)}: Broken link '{target}' -> {resolved_path} (does not exist)",
        )

    return True, ""


# .md files exempt from the README-reachability rule (config, not docs).
EXEMPT_FROM_REACHABILITY = {"CLAUDE.md"}


def get_reachable_md_files(root: Path) -> set[Path]:
    """Walk the link graph from the `root`, returning every .md file reached."""
    root = root.resolve()
    root_dir = root.parent
    visited: set[Path] = {root}
    queue: list[Path] = [root]

    while queue:
        current = queue.pop()

        for match in LINK_PATTERN.finditer(current.read_text(encoding="utf-8")):
            resolved = resolve_target(current, match.group(2), root_dir)
            if (
                resolved is not None
                and resolved.suffix == ".md"
                and resolved.exists()
                and resolved not in visited
            ):
                visited.add(resolved)
                queue.append(resolved)

    return visited


def check_reachability(md_files: list[Path], root_dir: Path) -> list[str]:
    """Return an error for each .md file not reachable from README.md."""
    reachable = get_reachable_md_files(root_dir / "README.md")
    errors: list[str] = []

    for md_file in md_files:
        rel = md_file.relative_to(root_dir)
        if rel.parts[0] == "ops":
            continue
        if rel.as_posix() in EXEMPT_FROM_REACHABILITY:
            continue
        if md_file.resolve() not in reachable:
            errors.append(f"ERROR: {rel}: not reachable from README.md via any link path")

    return errors


def process_file(md_file: Path, root_dir: Path) -> tuple[int, int, int, list[str], list[str]]:
    """Process a single markdown file and return validation results."""
    errors = 0
    warnings = 0
    links_checked = 0
    error_msgs: list[str] = []
    warning_msgs: list[str] = []

    try:
        content = md_file.read_text(encoding="utf-8")
    except OSError as e:
        return 1, 0, 0, [f"ERROR: Could not read {md_file.relative_to(root_dir)}: {e}"], []

    for line in content.split("\n"):
        for match in LINK_PATTERN.finditer(line):
            target = match.group(2)
            links_checked += 1

            is_valid, message = validate_link(md_file, target, root_dir)

            if not is_valid:
                errors += 1
                error_msgs.append(message)
            elif message.startswith("WARNING:"):
                warnings += 1
                warning_msgs.append(message)

    return errors, warnings, links_checked, error_msgs, warning_msgs


def main() -> None:
    root_dir = Path(__file__).parent.parent

    md_files = get_md_files(root_dir)

    if not md_files:
        print("No markdown files found to validate.")
        sys.exit(0)

    total_errors = 0
    total_warnings = 0
    total_links_checked = 0

    for md_file in md_files:
        errors, warnings, links_checked, error_msgs, warning_msgs = process_file(md_file, root_dir)

        total_errors += errors
        total_warnings += warnings
        total_links_checked += links_checked

        for msg in error_msgs:
            print(msg, file=sys.stderr)
        for msg in warning_msgs:
            print(msg, file=sys.stderr)

    reachability_errors = check_reachability(md_files, root_dir)
    for msg in reachability_errors:
        print(msg, file=sys.stderr)
    total_errors += len(reachability_errors)

    print(f"Checked {len(md_files)} markdown files, validated {total_links_checked} links.")

    if total_warnings > 0:
        print(f"Warnings: {total_warnings}")

    if total_errors > 0:
        print(f"Errors: {total_errors}", file=sys.stderr)
        sys.exit(1)

    print("All links valid.")
    sys.exit(0)


if __name__ == "__main__":
    main()

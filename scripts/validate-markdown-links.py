# This file has been created with the assistance of an AI tool.
#
# Validates markdown files for broken links, unresolved anchors, and doc reachability,
# and resolves every GitHub URL that pins a repo path to a ref against the git tree at
# that ref.
#
# releases/styleguide.md requires a release note to link each repo path it mentions at
# the release's own tag, because paths move between releases -- the ops/ reorg in 0.4.0
# means `providers/template` resolves at 0.3.0 and nowhere later. Only git can answer
# what a path was at a tag, so tagged URLs are resolved through `git ls-tree` rather
# than against the working tree. A ref that is not a tag yet is the release being
# drafted: its links fall back to the working tree and raise a warning, not an error.
# ML006 is what keeps that fallback honest -- a typo'd tag is an error rather than a
# silent drop-through to the working tree.
#
# Rules enforced:
#   ML001 - every internal link resolves to an existing file
#   ML002 - every .md file (except those under ops/, and CLAUDE.md) is reachable from
#           the root README.md via one or more link hops. Orphaned docs rot silently,
#           so an unreachable doc is a hard error.
#   ML003 - a link's #anchor matches a heading of the markdown file it points at
#   ML004 - a GitHub URL's path exists at the ref it pins
#   ML005 - a GitHub /blob/ URL names a file and a /tree/ URL names a directory
#   ML006 - a release note pins every repo URL to its own tag
#
# Search paths:
#   Project root (non-recursive)
#   docs, agent_wrap, releases (recursive)  # noqa: ERA001
#   ops (recursive, with special absolute path handling)
#
# Usage: python3 scripts/validate-markdown-links.py
#
# Exit codes:
#   0 - every link, anchor and pinned path resolves, and every doc is reachable
#   1 - one or more violations found
#   2 - no git tags are available, so pinned links cannot be resolved

import functools
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Inline links: [text](target) or [`text`](target). Group 2 is the target.
LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

# Reference-style definitions: [label]: target. These carry most of the pinned URLs in
# releases/0.8.0.md and are invisible to LINK_PATTERN, which needs parentheses.
REFDEF_PATTERN = re.compile(r"^ {0,3}\[([^\]]+)\]:\s*(\S+)\s*$")

EXTERNAL_PREFIXES = ("http://", "https://")

# Only this repo's own URLs can be resolved offline. Third-party GitHub links
# (BerriAI/litellm, astral-sh/python-build-standalone) are left alone.
REPO_URL = re.compile(
    r"^https://github\.com/mayty/claude-agent-wrap/(blob|tree)/([^/#]+)/([^#]+?)(?:#(.+))?$"
)

RELEASE_VERSION = re.compile(r"^\d+\.\d+\.\d+$")

HEADING = re.compile(r"^ {0,3}#{1,6} +(.*?)(?: +#+)?\s*$")
FENCE = re.compile(r"^\s*(?:```|~~~)")

# GitHub slugs the *rendered* heading, so a link in one contributes only its text.
INLINE_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")

# GitHub's heading slug: lowercase, drop everything outside this set, spaces to dashes.
# Runs of dashes are deliberately not collapsed -- GitHub deletes punctuation without
# closing the gap, so "A -- B" slugs to "a----b".
SLUG_DROP = re.compile(r"[^a-z0-9 _-]")

# .md files exempt from the README-reachability rule (config, not docs).
EXEMPT_FROM_REACHABILITY = {"CLAUDE.md"}

# One violation: the source file, a 1-based line (0 when the rule is not line-anchored),
# the rule code, and the message.
Violation = tuple[Path, int, str, str]


def git(*args: str) -> tuple[str, int]:
    """Run git in the repo root, returning (stdout, returncode). Never raises."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            # A blob is arbitrary bytes; only .md content is ever parsed, so a
            # non-UTF-8 file must degrade rather than raise.
            errors="replace",
            cwd=str(ROOT),
        )
    except OSError, subprocess.SubprocessError:
        return "", 1
    return result.stdout, result.returncode


def tag_set() -> frozenset[str]:
    out, returncode = git("tag")
    if returncode != 0:
        return frozenset()
    return frozenset(out.split())


@functools.cache
def ref_tree(ref: str) -> dict[str, str]:
    """Map every path in *ref*'s tree to its git object type ("blob" or "tree")."""
    out, returncode = git("ls-tree", "-r", "-t", ref)
    tree: dict[str, str] = {}
    if returncode != 0:
        return tree

    # `<mode> <type> <sha>\t<path>`. Parsed rather than asked for via --format, which
    # keeps this working on the git versions already in use.
    for line in out.split("\n"):
        head, tab, path = line.partition("\t")
        if not tab:
            continue
        _mode, _, rest = head.partition(" ")
        objtype, _, _sha = rest.partition(" ")
        if objtype:
            tree[path] = objtype

    return tree


def path_kind(ref: str | None, path: str) -> str | None:
    """Type of *path* at *ref*, or in the working tree when *ref* is None."""
    if ref is None:
        candidate = ROOT / path
        if candidate.is_file():
            return "blob"
        if candidate.is_dir():
            return "tree"
        return None

    return ref_tree(ref).get(path)


@functools.cache
def read_at(ref: str | None, path: str) -> str | None:
    """Contents of *path* at *ref*, or from the working tree when *ref* is None."""
    if ref is None:
        try:
            return (ROOT / path).read_text(encoding="utf-8")
        except OSError:
            return None

    out, returncode = git("cat-file", "blob", f"{ref}:{path}")
    return None if returncode != 0 else out


def strip_fences(text: str) -> str:
    """Drop fenced code blocks. Headings inside a fence get no anchor on GitHub."""
    kept: list[str] = []
    inside = False

    for line in text.split("\n"):
        if FENCE.match(line):
            inside = not inside
            continue
        if not inside:
            kept.append(line)

    return "\n".join(kept)


def heading_slugs(text: str) -> set[str]:
    """Every anchor GitHub would generate for *text*, including the -1 repeat suffixes."""
    slugs: set[str] = set()
    seen: dict[str, int] = {}

    for line in strip_fences(text).split("\n"):
        match = HEADING.match(line)
        if match is None:
            continue

        text_only = INLINE_LINK.sub(r"\1", match.group(1)).replace("`", "")
        base = SLUG_DROP.sub("", text_only.lower()).replace(" ", "-")
        count = seen.get(base, 0)
        seen[base] = count + 1
        slugs.add(base if count == 0 else f"{base}-{count}")

    return slugs


def parse_repo_url(target: str) -> tuple[str, str, str, str | None] | None:
    """Split one of this repo's blob/tree URLs into (kind, ref, path, fragment)."""
    match = REPO_URL.match(target)
    if match is None:
        return None

    kind, ref, path, fragment = match.group(1), match.group(2), match.group(3), match.group(4)

    # releases/styleguide.md documents the form with literal <tag> and <prev>...<this>
    # placeholders, two of them inside fences this validator does not strip.
    if any("<" in part or ">" in part for part in (ref, path)):
        return None

    return kind, ref, path.rstrip("/"), fragment


def release_version(md_file: Path) -> str | None:
    """Give the version a releases/<v>.md file documents, or None for any other file."""
    relative = md_file.relative_to(ROOT)
    if relative.parent.as_posix() == "releases" and RELEASE_VERSION.match(relative.stem):
        return relative.stem
    return None


def iter_targets(content: str):
    """Yield (line_number, target) for every inline link and reference definition."""
    for number, line in enumerate(content.split("\n"), start=1):
        refdef = REFDEF_PATTERN.match(line)
        if refdef is not None:
            yield number, refdef.group(2)
            continue

        for match in LINK_PATTERN.finditer(line):
            yield number, match.group(2)


def resolve_target(source_file: Path, target: str) -> Path | None:
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
        source_file.relative_to(ROOT / "ops")
        in_ops = True
    except ValueError:
        in_ops = False

    if in_ops and file_target.startswith("/opt/agent-wrap/"):
        relative_path = file_target.removeprefix("/opt/agent-wrap/")
        return (ROOT / "ops" / relative_path).resolve()

    if file_target.startswith("/"):
        return None

    return (source_file.parent / file_target).resolve()


def check_anchor(
    source_file: Path, line: int, fragment: str, text: str | None, shown: str
) -> list[Violation]:
    if text is None:
        return []

    if fragment in heading_slugs(text):
        return []

    return [(source_file, line, "ML003", f"'{shown}': no heading matches #{fragment}")]


def check_repo_url(
    source_file: Path,
    line: int,
    target: str,
    tags: frozenset[str],
) -> tuple[list[Violation], str | None]:
    """
    Validate one of this repo's pinned blob/tree URLs.

    Returns the violations plus the ref that had to fall back to the working tree,
    so the caller can warn about it once per file rather than once per link.
    """
    parsed = parse_repo_url(target)
    if parsed is None:
        return [], None

    kind, ref, path, fragment = parsed
    violations: list[Violation] = []

    version = release_version(source_file)
    if version is not None and ref != version:
        violations.append(
            (
                source_file,
                line,
                "ML006",
                f"'{target}' pins {ref}, but this note documents {version}",
            )
        )
        return violations, None

    # Not a tag yet: this is the release being drafted, so its paths are whatever the
    # working tree holds. The caller turns `uncut` into one warning for the file.
    uncut = None if ref in tags else ref
    lookup: str | None = ref if uncut is None else None

    found = path_kind(lookup, path)
    if found is None:
        where = f"at {ref}" if uncut is None else "in the working tree"
        violations.append(
            (source_file, line, "ML004", f"'{target}': {path} does not exist {where}")
        )
        return violations, uncut

    if found != kind:
        named = "a file" if kind == "blob" else "a directory"
        actual = "a file" if found == "blob" else "a directory"
        violations.append(
            (source_file, line, "ML005", f"'{target}' names {named}, but {path} is {actual}")
        )

    if fragment is not None and path.endswith(".md"):
        violations.extend(check_anchor(source_file, line, fragment, read_at(lookup, path), target))

    return violations, uncut


def check_local_link(
    source_file: Path, line: int, target: str
) -> tuple[list[Violation], list[str]]:
    """Validate a link that resolves against the filesystem. Returns (violations, warnings)."""
    resolved = resolve_target(source_file, target)

    if resolved is None:
        # Reaches here only for an absolute path outside the ops special case.
        file_target = target.split("#", 1)[0]
        relative = source_file.relative_to(ROOT)
        return [], [f"WARNING: {relative}: absolute path '{file_target}'"]

    if not resolved.exists():
        message = f"broken link '{target}' -> {resolved} (does not exist)"
        return [(source_file, line, "ML001", message)], []

    fragment = target.split("#", 1)[1] if "#" in target else None
    if fragment and resolved.suffix == ".md":
        text = resolved.read_text(encoding="utf-8")
        return check_anchor(source_file, line, fragment, text, target), []

    return [], []


def validate_link(
    source_file: Path, line: int, target: str, tags: frozenset[str]
) -> tuple[list[Violation], list[str], str | None]:
    """Validate one link target. Returns (violations, warnings, uncut ref)."""
    if target.startswith(EXTERNAL_PREFIXES):
        violations, uncut = check_repo_url(source_file, line, target, tags)
        return violations, [], uncut

    # A fragment-only link points at a heading of the file it sits in.
    if target.startswith("#"):
        text = source_file.read_text(encoding="utf-8")
        return check_anchor(source_file, line, target[1:], text, target), [], None

    if target == "":
        return [], [], None

    violations, warnings = check_local_link(source_file, line, target)
    return violations, warnings, None


def get_md_files() -> list[Path]:
    md_files: list[Path] = [f for f in ROOT.iterdir() if f.is_file() and f.suffix == ".md"]

    for subdir in ("docs", "agent_wrap", "ops", "releases"):
        subdir_path = ROOT / subdir
        if subdir_path.is_dir():
            md_files.extend(subdir_path.rglob("*.md"))

    return md_files


def get_reachable_md_files(root: Path) -> set[Path]:
    """Walk the link graph from the `root`, returning every .md file reached."""
    root = root.resolve()
    visited: set[Path] = {root}
    queue: list[Path] = [root]

    while queue:
        current = queue.pop()

        for _, target in iter_targets(current.read_text(encoding="utf-8")):
            resolved = resolve_target(current, target)
            if (
                resolved is not None
                and resolved.suffix == ".md"
                and resolved.exists()
                and resolved not in visited
            ):
                visited.add(resolved)
                queue.append(resolved)

    return visited


def check_reachability(md_files: list[Path]) -> list[Violation]:
    """Return a violation for each .md file not reachable from README.md."""
    reachable = get_reachable_md_files(ROOT / "README.md")
    violations: list[Violation] = []

    for md_file in md_files:
        relative = md_file.relative_to(ROOT)
        if relative.parts[0] == "ops":
            continue
        if relative.as_posix() in EXEMPT_FROM_REACHABILITY:
            continue
        if md_file.resolve() not in reachable:
            violations.append(
                (md_file, 0, "ML002", "not reachable from README.md via any link path")
            )

    return violations


def process_file(md_file: Path, tags: frozenset[str]) -> tuple[list[Violation], list[str], int]:
    try:
        content = md_file.read_text(encoding="utf-8")
    except OSError as e:
        return [(md_file, 0, "ML001", f"could not read the file: {e}")], [], 0

    violations: list[Violation] = []
    warnings: list[str] = []
    uncut: set[str] = set()
    checked = 0

    for line, target in iter_targets(content):
        checked += 1
        link_violations, link_warnings, uncut_ref = validate_link(md_file, line, target, tags)
        violations.extend(link_violations)
        warnings.extend(link_warnings)
        if uncut_ref is not None:
            uncut.add(uncut_ref)

    relative = md_file.relative_to(ROOT)
    warnings.extend(
        f"WARNING: {relative}: tag '{ref}' is not cut yet, "
        f"so its links resolved against the working tree"
        for ref in sorted(uncut)
    )

    return violations, warnings, checked


def main() -> None:
    tags = tag_set()
    if not tags:
        print(
            "ERROR: no git tags are available, so links pinned to a tag cannot be resolved.",
            file=sys.stderr,
        )
        print(
            "Fetch them with `git fetch --tags` (in CI, check out with fetch-depth: 0).",
            file=sys.stderr,
        )
        sys.exit(2)

    md_files = get_md_files()

    if not md_files:
        print("No markdown files found to validate.")
        sys.exit(0)

    violations: list[Violation] = []
    warnings: list[str] = []
    total_checked = 0

    for md_file in md_files:
        file_violations, file_warnings, checked = process_file(md_file, tags)
        violations.extend(file_violations)
        warnings.extend(file_warnings)
        total_checked += checked

    violations.extend(check_reachability(md_files))

    for source_file, line, code, message in violations:
        relative = source_file.relative_to(ROOT)
        anchor = f"{relative}:{line}" if line else str(relative)
        print(f"{anchor}: error: {code}: {message}", file=sys.stderr)

    for message in warnings:
        print(message, file=sys.stderr)

    print(f"Checked {len(md_files)} markdown files, validated {total_checked} links.")

    if warnings:
        print(f"Warnings: {len(warnings)}")

    if violations:
        print(f"Errors: {len(violations)}", file=sys.stderr)
        sys.exit(1)

    print("All links valid.")
    sys.exit(0)


if __name__ == "__main__":
    main()

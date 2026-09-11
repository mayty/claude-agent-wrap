# This file has been created with the assistance of an AI tool.
"""Tests for scripts/validate-markdown-links.py."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent / "validate-markdown-links.py"
_spec = importlib.util.spec_from_file_location("validate_markdown_links", _SCRIPT_PATH)
assert _spec is not None
_module = importlib.util.module_from_spec(_spec)
sys.modules["validate_markdown_links"] = _module
_spec.loader.exec_module(_module)  # pyrefly: ignore [missing-attribute]

# Convenience aliases
strip_fences = _module.strip_fences
heading_slugs = _module.heading_slugs
parse_repo_url = _module.parse_repo_url
release_version = _module.release_version
iter_targets = _module.iter_targets
ref_tree = _module.ref_tree
read_at = _module.read_at
path_kind = _module.path_kind
check_repo_url = _module.check_repo_url
validate_link = _module.validate_link
tag_set = _module.tag_set
Violation = _module.Violation

TAG = "1.0.0"
TAGS = frozenset({TAG})


@pytest.fixture(autouse=True)
def _clear_caches():
    """Clear the process-wide ref/blob caches, which test order would otherwise decide."""
    ref_tree.cache_clear()
    read_at.cache_clear()
    yield
    ref_tree.cache_clear()
    read_at.cache_clear()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Build a real git repo with one tag, and point the module's ROOT at it.

    Real git rather than a mocked `git()`: what is under test is the parse of
    `git ls-tree -r -t` output, and a mock would freeze this file's assumption about
    that format instead of git's actual behaviour.
    """
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "guide.md").write_text("# Guide\n\n## Live Section\n", encoding="utf-8")

    # devnull for both config scopes: a developer's commit.gpgsign or core.hooksPath
    # must not reach this repo.
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True)

    run("init", "-q")
    run("add", "-A")
    run("-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "one")
    run("tag", TAG)

    # Move the file, so the tag and the working tree genuinely disagree.
    (root / "docs" / "guide.md").rename(root / "docs" / "moved.md")

    monkeypatch.setattr(_module, "ROOT", root)
    return root


def test_strip_fences_drops_fenced_content():
    text = dedent("""\
        # Real

        ```python
        # FORBIDDEN in stats/models.py:
        ```

        ## Also Real
        """)
    assert "FORBIDDEN" not in strip_fences(text)
    assert "# Real" in strip_fences(text)


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("# Simple Title", "simple-title"),
        ("## `AGENT_TIMEZONE` display timezone", "agent_timezone-display-timezone"),
        ("### Added: a change", "added-a-change"),
        ("## A (parenthesised) note", "a-parenthesised-note"),
        # GitHub deletes punctuation without closing the gap around it.
        ("## A — B", "a--b"),
        # The slug comes from the rendered text, so a link contributes only its label.
        ("## [0.11.0](releases/0.11.0.md)", "0110"),
    ],
)
def test_heading_slugs_matches_github(heading: str, expected: str):
    assert heading_slugs(heading) == {expected}


def test_heading_slugs_suffixes_repeats():
    text = "# Dup\n# Dup\n# Dup\n"
    assert heading_slugs(text) == {"dup", "dup-1", "dup-2"}


def test_heading_slugs_ignores_headings_inside_fences():
    text = "# Kept\n\n```sh\n# Dropped\n```\n"
    assert heading_slugs(text) == {"kept"}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://github.com/mayty/claude-agent-wrap/blob/0.6.0/docs/a.md",
            ("blob", "0.6.0", "docs/a.md", None),
        ),
        (
            "https://github.com/mayty/claude-agent-wrap/tree/0.5.0/ops",
            ("tree", "0.5.0", "ops", None),
        ),
        (
            "https://github.com/mayty/claude-agent-wrap/blob/0.9.0/docs/a.md#agent-logs",
            ("blob", "0.9.0", "docs/a.md", "agent-logs"),
        ),
    ],
)
def test_parse_repo_url_splits_pinned_urls(url: str, expected: tuple[str, str, str, str | None]):
    assert parse_repo_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        # The styleguide's literal placeholders, which document the form itself.
        "https://github.com/mayty/claude-agent-wrap/blob/<tag>/agent_wrap/README.md",
        "https://github.com/mayty/claude-agent-wrap/compare/0.9.0...0.10.0",
        "https://github.com/mayty/claude-agent-wrap/commits/0.1.0",
        "https://github.com/BerriAI/litellm",
        "https://github.com/astral-sh/python-build-standalone",
        "docs/configuration.md",
    ],
)
def test_parse_repo_url_skips_everything_else(url: str):
    assert parse_repo_url(url) is None


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("releases/0.9.0.md", "0.9.0"),
        ("releases/0.10.0.md", "0.10.0"),
        ("releases/styleguide.md", None),
        ("docs/providers.md", None),
        ("CHANGELOG.md", None),
    ],
)
def test_release_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str, expected: str | None
):
    monkeypatch.setattr(_module, "ROOT", tmp_path)
    assert release_version(tmp_path / relative) == expected


def test_iter_targets_finds_inline_and_reference_links():
    text = dedent("""\
        An [inline](docs/a.md) link.

        [label]: https://example.com/one
          [indented]: https://example.com/two
        """)
    assert list(iter_targets(text)) == [
        (1, "docs/a.md"),
        (3, "https://example.com/one"),
        (4, "https://example.com/two"),
    ]


@pytest.mark.usefixtures("repo")
def test_ref_tree_reports_object_types():
    tree = ref_tree(TAG)
    assert tree["docs/guide.md"] == "blob"
    assert tree["docs"] == "tree"


@pytest.mark.usefixtures("repo")
def test_read_at_returns_content_committed_at_the_tag():
    assert read_at(TAG, "docs/guide.md") == "# Guide\n\n## Live Section\n"


@pytest.mark.usefixtures("repo")
def test_read_at_reads_the_working_tree_for_an_uncut_ref():
    assert read_at(None, "docs/guide.md") is None
    assert read_at(None, "docs/moved.md") == "# Guide\n\n## Live Section\n"


@pytest.mark.usefixtures("repo")
def test_path_kind_distinguishes_the_tag_from_the_working_tree():
    assert path_kind(TAG, "docs/guide.md") == "blob"
    assert path_kind(None, "docs/guide.md") is None
    assert path_kind(None, "docs/moved.md") == "blob"


def _codes(violations: list[Violation]) -> list[str]:
    return [code for _, _, code, _ in violations]


def test_check_repo_url_accepts_a_path_present_at_the_tag(repo: Path):
    source = repo / "releases" / f"{TAG}.md"
    url = f"https://github.com/mayty/claude-agent-wrap/blob/{TAG}/docs/guide.md"
    violations, uncut = check_repo_url(source, 1, url, TAGS)
    assert violations == []
    assert uncut is None


def test_check_repo_url_reports_a_path_absent_at_the_tag(repo: Path):
    source = repo / "releases" / f"{TAG}.md"
    url = f"https://github.com/mayty/claude-agent-wrap/blob/{TAG}/docs/moved.md"
    violations, _ = check_repo_url(source, 1, url, TAGS)
    assert _codes(violations) == ["ML004"]


def test_check_repo_url_reports_a_blob_url_naming_a_directory(repo: Path):
    source = repo / "releases" / f"{TAG}.md"
    url = f"https://github.com/mayty/claude-agent-wrap/blob/{TAG}/docs"
    violations, _ = check_repo_url(source, 1, url, TAGS)
    assert _codes(violations) == ["ML005"]


def test_check_repo_url_resolves_an_anchor_against_the_file_at_the_tag(repo: Path):
    source = repo / "releases" / f"{TAG}.md"
    good = f"https://github.com/mayty/claude-agent-wrap/blob/{TAG}/docs/guide.md#live-section"
    bad = f"https://github.com/mayty/claude-agent-wrap/blob/{TAG}/docs/guide.md#gone"

    assert check_repo_url(source, 1, good, TAGS)[0] == []
    assert _codes(check_repo_url(source, 1, bad, TAGS)[0]) == ["ML003"]


def test_check_repo_url_rejects_a_ref_that_is_not_the_notes_own_version(repo: Path):
    source = repo / "releases" / "2.0.0.md"
    url = f"https://github.com/mayty/claude-agent-wrap/blob/{TAG}/docs/guide.md"
    violations, _ = check_repo_url(source, 1, url, TAGS)
    assert _codes(violations) == ["ML006"]


def test_check_repo_url_falls_back_to_the_working_tree_for_an_uncut_tag(repo: Path):
    source = repo / "releases" / "2.0.0.md"
    url = "https://github.com/mayty/claude-agent-wrap/blob/2.0.0/docs/moved.md"
    violations, uncut = check_repo_url(source, 1, url, TAGS)

    # docs/moved.md exists only in the working tree, which is exactly what an
    # unreleased note must be checked against.
    assert violations == []
    assert uncut == "2.0.0"


def test_check_repo_url_reports_an_uncut_tag_link_missing_from_the_working_tree(repo: Path):
    source = repo / "releases" / "2.0.0.md"
    url = "https://github.com/mayty/claude-agent-wrap/blob/2.0.0/docs/guide.md"
    violations, uncut = check_repo_url(source, 1, url, TAGS)

    assert _codes(violations) == ["ML004"]
    assert uncut == "2.0.0"
    assert "working tree" in violations[0][3]


def test_validate_link_reports_a_broken_relative_link(repo: Path):
    source = repo / "docs" / "moved.md"
    violations, warnings, _ = validate_link(source, 3, "nowhere.md", TAGS)
    assert _codes(violations) == ["ML001"]
    assert warnings == []


def test_validate_link_checks_a_fragment_only_link_against_its_own_file(repo: Path):
    source = repo / "docs" / "moved.md"
    assert validate_link(source, 1, "#live-section", TAGS)[0] == []
    assert _codes(validate_link(source, 1, "#missing", TAGS)[0]) == ["ML003"]


def test_validate_link_warns_on_an_absolute_path(repo: Path):
    source = repo / "docs" / "moved.md"
    violations, warnings, _ = validate_link(source, 1, "/etc/hosts", TAGS)
    assert violations == []
    assert "absolute path" in warnings[0]


def test_tag_set_is_empty_without_a_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_module, "ROOT", tmp_path)
    assert tag_set() == frozenset()

# This file has been created with the assistance of an AI tool.
"""Tests for scripts/sync-dependencies.py."""

import importlib.util
import io
import sys
from pathlib import Path
from textwrap import dedent

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent / "sync-dependencies.py"
_spec = importlib.util.spec_from_file_location("sync_dependencies", _SCRIPT_PATH)
assert _spec is not None
_module = importlib.util.module_from_spec(_spec)
sys.modules["sync_dependencies"] = _module
_spec.loader.exec_module(_module)  # pyrefly: ignore [missing-attribute]

# Convenience aliases
canonical = _module.canonical
parse_tree = _module.parse_tree
rewrite_body = _module.rewrite_body
update_dependencies = _module.update_dependencies
main = _module.main

PYPROJECT_TEXT = dedent(
    """\
    [project]
    name = "demo"
    # First-degree runtime dependencies, as ranges.
    dependencies = [
        "httpx2>=2.12",
    ]

    [tool.uv]
    package = false

    [dependency-groups]
    dev = [
        # tooling
        "pytest>=9.1.1,<10",
        "pytest-cov==7.1.0",
        "ruff",
        "hand-rolled>=1.0",
    ]
    """
)

PROD_TREE = dedent(
    """\
    demo v0.0.0
    └── httpx2 v2.12.3
        └── idna v3.10
    """
)

DEV_TREE = dedent(
    """\
    demo v0.0.0
    ├── pytest v9.2.0
    ├── pytest-cov v7.1.0
    │   └── coverage v7.6.1
    └── ruff v0.16.4
    """
)


@pytest.fixture
def pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(PYPROJECT_TEXT, encoding="utf-8")
    monkeypatch.setattr(_module, "PYPROJECT", path)
    return path


def test_canonical_normalizes_separators_and_case() -> None:
    assert canonical("Pytest_Cov") == "pytest-cov"
    assert canonical("zope.interface") == "zope-interface"


def test_parse_tree_keeps_only_direct_dependencies() -> None:
    assert parse_tree(PROD_TREE) == {"httpx2": "2.12.3"}


def test_parse_tree_reads_every_direct_dependency() -> None:
    assert parse_tree(DEV_TREE) == {
        "pytest": "9.2.0",
        "pytest-cov": "7.1.0",
        "ruff": "0.16.4",
    }


def test_parse_tree_strips_extras() -> None:
    assert parse_tree("└── uvicorn[standard] v0.30.0\n") == {"uvicorn": "0.30.0"}


def test_parse_tree_canonicalizes_names() -> None:
    assert parse_tree("└── Pytest_Cov v7.1.0\n") == {"pytest-cov": "7.1.0"}


def test_parse_tree_ignores_lines_without_a_version() -> None:
    assert parse_tree("└── httpx2\n") == {}


def test_rewrite_body_reports_the_versions_that_moved() -> None:
    body, changes = rewrite_body('    "pytest>=9.1.1,<10",', {"pytest": "9.2.0"})
    assert body == '    "pytest>=9.2.0",'
    assert changes == [("pytest", "9.1.1", "9.2.0")]


def test_rewrite_body_normalizes_the_operator() -> None:
    body, changes = rewrite_body('    "pytest-cov==7.1.0",', {"pytest-cov": "7.1.0"})
    assert body == '    "pytest-cov>=7.1.0",'
    assert changes == []


def test_rewrite_body_gives_an_unconstrained_requirement_a_floor() -> None:
    body, changes = rewrite_body('    "ruff",', {"ruff": "0.16.4"})
    assert body == '    "ruff>=0.16.4",'
    assert changes == [("ruff", "", "0.16.4")]


def test_rewrite_body_preserves_extras() -> None:
    body, _ = rewrite_body('    "uvicorn[standard]>=0.29",', {"uvicorn": "0.30.0"})
    assert body == '    "uvicorn[standard]>=0.30.0",'


def test_rewrite_body_leaves_unknown_and_non_requirement_lines_alone() -> None:
    original = '    # tooling\n\n    "hand-rolled>=1.0",'
    body, changes = rewrite_body(original, {"pytest": "9.2.0"})
    assert body == original
    assert changes == []


def test_update_dependencies_rewrites_the_prod_array(pyproject: Path) -> None:
    assert update_dependencies("prod", PROD_TREE) == 0
    assert '"httpx2>=2.12.3",' in pyproject.read_text(encoding="utf-8")


def test_update_dependencies_leaves_the_dev_array_alone_for_prod(pyproject: Path) -> None:
    update_dependencies("prod", PROD_TREE)
    assert '"pytest>=9.1.1,<10",' in pyproject.read_text(encoding="utf-8")


def test_update_dependencies_rewrites_the_dev_group(pyproject: Path) -> None:
    assert update_dependencies("dev", DEV_TREE) == 0
    text = pyproject.read_text(encoding="utf-8")
    assert '"pytest>=9.2.0",' in text
    assert '"pytest-cov>=7.1.0",' in text
    assert '"ruff>=0.16.4",' in text
    assert '"hand-rolled>=1.0",' in text
    assert '"httpx2>=2.12",' in text


def test_update_dependencies_preserves_surrounding_content(pyproject: Path) -> None:
    update_dependencies("dev", DEV_TREE)
    text = pyproject.read_text(encoding="utf-8")
    assert "# tooling" in text
    assert "[tool.uv]\npackage = false" in text
    assert text.endswith("]\n")


@pytest.mark.usefixtures("pyproject")
def test_update_dependencies_reports_each_change(capsys: pytest.CaptureFixture[str]) -> None:
    update_dependencies("dev", DEV_TREE)
    out = capsys.readouterr().out
    assert "dev dependencies updated" in out
    assert "pytest 9.1.1 -> 9.2.0" in out
    assert "ruff (none) -> 0.16.4" in out


def test_update_dependencies_does_not_rewrite_an_array_already_in_step(
    pyproject: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    update_dependencies("prod", PROD_TREE)
    before = pyproject.read_text(encoding="utf-8")
    capsys.readouterr()

    assert update_dependencies("prod", PROD_TREE) == 0
    assert pyproject.read_text(encoding="utf-8") == before
    assert "prod dependencies up to date" in capsys.readouterr().out


def test_update_dependencies_rejects_an_empty_tree(pyproject: Path) -> None:
    assert update_dependencies("prod", "demo v0.0.0\n") == 1
    assert pyproject.read_text(encoding="utf-8") == PYPROJECT_TEXT


def test_update_dependencies_rejects_a_missing_array(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "demo"\n', encoding="utf-8")
    monkeypatch.setattr(_module, "PYPROJECT", path)

    assert update_dependencies("prod", PROD_TREE) == 1


def test_main_syncs_the_section_named_on_the_command_line(
    pyproject: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(PROD_TREE))

    assert main(["prod"]) == 0
    assert '"httpx2>=2.12.3",' in pyproject.read_text(encoding="utf-8")


def test_main_rejects_an_unknown_section(pyproject: Path) -> None:
    assert main(["docs"]) == 1
    assert pyproject.read_text(encoding="utf-8") == PYPROJECT_TEXT


def test_main_rejects_a_missing_argument(pyproject: Path) -> None:
    assert main([]) == 1
    assert pyproject.read_text(encoding="utf-8") == PYPROJECT_TEXT

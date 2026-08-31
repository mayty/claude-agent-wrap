# This file has been created with the assistance of an AI tool.
#
# Validates architectural rules for the agent-wrap codebase via AST analysis.
#
# Rules enforced:
#   EA001 — Runtime cross-domain import: a file under agent_wrap/domain/
#           imports from a different domain subpackage outside of a
#           `if TYPE_CHECKING:` guard.
#   EB001 — Private-name import: `from <module> import <_prefixed_name>`
#           outside of test files.  Also catches `import X._private_module`
#           and `from X._private_module import Y` (private module in path).
#   EC001 — LiteLLM runtime agent_wrap import: files under litellm_runtime/
#           must not import from agent_wrap outside of a `if TYPE_CHECKING:`
#           guard.
#   ED001 — Misplaced type: a dataclass / NamedTuple / TypedDict defined
#           outside its package's `models.py` (architecture rule 8).
#   EE001 — Misplaced constant: an UPPER_CASE module-level assignment outside
#           its package's `constants.py` (architecture rule 10).
#   EF001 — Misplaced enum: an Enum class defined outside its package's
#           `constants.py` (architecture rule 10). Applies to the same scope
#           as ED001/EE001, including the package root.
#
# Usage: python3 scripts/validate-architecture.py
#
# Exit codes:
#   0 — no violations
#   1 — one or more violations found

from __future__ import annotations

import ast
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
DOMAIN_DIR = ROOT / "agent_wrap" / "domain"
PACKAGE_PREFIX = "agent_wrap.domain."

# Directories to skip during file discovery.
SKIP_DIRS = frozenset({"__pycache__", ".pytest_cache", ".git"})

# Paths entirely excluded from all checks (relative to project root).
EXCLUDED_PATHS: tuple[str, ...] = ()

# litellm_runtime directory (plain directory, no __init__.py) — files here
# are mounted into the LiteLLM sidecar and must not depend on agent_wrap at
# runtime.  Rule EC001 enforces this.
_LITELLM_RUNTIME = "agent_wrap/domain/providers/litellm_runtime"


def _discover_subpackages(domain_dir: Path) -> tuple[str, ...]:
    """
    Discover domain subpackages by scanning for ``__init__.py`` files.

    Returns subpackage keys sorted longest-first so prefix matching picks
    the most specific match.
    """
    found: list[str] = []

    for entry in sorted(domain_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        if entry.name in SKIP_DIRS or not (entry / "__init__.py").exists():
            continue

        if entry.name == "providers":
            found.append("providers")
            _collect_provider_subpackages(entry, found)
        else:
            found.append(entry.name)

    found.sort(key=lambda s: (-len(s), s))  # pyrefly: ignore [implicit-any-lambda]
    return tuple(found)


def _collect_provider_subpackages(providers_dir: Path, found: list[str]) -> None:
    """Append nested provider subpackage keys to *found*."""
    for prov_entry in sorted(providers_dir.iterdir()):
        if not prov_entry.is_dir():
            continue
        if prov_entry.name.startswith("_") or prov_entry.name in SKIP_DIRS:
            continue
        if prov_entry.name == "tests":
            continue
        if (prov_entry / "__init__.py").exists():
            found.append(f"providers.{prov_entry.name}")


# Discovered at import time from the real filesystem.  Tests that redirect
# DOMAIN_DIR can monkeypatch this tuple directly.
KNOWN_SUBPACKAGES: tuple[str, ...] = _discover_subpackages(DOMAIN_DIR)


# ---------------------------------------------------------------------------
# Subpackage key resolution
# ---------------------------------------------------------------------------


def source_subpackage_key(file_path: Path) -> str | None:
    """
    Return the domain subpackage key for *file_path*, or *None* if the file
    is not under ``agent_wrap/domain/`` (Rule A does not apply).
    """
    try:
        rel = file_path.relative_to(DOMAIN_DIR)
    except ValueError:
        return None

    parts = rel.parts
    if not parts:
        return None

    # Nested providers: providers/<name>/...  ->  providers.<name>
    # Check whether providers/<second> is itself a subpackage.
    if parts[0] == "providers" and len(parts) > 1:
        candidate = f"providers.{parts[1]}"
        if candidate in KNOWN_SUBPACKAGES:
            return candidate

    return parts[0]


def target_subpackage_key(module_path: str) -> str | None:
    """
    Return the subpackage key for an import target module path.

    *module_path* is the dotted path *after* ``agent_wrap.domain.``
    (e.g. ``build.project_utils`` or ``providers.litellm_bedrock.provider``).
    """
    for subpkg in KNOWN_SUBPACKAGES:
        if module_path == subpkg or module_path.startswith(subpkg + "."):
            return subpkg
    return None


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _build_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """Build a ``child -> parent`` mapping for every node in *tree*."""
    parent_map: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[child] = parent
    return parent_map


def _test_references_type_checking(test_node: ast.expr) -> bool:
    """Return *True* if *test_node* references ``TYPE_CHECKING``."""
    if isinstance(test_node, ast.Name) and test_node.id == "TYPE_CHECKING":
        return True
    # Compound condition:  if TYPE_CHECKING and ...:
    if isinstance(test_node, ast.BoolOp):
        return any(isinstance(v, ast.Name) and v.id == "TYPE_CHECKING" for v in test_node.values)
    return False


def _is_type_checking_guarded(node: ast.AST, parent_map: dict[ast.AST, ast.AST]) -> bool:
    """Return *True* if *node* sits inside ``if TYPE_CHECKING:``."""
    current = node
    while current in parent_map:
        parent = parent_map[current]
        if isinstance(parent, ast.If) and _test_references_type_checking(parent.test):
            return True
        current = parent
    return False


def _resolve_relative_import(node: ast.ImportFrom, file_path: Path) -> str | None:
    """
    Resolve a relative import to an absolute dotted module path.

    Returns *None* when resolution fails (invalid level for the path).
    """
    # Absolute imports — not our concern here.
    if node.level == 0:
        return node.module

    try:
        rel = file_path.relative_to(ROOT)
    except ValueError:
        return None

    parts = list(rel.with_suffix("").parts)

    # node.level tells how many segments to strip from the end of the
    # *file's own* module path before appending *node.module*.
    base_len = len(parts) - node.level
    if base_len < 0:
        return None

    base_parts = parts[:base_len] if base_len > 0 else []
    if node.module:
        base_parts.extend(node.module.split("."))

    return ".".join(base_parts)


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


def _is_excluded(rel_path: str) -> bool:
    """Return *True* if *rel_path* should be excluded from all checks."""
    return any(rel_path.startswith(ex) for ex in EXCLUDED_PATHS)


def _is_test_file(file_path: Path) -> bool:
    """Return *True* if *file_path* lives under a ``tests/`` directory."""
    return "tests" in file_path.parts


def _is_litellm_runtime(file_path: Path) -> bool:
    """Return *True* if *file_path* is under the litellm_runtime directory."""
    try:
        rel = str(file_path.relative_to(ROOT))
    except ValueError:
        return False
    return rel.startswith(_LITELLM_RUNTIME)


def _find_python_files(root_dir: Path) -> list[Path]:
    """Yield every ``.py`` file under *root_dir*, respecting exclusions."""
    files: list[Path] = []
    for py_file in root_dir.rglob("*.py"):
        parts = frozenset(py_file.parts)
        if parts & SKIP_DIRS:
            continue
        try:
            rel = str(py_file.relative_to(ROOT))
        except ValueError:
            continue
        if _is_excluded(rel):
            continue
        files.append(py_file)
    return sorted(files)


# ---------------------------------------------------------------------------
# Rule checks
# ---------------------------------------------------------------------------


def _check_rule_a(
    file_path: Path,
    tree: ast.AST,
    parent_map: dict[ast.AST, ast.AST],
    source_key: str,
) -> list[tuple[str, int, str, str]]:
    """Return EA001 violations found in *tree*."""
    violations: list[tuple[str, int, str, str]] = []
    rel_file = str(file_path.relative_to(ROOT))

    for node in ast.walk(tree):
        if _is_type_checking_guarded(node, parent_map):
            continue

        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_module_for_cross_domain(
                    violations, alias.name, rel_file, node.lineno, source_key
                )

        elif isinstance(node, ast.ImportFrom):
            # Absolute import:  from agent_wrap.domain.foo.bar import X
            if node.level == 0 and node.module is not None:
                _check_prefix_module_for_cross_domain(
                    violations, node.module, rel_file, node.lineno, source_key
                )
            # Relative import within domain
            elif node.level > 0:
                resolved = _resolve_relative_import(node, file_path)
                if resolved:
                    _check_prefix_module_for_cross_domain(
                        violations, resolved, rel_file, node.lineno, source_key
                    )

    return violations


def _check_module_for_cross_domain(
    violations: list[tuple[str, int, str, str]],
    module_name: str,
    rel_file: str,
    lineno: int,
    source_key: str,
) -> None:
    """Flag *module_name* if it crosses domain subpackage boundaries."""
    if not module_name.startswith(PACKAGE_PREFIX):
        return
    mod_path = module_name[len(PACKAGE_PREFIX) :]
    target_key = target_subpackage_key(mod_path)
    if target_key is not None and target_key != source_key:
        # Allow child→parent (ancestor) imports — e.g. providers.litellm_bedrock
        # importing from providers is a child importing from its parent package.
        if source_key.startswith(target_key + "."):
            return
        violations.append(
            (
                rel_file,
                lineno,
                "EA001",
                f"runtime cross-domain import of '{module_name}' ({source_key} -> {target_key})",
            )
        )


def _check_prefix_module_for_cross_domain(
    violations: list[tuple[str, int, str, str]],
    module_name: str,
    rel_file: str,
    lineno: int,
    source_key: str,
) -> None:
    """Flag a ``from module_name import ...`` that crosses subpackage boundaries."""
    if not module_name.startswith(PACKAGE_PREFIX):
        return
    mod_path = module_name[len(PACKAGE_PREFIX) :]
    target_key = target_subpackage_key(mod_path)
    if target_key is not None and target_key != source_key:
        # Allow child→parent (ancestor) imports — e.g. providers.litellm_bedrock
        # importing from providers is a child importing from its parent package.
        if source_key.startswith(target_key + "."):
            return
        violations.append(
            (
                rel_file,
                lineno,
                "EA001",
                f"runtime cross-domain import from '{module_name}' ({source_key} -> {target_key})",
            )
        )


def _check_rule_b(
    file_path: Path,
    tree: ast.AST,
) -> list[tuple[str, int, str, str]]:
    """Return EB001 violations found in *tree*."""
    violations: list[tuple[str, int, str, str]] = []
    rel_file = str(file_path.relative_to(ROOT))

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            violations.extend(
                (
                    rel_file,
                    node.lineno,
                    "EB001",
                    f"private name import '{alias.name}' from '{node.module or '.'}'",
                )
                for alias in node.names
                if alias.name.startswith("_")
            )
            # Also flag imports whose module *path* contains a _-prefixed segment,
            # e.g. ``from agent_wrap.domain.secrets._store import EncryptedFileStore``.
            # Exclude double-underscore (dunder) names like ``__future__`` — those
            # are standard Python convention, not private modules.
            if node.module is not None:
                for segment in node.module.split("."):
                    if segment.startswith("_") and not segment.startswith("__"):
                        violations.append(
                            (
                                rel_file,
                                node.lineno,
                                "EB001",
                                f"private module import from '{node.module}'",
                            )
                        )
                        break

        elif isinstance(node, ast.Import):
            violations.extend(
                (rel_file, node.lineno, "EB001", f"private name import of module '{alias.name}'")
                for alias in node.names
                if alias.name.rsplit(".", 1)[-1].startswith("_")
            )

    return violations


def _check_rule_c(
    file_path: Path,
    tree: ast.AST,
    parent_map: dict[ast.AST, ast.AST],
) -> list[tuple[str, int, str, str]]:
    """
    Return EC001 violations found in *tree*.

    Flags any ``import agent_wrap`` or ``from agent_wrap ...`` that is
    **not** inside an ``if TYPE_CHECKING:`` guard.  Only applies to files
    under ``litellm_runtime/``.
    """
    violations: list[tuple[str, int, str, str]] = []
    rel_file = str(file_path.relative_to(ROOT))

    for node in ast.walk(tree):
        if _is_type_checking_guarded(node, parent_map):
            continue

        if isinstance(node, ast.Import):
            violations.extend(
                (
                    rel_file,
                    node.lineno,
                    "EC001",
                    f"runtime agent_wrap import forbidden in litellm_runtime: '{alias.name}'",
                )
                for alias in node.names
                if alias.name == "agent_wrap" or alias.name.startswith("agent_wrap.")
            )

        elif (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and (node.module == "agent_wrap" or node.module.startswith("agent_wrap."))
        ):
            violations.append(
                (
                    rel_file,
                    node.lineno,
                    "EC001",
                    (
                        f"runtime agent_wrap import forbidden in litellm_runtime: "
                        f"from '{node.module}' import ..."
                    ),
                )
            )

    return violations


def _is_type_carrying_class(node: ast.ClassDef) -> bool:
    """
    Whether *node* declares a data/type-carrying class rather than behaviour.

    Recognises the three forms rule 8 names: a ``@dataclass``, a ``NamedTuple``, and a
    ``TypedDict``. A plain class is behaviour and is not flagged — "plain data-holding
    class" cannot be told from a service by AST alone. Enums are a separate case (see
    ``_is_enum_class``) — they belong in ``constants.py``, not ``models.py``.
    """
    for base in node.bases:
        name = ast.unparse(base).rsplit(".", 1)[-1]
        if name in ("NamedTuple", "TypedDict"):
            return True
    return any(
        ast.unparse(dec).rsplit(".", 1)[-1].partition("(")[0] == "dataclass"
        for dec in node.decorator_list
    )


def _is_enum_class(node: ast.ClassDef) -> bool:
    """
    Whether *node* declares an ``Enum`` (including ``IntEnum``/``StrEnum`` variants and
    the ``str, Enum`` mixin form, qualified or not, e.g. ``enum.IntEnum``).
    """
    return any(ast.unparse(base).rsplit(".", 1)[-1].endswith("Enum") for base in node.bases)


def _check_rule_d(file_path: Path, tree: ast.AST) -> list[tuple[str, int, str, str]]:
    """
    Return ED001 violations found in *tree*.

    Rule 8: a data/type-carrying class belongs in its package's ``models.py``. Applies
    to ``agent_wrap/domain/``, ``agent_wrap/cli/``, and the package root — ``lib/`` is
    standalone general-purpose code that does not follow the models.py convention.
    """
    rel_file = str(file_path.relative_to(ROOT))
    return [
        (
            rel_file,
            node.lineno,
            "ED001",
            f"data/type-carrying class '{node.name}' must live in this package's models.py",
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and _is_type_carrying_class(node)
    ]


def _check_rule_f(file_path: Path, tree: ast.AST) -> list[tuple[str, int, str, str]]:
    """
    Return EF001 violations found in *tree*.

    Rule 10: an enum belongs in its package's ``constants.py`` — a fixed set of named
    values, not a data-carrying type. Applies wherever rule D applies (see
    ``_models_constants_scope``), including the package root.
    """
    rel_file = str(file_path.relative_to(ROOT))
    return [
        (
            rel_file,
            node.lineno,
            "EF001",
            f"enum '{node.name}' must live in this package's constants.py",
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and _is_enum_class(node)
    ]


def _check_rule_e(file_path: Path, tree: ast.AST) -> list[tuple[str, int, str, str]]:
    """
    Return EE001 violations found in *tree*.

    Rule 10: a module-level constant belongs in its package's ``constants.py``. A
    constant is an UPPER_CASE (or ``_UPPER_CASE``) module-level assignment.
    ``USAGE``/``SUMMARY`` are exempt: ``cli/commands.py`` reads them reflectively off
    each command's ``run`` module by name, so they cannot move.
    """
    violations: list[tuple[str, int, str, str]] = []
    rel_file = str(file_path.relative_to(ROOT))
    exempt = {"USAGE", "SUMMARY"}

    for node in tree.body if isinstance(tree, ast.Module) else []:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]

        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            bare = target.id.lstrip("_")
            if bare in exempt or not bare.isupper() or not bare.replace("_", "").isalnum():
                continue
            violations.append(
                (
                    rel_file,
                    node.lineno,
                    "EE001",
                    (
                        f"module-level constant '{target.id}' must live in this package's "
                        f"constants.py"
                    ),
                )
            )

    return violations


def _models_constants_scope(file_path: Path) -> str | None:
    """
    Return "models" / "constants" when *file_path* is subject to rule 8 / 10.

    Both rules apply under ``agent_wrap/domain/``, ``agent_wrap/cli/``, and the
    package root itself (``agent_wrap/*.py``, not recursing into subdirectories) —
    skipping the file that is itself the designated home, test files, and
    ``litellm_runtime/`` (a plain directory mounted into the sidecar, exempt by
    convention). ``lib/`` has no models.py/constants.py convention and stays excluded.
    """
    try:
        rel = file_path.relative_to(ROOT / "agent_wrap")
    except ValueError:
        return None
    if len(rel.parts) > 1 and rel.parts[0] not in ("domain", "cli"):
        return None
    if _is_test_file(file_path) or _is_litellm_runtime(file_path):
        return None
    if file_path.name == "models.py":
        return "constants"
    if file_path.name == "constants.py":
        return "models"
    return "both"


def check_file(file_path: Path) -> list[tuple[str, int, str, str]]:
    """Run both architecture rules against a single ``.py`` file."""
    violations: list[tuple[str, int, str, str]] = []

    # Respect exclusion list even when called directly.
    try:
        rel = str(file_path.relative_to(ROOT))
    except ValueError:
        return violations
    if _is_excluded(rel):
        return violations

    try:
        source = file_path.read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return violations

    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return violations

    parent_map = _build_parent_map(tree)
    source_key = source_subpackage_key(file_path)

    # Rule A only applies to files inside agent_wrap/domain/ (excludes test files,
    # consistent with EB001's treatment — tests are not runtime production code).
    if source_key is not None and not _is_test_file(file_path):
        violations.extend(_check_rule_a(file_path, tree, parent_map, source_key))

    # Rule B applies to all files except tests.
    if not _is_test_file(file_path):
        violations.extend(_check_rule_b(file_path, tree))

    # Rule C: litellm_runtime files must not import from agent_wrap at runtime.
    if _is_litellm_runtime(file_path):
        violations.extend(_check_rule_c(file_path, tree, parent_map))

    # Rules D/E/F: types belong in models.py, constants (incl. enums) in constants.py.
    scope = _models_constants_scope(file_path)
    if scope in ("models", "both"):
        violations.extend(_check_rule_d(file_path, tree))
    if scope in ("constants", "both"):
        violations.extend(_check_rule_e(file_path, tree))
        violations.extend(_check_rule_f(file_path, tree))

    return violations


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    violations: list[tuple[str, int, str, str]] = []
    python_files = _find_python_files(ROOT / "agent_wrap")

    for fp in python_files:
        violations.extend(check_file(fp))

    if violations:
        for rel_path, line, code, msg in violations:
            print(f"{rel_path}:{line}: error: {code}: {msg}", file=sys.stderr)

        parts: list[str] = []
        for code in ("EA001", "EB001", "EC001", "ED001", "EE001", "EF001"):
            count = sum(1 for v in violations if v[2] == code)
            if count:
                parts.append(f"{count} {code}")

        print(
            f"\n{len(violations)} architecture violations found ({', '.join(parts)})",
            file=sys.stderr,
        )
        sys.exit(1)

    print("No architecture violations found.")
    sys.exit(0)


if __name__ == "__main__":
    main()

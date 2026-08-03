# This file has been created with the assistance of an AI tool.
"""Tests for scripts/validate-architecture.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from textwrap import dedent

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent / "validate-architecture.py"
_spec = importlib.util.spec_from_file_location("validate_architecture", _SCRIPT_PATH)
assert _spec is not None
_module = importlib.util.module_from_spec(_spec)
sys.modules["validate_architecture"] = _module
_spec.loader.exec_module(_module)  # type: ignore[arg-type]

# Convenience aliases
source_subpackage_key = _module.source_subpackage_key  # type: ignore[attr-defined]
target_subpackage_key = _module.target_subpackage_key  # type: ignore[attr-defined]
check_file = _module.check_file  # type: ignore[attr-defined]
DOMAIN_DIR = _module.DOMAIN_DIR  # type: ignore[attr-defined]
ROOT = _module.ROOT  # type: ignore[attr-defined]


def test_source_subpackage_key_top_level() -> None:
    p = DOMAIN_DIR / "build" / "project_utils.py"
    assert source_subpackage_key(p) == "build"


def test_source_subpackage_key_nested_provider() -> None:
    p = DOMAIN_DIR / "providers" / "litellm_bedrock" / "provider.py"
    assert source_subpackage_key(p) == "providers.litellm_bedrock"


def test_source_subpackage_key_top_level_providers() -> None:
    p = DOMAIN_DIR / "providers" / "discovery.py"
    assert source_subpackage_key(p) == "providers"


def test_source_subpackage_key_test_file_inherits_parent() -> None:
    p = DOMAIN_DIR / "stats" / "tests" / "test_scan.py"
    assert source_subpackage_key(p) == "stats"


def test_source_subpackage_key_nested_test_file_inherits_nested() -> None:
    p = DOMAIN_DIR / "providers" / "litellm_bedrock" / "tests" / "test_bedrock.py"
    assert source_subpackage_key(p) == "providers.litellm_bedrock"


def test_source_subpackage_key_outside_domain_returns_none() -> None:
    p = ROOT / "agent_wrap" / "cli" / "stats" / "render.py"
    assert source_subpackage_key(p) is None


def test_source_subpackage_key_unknown_provider_nested_dir_not_flagged() -> None:
    """Only known NESTED_PROVIDERS get special treatment."""
    p = DOMAIN_DIR / "providers" / "something_else" / "foo.py"
    assert source_subpackage_key(p) == "providers"


def test_target_subpackage_key_top_level() -> None:
    assert target_subpackage_key("build.project_utils") == "build"


def test_target_subpackage_key_nested_provider() -> None:
    assert (
        target_subpackage_key("providers.litellm_bedrock.provider") == "providers.litellm_bedrock"
    )


def test_target_subpackage_key_parent_providers() -> None:
    assert target_subpackage_key("providers.discovery") == "providers"


def test_target_subpackage_key_exact_match() -> None:
    assert target_subpackage_key("build") == "build"


def test_target_subpackage_key_non_domain_module_returns_none() -> None:
    assert target_subpackage_key("lib.format") is None


class _Maker:
    """Helper to write .py files into a temp directory tree."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def write(self, rel: str, content: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(dedent(content), encoding="utf-8")
        return p


def _violation_codes(violations: list[tuple[str, int, str, str]]) -> set[str]:
    return {v[2] for v in violations}


@pytest.fixture
def make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Maker:
    """Redirect the script's ROOT and DOMAIN_DIR into *tmp_path*."""
    fake_root = tmp_path / "project"
    fake_root.mkdir()
    (fake_root / "agent_wrap" / "domain").mkdir(parents=True)
    monkeypatch.setattr(_module, "ROOT", fake_root)
    monkeypatch.setattr(
        _module,
        "DOMAIN_DIR",
        fake_root / "agent_wrap" / "domain",
    )
    return _Maker(fake_root)


def test_rule_a_same_subpackage_allowed_direct_import(make: _Maker) -> None:
    make.write(
        "agent_wrap/domain/build/foo.py",
        """\
        from agent_wrap.domain.build.bar import helper
        """,
    )
    fp = make.root / "agent_wrap" / "domain" / "build" / "foo.py"
    assert _violation_codes(check_file(fp)) == set()


def test_rule_a_cross_subpackage_violation_absolute_import(make: _Maker) -> None:
    make.write(
        "agent_wrap/domain/stats/cost.py",
        """\
        from agent_wrap.domain.pricing.models import Bucket
        """,
    )
    fp = make.root / "agent_wrap" / "domain" / "stats" / "cost.py"
    violations = check_file(fp)
    assert "EA001" in _violation_codes(violations)


def test_rule_a_cross_subpackage_violation_relative_import_cross_subpackage(
    make: _Maker,
) -> None:
    make.write(
        "agent_wrap/domain/stats/cost.py",
        """\
        from ..pricing.models import Bucket
        """,
    )
    fp = make.root / "agent_wrap" / "domain" / "stats" / "cost.py"
    violations = check_file(fp)
    assert "EA001" in _violation_codes(violations)


def test_rule_a_cross_subpackage_violation_import_statement(make: _Maker) -> None:
    """Plain ``import agent_wrap.domain.foo.bar`` is also checked."""
    make.write(
        "agent_wrap/domain/stats/cost.py",
        """\
        import agent_wrap.domain.pricing.models
        """,
    )
    fp = make.root / "agent_wrap" / "domain" / "stats" / "cost.py"
    violations = check_file(fp)
    assert "EA001" in _violation_codes(violations)


def test_rule_a_type_checking_guard_allowed_simple_guard(make: _Maker) -> None:
    make.write(
        "agent_wrap/domain/stats/cost.py",
        """\
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            from agent_wrap.domain.pricing.models import Bucket
        """,
    )
    fp = make.root / "agent_wrap" / "domain" / "stats" / "cost.py"
    assert _violation_codes(check_file(fp)) == set()


def test_rule_a_type_checking_guard_allowed_compound_guard(make: _Maker) -> None:
    make.write(
        "agent_wrap/domain/stats/cost.py",
        """\
        from typing import TYPE_CHECKING
        if TYPE_CHECKING and False:
            from agent_wrap.domain.pricing.models import Bucket
        """,
    )
    fp = make.root / "agent_wrap" / "domain" / "stats" / "cost.py"
    assert _violation_codes(check_file(fp)) == set()


def test_rule_a_type_checking_guard_allowed_runtime_import_same_subpackage_still_allowed(
    make: _Maker,
) -> None:
    """Within-subpackage runtime import is fine regardless."""
    make.write(
        "agent_wrap/domain/stats/cost.py",
        """\
        from agent_wrap.domain.stats.scan import scan_dirs
        """,
    )
    fp = make.root / "agent_wrap" / "domain" / "stats" / "cost.py"
    assert _violation_codes(check_file(fp)) == set()


def test_rule_a_provider_nesting_provider_import_from_sibling_is_cross(make: _Maker) -> None:
    """One nested provider reaching into another is a cross-subpackage import."""
    make.write(
        "agent_wrap/domain/providers/litellm_bedrock/provider.py",
        """\
        from agent_wrap.domain.providers.litellm_dashscope.provider import DashscopeProvider
        """,
    )
    fp = make.root / "agent_wrap" / "domain" / "providers" / "litellm_bedrock" / "provider.py"
    violations = check_file(fp)
    assert "EA001" in _violation_codes(violations)


def test_rule_a_provider_nesting_provider_import_from_parent_providers_is_allowed(
    make: _Maker,
) -> None:
    """
    A nested provider importing from its parent ``providers`` package is allowed.

    This is a child -> ancestor import, which ``_check_prefix_module_for_cross_domain``
    exempts explicitly — every provider subclasses ``providers.base.Provider``.
    """
    make.write(
        "agent_wrap/domain/providers/litellm_bedrock/provider.py",
        """\
        from agent_wrap.domain.providers.base import Provider
        """,
    )
    fp = make.root / "agent_wrap" / "domain" / "providers" / "litellm_bedrock" / "provider.py"
    assert "EA001" not in _violation_codes(check_file(fp))


def test_rule_b_private_name_import_violation_from_import_private(make: _Maker) -> None:
    make.write(
        "agent_wrap/domain/logs/io.py",
        """\
        from agent_wrap.domain.logs.models import _SessionMeta
        """,
    )
    fp = make.root / "agent_wrap" / "domain" / "logs" / "io.py"
    violations = check_file(fp)
    assert "EB001" in _violation_codes(violations)


def test_rule_b_private_name_import_violation_import_private_module(make: _Maker) -> None:
    """``import foo._bar`` should also be flagged."""
    make.write(
        "agent_wrap/lib/utils.py",
        """\
        import agent_wrap.domain.logs._internal
        """,
    )
    fp = make.root / "agent_wrap" / "lib" / "utils.py"
    violations = check_file(fp)
    assert "EB001" in _violation_codes(violations)


def test_rule_b_test_files_allowed_private_import_in_test_dir(make: _Maker) -> None:
    make.write(
        "agent_wrap/domain/updates/tests/test_updates.py",
        """\
        from agent_wrap.domain.updates.updates import _GitOps
        """,
    )
    fp = make.root / "agent_wrap" / "domain" / "updates" / "tests" / "test_updates.py"
    violations = check_file(fp)
    # EA001 may fire if updates.tests is considered a different subpkg,
    # but EB001 must NOT fire.
    assert "EB001" not in _violation_codes(violations)


def test_rule_c_litellm_runtime_runtime_agent_wrap_import_flagged(make: _Maker) -> None:
    """Files under litellm_runtime/ must not import from agent_wrap at runtime."""
    make.write(
        "agent_wrap/domain/providers/litellm_runtime/callback.py",
        """\
        from agent_wrap.domain.pricing.models import Bucket
        """,
    )
    fp = make.root / "agent_wrap" / "domain" / "providers" / "litellm_runtime" / "callback.py"
    violations = check_file(fp)
    assert "EC001" in _violation_codes(violations)


def test_rule_c_litellm_runtime_runtime_import_agent_wrap_statement_flagged(
    make: _Maker,
) -> None:
    """Plain ``import agent_wrap.foo`` in litellm_runtime is also flagged."""
    make.write(
        "agent_wrap/domain/providers/litellm_runtime/callback.py",
        """\
        import agent_wrap.domain.foo
        """,
    )
    fp = make.root / "agent_wrap" / "domain" / "providers" / "litellm_runtime" / "callback.py"
    violations = check_file(fp)
    assert "EC001" in _violation_codes(violations)


def test_rule_c_litellm_runtime_type_checking_guard_allowed(make: _Maker) -> None:
    """TYPE_CHECKING-guarded agent_wrap imports are allowed in litellm_runtime."""
    make.write(
        "agent_wrap/domain/providers/litellm_runtime/callback.py",
        """\
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            from agent_wrap.domain.pricing.models import Bucket
        """,
    )
    fp = make.root / "agent_wrap" / "domain" / "providers" / "litellm_runtime" / "callback.py"
    violations = check_file(fp)
    assert "EC001" not in _violation_codes(violations)


def test_rule_c_litellm_runtime_non_agent_wrap_import_allowed(make: _Maker) -> None:
    """Non-agent_wrap imports in litellm_runtime are fine."""
    make.write(
        "agent_wrap/domain/providers/litellm_runtime/callback.py",
        """\
        import os
        from typing import Any
        """,
    )
    fp = make.root / "agent_wrap" / "domain" / "providers" / "litellm_runtime" / "callback.py"
    violations = check_file(fp)
    assert "EC001" not in _violation_codes(violations)


def test_edge_case_syntax_error_file_is_skipped(make: _Maker) -> None:
    make.write(
        "agent_wrap/domain/build/broken.py",
        "this is not valid python {{{",
    )
    fp = make.root / "agent_wrap" / "domain" / "build" / "broken.py"
    assert check_file(fp) == []


def test_edge_case_outside_domain_file_not_checked_for_rule_a(make: _Maker) -> None:
    """CLI files importing from domain are NOT subject to Rule A."""
    make.write(
        "agent_wrap/cli/stats/render.py",
        """\
        from agent_wrap.domain.pricing.models import Bucket
        """,
    )
    fp = make.root / "agent_wrap" / "cli" / "stats" / "render.py"
    violations = check_file(fp)
    assert "EA001" not in _violation_codes(violations)


def test_edge_case_standard_library_import_ignored(make: _Maker) -> None:
    make.write(
        "agent_wrap/domain/build/foo.py",
        """\
        from __future__ import annotations
        from typing import TYPE_CHECKING
        import os
        """,
    )
    fp = make.root / "agent_wrap" / "domain" / "build" / "foo.py"
    assert check_file(fp) == []


def test_edge_case_import_from_domain_package_not_subpackage(make: _Maker) -> None:
    """
    ``from agent_wrap.domain import something`` — not a cross-subpkg import
    since the target is the domain package itself, not a subpackage.
    """
    make.write(
        "agent_wrap/domain/stats/cost.py",
        """\
        from agent_wrap.domain import exceptions
        """,
    )
    fp = make.root / "agent_wrap" / "domain" / "stats" / "cost.py"
    violations = check_file(fp)
    assert "EA001" not in _violation_codes(violations)


def test_edge_case_import_agent_wrap_not_domain(make: _Maker) -> None:
    """Importing from agent_wrap.lib (not domain) is fine."""
    make.write(
        "agent_wrap/domain/stats/cost.py",
        """\
        from agent_wrap.lib.format import day_in_range
        """,
    )
    fp = make.root / "agent_wrap" / "domain" / "stats" / "cost.py"
    assert check_file(fp) == []


# --- Rule D: types belong in models.py -------------------------------------


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("dataclass", "@dataclass\nclass Thing:\n    a: int\n"),
        ("named_tuple", "class Thing(NamedTuple):\n    a: int\n"),
        ("typed_dict", "class Thing(TypedDict):\n    a: int\n"),
    ],
)
def test_rule_d_type_outside_models_flagged(make: _Maker, name: str, body: str) -> None:
    make.write(f"agent_wrap/domain/stats/{name}.py", body)
    fp = make.root / "agent_wrap" / "domain" / "stats" / f"{name}.py"
    assert "ED001" in _violation_codes(check_file(fp))


def test_rule_d_type_inside_models_allowed(make: _Maker) -> None:
    make.write("agent_wrap/domain/stats/models.py", "class Thing(NamedTuple):\n    a: int\n")
    fp = make.root / "agent_wrap" / "domain" / "stats" / "models.py"
    assert "ED001" not in _violation_codes(check_file(fp))


def test_rule_d_plain_class_is_behaviour_not_flagged(make: _Maker) -> None:
    """A plain class is a service or helper, not a data carrier."""
    make.write("agent_wrap/domain/stats/service.py", "class StatsService:\n    pass\n")
    fp = make.root / "agent_wrap" / "domain" / "stats" / "service.py"
    assert "ED001" not in _violation_codes(check_file(fp))


def test_rule_d_applies_to_cli_too(make: _Maker) -> None:
    make.write("agent_wrap/cli/stats/render.py", "class Rows(NamedTuple):\n    a: int\n")
    fp = make.root / "agent_wrap" / "cli" / "stats" / "render.py"
    assert "ED001" in _violation_codes(check_file(fp))


def test_rule_d_skips_lib(make: _Maker) -> None:
    """lib/ is standalone general-purpose code with no models.py convention."""
    make.write("agent_wrap/lib/flock.py", "class Row(NamedTuple):\n    a: int\n")
    fp = make.root / "agent_wrap" / "lib" / "flock.py"
    assert "ED001" not in _violation_codes(check_file(fp))


def test_rule_d_skips_test_files(make: _Maker) -> None:
    make.write("agent_wrap/domain/stats/tests/test_scan.py", "class Row(NamedTuple):\n    a: int\n")
    fp = make.root / "agent_wrap" / "domain" / "stats" / "tests" / "test_scan.py"
    assert "ED001" not in _violation_codes(check_file(fp))


# --- Rule F: enums belong in constants.py -----------------------------------


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("enum", "class Thing(Enum):\n    A = 1\n"),
        ("str_enum_mixin", "class Thing(str, Enum):\n    A = 'a'\n"),
        ("qualified_base", "class Thing(enum.IntEnum):\n    A = 1\n"),
    ],
)
def test_rule_f_enum_outside_constants_flagged(make: _Maker, name: str, body: str) -> None:
    make.write(f"agent_wrap/domain/stats/{name}.py", body)
    fp = make.root / "agent_wrap" / "domain" / "stats" / f"{name}.py"
    assert "EF001" in _violation_codes(check_file(fp))


def test_rule_f_enum_inside_constants_allowed(make: _Maker) -> None:
    make.write("agent_wrap/domain/stats/constants.py", "class Thing(Enum):\n    A = 1\n")
    fp = make.root / "agent_wrap" / "domain" / "stats" / "constants.py"
    assert "EF001" not in _violation_codes(check_file(fp))


def test_rule_f_enum_inside_models_flagged(make: _Maker) -> None:
    """models.py is not the enum's home — it must move to constants.py."""
    make.write("agent_wrap/domain/stats/models.py", "class Thing(Enum):\n    A = 1\n")
    fp = make.root / "agent_wrap" / "domain" / "stats" / "models.py"
    assert "EF001" in _violation_codes(check_file(fp))


def test_rule_f_applies_to_cli_too(make: _Maker) -> None:
    make.write("agent_wrap/cli/stats/render.py", "class Mode(Enum):\n    A = 1\n")
    fp = make.root / "agent_wrap" / "cli" / "stats" / "render.py"
    assert "EF001" in _violation_codes(check_file(fp))


def test_rule_f_skips_lib(make: _Maker) -> None:
    """lib/ is standalone general-purpose code with no constants.py convention."""
    make.write("agent_wrap/lib/flock.py", "class Priority(Enum):\n    HI = 1\n")
    fp = make.root / "agent_wrap" / "lib" / "flock.py"
    assert "EF001" not in _violation_codes(check_file(fp))


def test_rule_f_skips_test_files(make: _Maker) -> None:
    make.write("agent_wrap/domain/stats/tests/test_scan.py", "class Mode(Enum):\n    A = 1\n")
    fp = make.root / "agent_wrap" / "domain" / "stats" / "tests" / "test_scan.py"
    assert "EF001" not in _violation_codes(check_file(fp))


def test_rule_f_applies_to_package_root(make: _Maker) -> None:
    """The package root's models.py/constants.py pair follows the same split."""
    make.write("agent_wrap/models.py", "class Thing(Enum):\n    A = 1\n")
    fp = make.root / "agent_wrap" / "models.py"
    assert "EF001" in _violation_codes(check_file(fp))


def test_rule_f_package_root_constants_allowed(make: _Maker) -> None:
    make.write("agent_wrap/constants.py", "class Thing(Enum):\n    A = 1\n")
    fp = make.root / "agent_wrap" / "constants.py"
    assert "EF001" not in _violation_codes(check_file(fp))


# --- Rule E: constants belong in constants.py ------------------------------


def test_rule_e_constant_outside_constants_flagged(make: _Maker) -> None:
    make.write("agent_wrap/domain/stats/scan.py", "MAX_FILES = 64\n")
    fp = make.root / "agent_wrap" / "domain" / "stats" / "scan.py"
    assert "EE001" in _violation_codes(check_file(fp))


def test_rule_e_private_constant_also_flagged(make: _Maker) -> None:
    """Rule 10 covers _-prefixed constants explicitly."""
    make.write("agent_wrap/domain/stats/scan.py", "_MAX_FILES = 64\n")
    fp = make.root / "agent_wrap" / "domain" / "stats" / "scan.py"
    assert "EE001" in _violation_codes(check_file(fp))


def test_rule_e_annotated_constant_flagged(make: _Maker) -> None:
    make.write("agent_wrap/domain/stats/scan.py", "MAX_FILES: int = 64\n")
    fp = make.root / "agent_wrap" / "domain" / "stats" / "scan.py"
    assert "EE001" in _violation_codes(check_file(fp))


def test_rule_e_constant_inside_constants_allowed(make: _Maker) -> None:
    make.write("agent_wrap/domain/stats/constants.py", "MAX_FILES = 64\n")
    fp = make.root / "agent_wrap" / "domain" / "stats" / "constants.py"
    assert "EE001" not in _violation_codes(check_file(fp))


def test_rule_e_usage_and_summary_exempt(make: _Maker) -> None:
    """cli/commands.py reads these off each run module by name, so they cannot move."""
    make.write("agent_wrap/cli/stats/run.py", "USAGE = '[-v]'\nSUMMARY = 'Show stats'\n")
    fp = make.root / "agent_wrap" / "cli" / "stats" / "run.py"
    assert "EE001" not in _violation_codes(check_file(fp))


def test_rule_e_lower_case_binding_not_a_constant(make: _Maker) -> None:
    make.write("agent_wrap/domain/stats/scan.py", "logger = make_logger()\n")
    fp = make.root / "agent_wrap" / "domain" / "stats" / "scan.py"
    assert "EE001" not in _violation_codes(check_file(fp))


def test_rule_e_class_and_function_scope_ignored(make: _Maker) -> None:
    """Only module-level assignments are constants; ClassVars and locals are not."""
    make.write(
        "agent_wrap/domain/stats/service.py",
        """\
        class StatsService:
            MAX_FILES = 64

            def go(self) -> None:
                LOCAL_MAX = 5
                print(LOCAL_MAX)
        """,
    )
    fp = make.root / "agent_wrap" / "domain" / "stats" / "service.py"
    assert "EE001" not in _violation_codes(check_file(fp))


def test_rule_e_dunder_all_not_flagged(make: _Maker) -> None:
    make.write("agent_wrap/domain/stats/scan.py", "__all__ = ['scan']\n")
    fp = make.root / "agent_wrap" / "domain" / "stats" / "scan.py"
    assert "EE001" not in _violation_codes(check_file(fp))


def test_rule_e_skips_lib(make: _Maker) -> None:
    make.write("agent_wrap/lib/flock.py", "LOCK_POLL_INTERVAL = 0.1\n")
    fp = make.root / "agent_wrap" / "lib" / "flock.py"
    assert "EE001" not in _violation_codes(check_file(fp))


def test_rule_e_models_may_hold_no_constants(make: _Maker) -> None:
    """models.py is exempt from rule D but still subject to rule E."""
    make.write("agent_wrap/domain/stats/models.py", "MAX_FILES = 64\n")
    fp = make.root / "agent_wrap" / "domain" / "stats" / "models.py"
    assert "EE001" in _violation_codes(check_file(fp))


def test_rule_d_constants_may_hold_no_types(make: _Maker) -> None:
    """constants.py is exempt from rule E but still subject to rule D."""
    make.write("agent_wrap/domain/stats/constants.py", "class Thing(NamedTuple):\n    a: int\n")
    fp = make.root / "agent_wrap" / "domain" / "stats" / "constants.py"
    assert "ED001" in _violation_codes(check_file(fp))

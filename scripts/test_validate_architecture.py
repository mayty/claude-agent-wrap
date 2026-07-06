# This file has been created with the assistance of an AI tool.
"""Tests for scripts/validate-architecture.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from textwrap import dedent

import pytest

# ---------------------------------------------------------------------------
# Load the script under test as a module
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# subpackage key resolution — unit tests
# ---------------------------------------------------------------------------


class TestSourceSubpackageKey:
    """source_subpackage_key: file path to subpackage name."""

    def test_top_level(self) -> None:
        p = DOMAIN_DIR / "build" / "project_utils.py"
        assert source_subpackage_key(p) == "build"

    def test_nested_provider(self) -> None:
        p = DOMAIN_DIR / "providers" / "litellm_bedrock" / "provider.py"
        assert source_subpackage_key(p) == "providers.litellm_bedrock"

    def test_top_level_providers(self) -> None:
        p = DOMAIN_DIR / "providers" / "discovery.py"
        assert source_subpackage_key(p) == "providers"

    def test_test_file_inherits_parent(self) -> None:
        p = DOMAIN_DIR / "stats" / "tests" / "test_scan.py"
        assert source_subpackage_key(p) == "stats"

    def test_nested_test_file_inherits_nested(self) -> None:
        p = DOMAIN_DIR / "providers" / "litellm_common" / "tests" / "test_provider.py"
        assert source_subpackage_key(p) == "providers.litellm_common"

    def test_outside_domain_returns_none(self) -> None:
        p = ROOT / "agent_wrap" / "cli" / "stats" / "render.py"
        assert source_subpackage_key(p) is None

    def test_unknown_provider_nested_dir_not_flagged(self) -> None:
        """Only known NESTED_PROVIDERS get special treatment."""
        p = DOMAIN_DIR / "providers" / "something_else" / "foo.py"
        assert source_subpackage_key(p) == "providers"


class TestTargetSubpackageKey:
    """target_subpackage_key: import module path to subpackage name."""

    def test_top_level(self) -> None:
        assert target_subpackage_key("build.project_utils") == "build"

    def test_nested_provider(self) -> None:
        assert (
            target_subpackage_key("providers.litellm_bedrock.provider")
            == "providers.litellm_bedrock"
        )

    def test_parent_providers(self) -> None:
        assert target_subpackage_key("providers.discovery") == "providers"

    def test_exact_match(self) -> None:
        assert target_subpackage_key("build") == "build"

    def test_non_domain_module_returns_none(self) -> None:
        assert target_subpackage_key("lib.format") is None


# ---------------------------------------------------------------------------
# End-to-end tests via check_file against temporary directories
# ---------------------------------------------------------------------------


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


# --- Rule A: cross-domain runtime imports --------------------------------


class TestRuleASameSubpackageAllowed:
    def test_direct_import(self, make: _Maker) -> None:
        make.write(
            "agent_wrap/domain/build/foo.py",
            """\
            from agent_wrap.domain.build.bar import helper
            """,
        )
        fp = make.root / "agent_wrap" / "domain" / "build" / "foo.py"
        assert _violation_codes(check_file(fp)) == set()


class TestRuleACrossSubpackageViolation:
    def test_absolute_import(self, make: _Maker) -> None:
        make.write(
            "agent_wrap/domain/stats/cost.py",
            """\
            from agent_wrap.domain.pricing.models import Bucket
            """,
        )
        fp = make.root / "agent_wrap" / "domain" / "stats" / "cost.py"
        violations = check_file(fp)
        assert "EA001" in _violation_codes(violations)

    def test_relative_import_cross_subpackage(self, make: _Maker) -> None:
        make.write(
            "agent_wrap/domain/stats/cost.py",
            """\
            from ..pricing.models import Bucket
            """,
        )
        fp = make.root / "agent_wrap" / "domain" / "stats" / "cost.py"
        violations = check_file(fp)
        assert "EA001" in _violation_codes(violations)

    def test_import_statement(self, make: _Maker) -> None:
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


class TestRuleATypeCheckingGuardAllowed:
    def test_simple_guard(self, make: _Maker) -> None:
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

    def test_compound_guard(self, make: _Maker) -> None:
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

    def test_runtime_import_same_subpackage_still_allowed(self, make: _Maker) -> None:
        """Within-subpackage runtime import is fine regardless."""
        make.write(
            "agent_wrap/domain/stats/cost.py",
            """\
            from agent_wrap.domain.stats.scan import scan_dirs
            """,
        )
        fp = make.root / "agent_wrap" / "domain" / "stats" / "cost.py"
        assert _violation_codes(check_file(fp)) == set()


class TestRuleAProviderNesting:
    def test_provider_import_from_common_is_cross(self, make: _Maker) -> None:
        make.write(
            "agent_wrap/domain/providers/litellm_bedrock/provider.py",
            """\
            from agent_wrap.domain.providers.litellm_common.provider import LiteLLMProvider
            """,
        )
        fp = make.root / "agent_wrap" / "domain" / "providers" / "litellm_bedrock" / "provider.py"
        violations = check_file(fp)
        assert "EA001" in _violation_codes(violations)

    def test_provider_import_from_parent_providers_is_cross(self, make: _Maker) -> None:
        make.write(
            "agent_wrap/domain/providers/litellm_common/provider.py",
            """\
            from agent_wrap.domain.providers.base import BaseProvider
            """,
        )
        fp = make.root / "agent_wrap" / "domain" / "providers" / "litellm_common" / "provider.py"
        violations = check_file(fp)
        assert "EA001" in _violation_codes(violations)


# --- Rule B: private-name imports ----------------------------------------


class TestRuleBPrivateNameImportViolation:
    def test_from_import_private(self, make: _Maker) -> None:
        make.write(
            "agent_wrap/domain/logs/io.py",
            """\
            from agent_wrap.domain.logs.models import _SessionMeta
            """,
        )
        fp = make.root / "agent_wrap" / "domain" / "logs" / "io.py"
        violations = check_file(fp)
        assert "EB001" in _violation_codes(violations)

    def test_import_private_module(self, make: _Maker) -> None:
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


class TestRuleBTestFilesAllowed:
    def test_private_import_in_test_dir(self, make: _Maker) -> None:
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


# --- Rule C: litellm_runtime agent_wrap imports ---------------------------


class TestRuleCLitellmRuntime:
    def test_runtime_agent_wrap_import_flagged(self, make: _Maker) -> None:
        """Files under litellm_runtime/ must not import from agent_wrap at runtime."""
        make.write(
            "agent_wrap/domain/providers/litellm_common/litellm_runtime/callback.py",
            """\
            from agent_wrap.domain.pricing.models import Bucket
            """,
        )
        fp = (
            make.root
            / "agent_wrap"
            / "domain"
            / "providers"
            / "litellm_common"
            / "litellm_runtime"
            / "callback.py"
        )
        violations = check_file(fp)
        assert "EC001" in _violation_codes(violations)

    def test_runtime_import_agent_wrap_statement_flagged(self, make: _Maker) -> None:
        """Plain ``import agent_wrap.foo`` in litellm_runtime is also flagged."""
        make.write(
            "agent_wrap/domain/providers/litellm_common/litellm_runtime/callback.py",
            """\
            import agent_wrap.domain.foo
            """,
        )
        fp = (
            make.root
            / "agent_wrap"
            / "domain"
            / "providers"
            / "litellm_common"
            / "litellm_runtime"
            / "callback.py"
        )
        violations = check_file(fp)
        assert "EC001" in _violation_codes(violations)

    def test_type_checking_guard_allowed(self, make: _Maker) -> None:
        """TYPE_CHECKING-guarded agent_wrap imports are allowed in litellm_runtime."""
        make.write(
            "agent_wrap/domain/providers/litellm_common/litellm_runtime/callback.py",
            """\
            from typing import TYPE_CHECKING
            if TYPE_CHECKING:
                from agent_wrap.domain.pricing.models import Bucket
            """,
        )
        fp = (
            make.root
            / "agent_wrap"
            / "domain"
            / "providers"
            / "litellm_common"
            / "litellm_runtime"
            / "callback.py"
        )
        violations = check_file(fp)
        assert "EC001" not in _violation_codes(violations)

    def test_non_agent_wrap_import_allowed(self, make: _Maker) -> None:
        """Non-agent_wrap imports in litellm_runtime are fine."""
        make.write(
            "agent_wrap/domain/providers/litellm_common/litellm_runtime/callback.py",
            """\
            import os
            from typing import Any
            """,
        )
        fp = (
            make.root
            / "agent_wrap"
            / "domain"
            / "providers"
            / "litellm_common"
            / "litellm_runtime"
            / "callback.py"
        )
        violations = check_file(fp)
        assert "EC001" not in _violation_codes(violations)


# --- Edge cases -----------------------------------------------------------


class TestEdgeCases:
    def test_syntax_error_file_is_skipped(self, make: _Maker) -> None:
        make.write(
            "agent_wrap/domain/build/broken.py",
            "this is not valid python {{{",
        )
        fp = make.root / "agent_wrap" / "domain" / "build" / "broken.py"
        assert check_file(fp) == []

    def test_outside_domain_file_not_checked_for_rule_a(self, make: _Maker) -> None:
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

    def test_standard_library_import_ignored(self, make: _Maker) -> None:
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

    def test_import_from_domain_package_not_subpackage(self, make: _Maker) -> None:
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

    def test_import_agent_wrap_not_domain(self, make: _Maker) -> None:
        """Importing from agent_wrap.lib (not domain) is fine."""
        make.write(
            "agent_wrap/domain/stats/cost.py",
            """\
            from agent_wrap.lib.format import day_in_range
            """,
        )
        fp = make.root / "agent_wrap" / "domain" / "stats" / "cost.py"
        assert check_file(fp) == []

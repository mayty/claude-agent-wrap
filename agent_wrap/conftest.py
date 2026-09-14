# This file has been edited with the assistance of an AI tool.
"""
Shared fixtures for all agent_wrap tests.

Placed at the package root so pytest discovers it for every test file
under ``agent_wrap/**/tests/``.
"""

from functools import cached_property
from typing import TYPE_CHECKING, Any, ClassVar, override
from unittest.mock import Mock

import pytest

from agent_wrap.constants import (
    AGENT_ASSETS_DIR,
    AGENT_DOCKERFILE_NAME,
    LEGACY_AGENT_DOCKERFILE_NAME,
)
from agent_wrap.containers import Core, core, repositories
from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.providers.base import Provider
from agent_wrap.domain.sidecars.service import SidecarService
from agent_wrap.domain.stats.service import StatsService
from agent_wrap.infrastructure.constants import BACKUPS_DIRNAME, DB_DIRNAME
from agent_wrap.infrastructure.logs.repositories.ingest import LogIngestRepository
from agent_wrap.infrastructure.logs.repositories.requests import RequestRepository
from agent_wrap.infrastructure.logs.repositories.sessions import SessionRepository
from agent_wrap.infrastructure.logs.repositories.usage import UsageRepository
from agent_wrap.infrastructure.projects.repositories.projects import ProjectsRepository

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from pytest_mock import MockerFixture

    from agent_wrap.domain.pricing.service import PricingService
    from agent_wrap.domain.providers.models import Tier


@pytest.fixture(autouse=True)
def _patch_path_constants(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect path constants to tmp_path for test isolation."""
    for mod in (
        "agent_wrap.constants",
        "agent_wrap.lib.utils",
        "agent_wrap.domain.stats.service",
        "agent_wrap.domain.secrets.service",
        "agent_wrap.domain.config.service",
        "agent_wrap.domain.updates.service",
        "agent_wrap.domain.pricing.service",
        "agent_wrap.domain.logs.io",
        "agent_wrap.domain.logs.cache",
        "agent_wrap.domain.logs.daemon",
        "agent_wrap.domain.logs.service",
        "agent_wrap.domain.logs.server",
        "agent_wrap.domain.logs.normalize",
        "agent_wrap.domain.logs.usage_tracker",
        "agent_wrap.domain.stats.fold",
        "agent_wrap.domain.status.service",
        "agent_wrap.domain.launch.service",
        "agent_wrap.domain.build.service",
        "agent_wrap.cli.logs.run",
        "agent_wrap.cli.update.run",
        "agent_wrap.cli.rebuild.run",
        "agent_wrap.cli.stats.run",
        "agent_wrap.cli.inspect.run",
        "agent_wrap.cli.run.run",
        "agent_wrap.domain.providers.key_approval",
        "agent_wrap.domain.providers.base",
    ):
        monkeypatch.setattr(f"{mod}.TOOL_DIR", tmp_path, raising=False)
        monkeypatch.setattr(f"{mod}.GLOBAL_CONFIG_DIR", tmp_path, raising=False)
        monkeypatch.setattr(f"{mod}.OPS_DIR", tmp_path / "ops", raising=False)
        monkeypatch.setattr(
            f"{mod}.AGENT_LAUNCHES_DIR", tmp_path / ".agent-launches", raising=False
        )


@pytest.fixture
def db_dir(tmp_path: Path) -> Path:
    """Return the directory every database and backup in a test is written under."""
    return tmp_path / DB_DIRNAME


@pytest.fixture
def db_core(db_dir: Path) -> Core:
    """Return a ``Core`` whose databases and backups live under ``tmp_path``."""
    return Core(db_dir=db_dir, backups_dir=db_dir / BACKUPS_DIRNAME)


@pytest.fixture(autouse=True)
def _isolate_databases(db_core: Core, db_dir: Path, mocker: MockerFixture) -> None:
    """
    Point the module-level containers at ``tmp_path`` before any test body runs.

    ``Core`` composes its paths once, at import, from the real ``AGENT_LAUNCHES_DIR``.
    Any test that reaches ``containers.services`` would otherwise migrate and write the
    developer's own database — and that is not hypothetical: ``patch.object`` saves the
    original by ``getattr``, so mocking a service *evaluates* the whole
    ``cached_property`` chain and opens the database as a side effect.

    Which makes the ordering here load-bearing. ``agent_wrap/cli/conftest.py``'s autouse
    ``_mock_all_services`` does exactly that ``getattr``; autouse fixtures from a parent
    conftest run before a child conftest's, so by the time it does, the singletons are
    already redirected. Do not move this fixture down the hierarchy.

    Only the cached properties are evicted, by name. ``__dict__.clear()`` would also
    take the attributes ``__init__`` set -- ``Core``'s directories, ``Repositories``'
    reference to the core -- and leave the container unusable.

    Eviction happens on setup only, deliberately. A teardown eviction would delete a
    ``cached_property`` entry that a test's own ``mocker.patch.object`` still intends to
    restore, and ``mock``'s ``delattr`` would then fail on the way out. Setup is where it
    matters anyway: every test is preceded by one.
    """
    for container in (core, repositories):
        cached = {
            name
            for name, attr in type(container).__dict__.items()
            if isinstance(attr, cached_property)
        }
        for name in cached & set(container.__dict__):
            del container.__dict__[name]

    mocker.patch.object(core, "_db_dir", db_dir)
    mocker.patch.object(core, "_backups_dir", db_dir / BACKUPS_DIRNAME)
    mocker.patch.object(repositories, "_core", db_core)


def _display_over(db_dir: Path, *, tty: bool) -> DisplayService:
    """
    Build a real ``DisplayService`` whose consoles state outright whether they are a terminal.

    A console resolves its colour system in its constructor and keeps that verdict, so
    ``force_terminal`` is the seam a test has: patching ``sys.stderr.isatty`` inside the
    test body would come too late for a console the fixture already built.

    Built through a throwaway ``Core`` so the rich flags are stated in exactly one place,
    the same override seam the repository fixtures use. No database is opened: the
    connection factories are never touched.
    """
    core = Core(db_dir=db_dir, backups_dir=db_dir / BACKUPS_DIRNAME, force_terminal=tty)
    return DisplayService(
        console_out=core.console_out,
        console_err=core.console_err,
        console_render=core.console_render,
    )


@pytest.fixture
def non_tty_display(db_dir: Path) -> DisplayService:
    """Return a real DisplayService writing to a pipe: no colour, and no width to respect."""
    return _display_over(db_dir, tty=False)


@pytest.fixture
def tty_display(db_dir: Path) -> DisplayService:
    """Return a real DisplayService writing to a terminal, for asserting on colour."""
    return _display_over(db_dir, tty=True)


@pytest.fixture
def read_only_core(db_dir: Path) -> Core:
    """
    Return a second ``Core`` over the same database whose factory can never write.

    Models a read-only consumer -- the logs daemon above all -- faithfully: a distinct
    ``ConnectionFactory`` that has never been granted writes, and so cannot be perturbed
    by whatever ``projects_repository`` did to the other one.
    """
    return Core(db_dir=db_dir, backups_dir=db_dir / BACKUPS_DIRNAME)


@pytest.fixture
def projects_repository(db_core: Core) -> Iterator[ProjectsRepository]:
    """
    Yield a ``ProjectsRepository`` over a migrated, empty database in ``tmp_path``.

    Holds a write grant for the test's duration: nearly everything that touches the
    registry writes to it, and a CLI command would hold one too. A test that needs a
    write *refused* builds over ``read_only_core`` instead.
    """
    factory = db_core.projects_db
    with factory.enable_writes():
        yield ProjectsRepository(connection_factory=factory)


@pytest.fixture
def log_ingest_repository(db_core: Core) -> Iterator[LogIngestRepository]:
    """
    Yield a ``LogIngestRepository`` over a migrated, empty logs database in ``tmp_path``.

    Holds a write grant for the test's duration, as the daemon and ``agent reindex``
    both do. A test that needs a write *refused* builds over ``read_only_core``.
    """
    factory = db_core.logs_db
    with factory.enable_writes():
        yield LogIngestRepository(connection_factory=factory)


@pytest.fixture
def usage_repository(db_core: Core) -> UsageRepository:
    """
    Return a ``UsageRepository`` over the migrated, empty logs database in ``tmp_path``.

    No write grant: this repository only reads, and ``agent stats`` is the one verb
    deliberately given no grant on either database. A test that seeds rows takes the
    ``log_ingest_repository`` fixture alongside this one, and the two share a factory.
    """
    return UsageRepository(connection_factory=db_core.logs_db)


@pytest.fixture
def log_session_repository(db_core: Core) -> SessionRepository:
    """
    Return a ``SessionRepository`` over the migrated, empty logs database in ``tmp_path``.

    No write grant, for the same reason ``usage_repository`` has none: this is the
    viewer's read side. A test that needs rows in it takes ``log_ingest_repository``
    alongside, which shares the factory.
    """
    return SessionRepository(connection_factory=db_core.logs_db)


@pytest.fixture
def log_request_repository(db_core: Core) -> RequestRepository:
    """
    Return a ``RequestRepository`` over the migrated, empty logs database in ``tmp_path``.

    The viewer's content read side, and read-only for the same reason the other two are.
    A test that needs requests in it takes ``log_ingest_repository`` alongside.
    """
    return RequestRepository(connection_factory=db_core.logs_db)


@pytest.fixture
def make_stats_service(
    usage_repository: UsageRepository, log_ingest_repository: LogIngestRepository
) -> Callable[[PricingService], StatsService]:
    """
    Return a factory building a ``StatsService`` over the test's own logs database.

    A factory rather than a fixture because the pricing table is what varies: some
    tests want exact costs from a fixed rate table, some want a bare mock, and one
    wants a service with no pricing data at all. Here at the root because both the
    stats tests and the logs tests need it -- the viewer's usage tracker asks a real
    ``StatsService`` for the day's total.
    """

    def _make(pricing: PricingService) -> StatsService:
        return StatsService(
            pricing_service=pricing,
            config_service=Mock(spec=ConfigService),
            usage_repository=usage_repository,
            log_ingest_repository=log_ingest_repository,
        )

    return _make


@pytest.fixture
def register_projects(projects_repository: ProjectsRepository) -> Callable[..., None]:
    """Return a factory registering project paths, the way ``agent run`` would."""

    def _register(*paths: Path | str) -> None:
        for path in paths:
            projects_repository.record(str(path))

    return _register


@pytest.fixture
def tool_dir(tmp_path: Path) -> Path:
    """Create a temporary tool directory with minimal structure."""
    d = tmp_path / "tool"
    d.mkdir(exist_ok=True)
    (d / ".claude_config").mkdir(exist_ok=True)
    (d / ".agent-launches").mkdir(exist_ok=True)
    return d


@pytest.fixture
def display_mock(mocker: MockerFixture) -> Mock:
    """Return a spec-mocked DisplayService for use in any test."""
    return mocker.Mock(spec=DisplayService)


class FakeProvider(Provider):
    """
    A concrete ``Provider`` for tests that only care about pricing.

    Built via the ``make_fake_provider`` factory fixture. The sidecar hooks are
    stubs — nothing that uses this drives a real sidecar.
    """

    name = "fake-provider"
    secret_description: ClassVar[str] = "Fake API Key"  # noqa: S105

    def __init__(
        self,
        display_service: DisplayService | Mock | None = None,
        flat: dict[str, dict[str, float]] | None = None,
        tiered: dict[str, list[Tier]] | None = None,
    ) -> None:
        super().__init__(
            sidecar_service=Mock(spec=SidecarService),
            display_service=display_service or Mock(spec=DisplayService),
        )
        self._flat = flat or {}
        self._tiered = tiered

    @override
    def get_sidecar_env(self, secrets: dict[str, Any]) -> dict[str, str]:
        return {"UPSTREAM_KEY": secrets.get("api_key", "")}

    @override
    def get_agent_env(self, master_key: str, base_url: str) -> dict[str, str]:
        return {"API_KEY": master_key, "BASE_URL": base_url}

    @override
    def _get_pricing(self, *, refresh_pricing_data: bool = False) -> dict[str, dict[str, float]]:
        return self._flat

    @override
    def _get_tiered_pricing(self, *, refresh_pricing_data: bool = False) -> dict[str, list[Tier]]:
        if self._tiered is None:
            raise NotImplementedError
        return self._tiered


@pytest.fixture
def make_fake_provider() -> Callable[..., FakeProvider]:
    """Return a factory building a ``FakeProvider`` with a flat or tiered price table."""

    def _make(
        display_service: DisplayService | Mock | None = None,
        flat: dict[str, dict[str, float]] | None = None,
        tiered: dict[str, list[Tier]] | None = None,
    ) -> FakeProvider:
        return FakeProvider(display_service=display_service, flat=flat, tiered=tiered)

    return _make


@pytest.fixture
def write_dockerfile(tmp_path: Path) -> Callable[[str], Path]:
    """Write content to a temporary project Dockerfile and return its path."""

    def _write(content: str) -> Path:
        p = tmp_path / AGENT_ASSETS_DIR / AGENT_DOCKERFILE_NAME
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    return _write


@pytest.fixture
def write_legacy_dockerfile(tmp_path: Path) -> Callable[[str], Path]:
    """Write content to a temporary deprecated ``Dockerfile.agent`` and return its path."""

    def _write(content: str) -> Path:
        p = tmp_path / LEGACY_AGENT_DOCKERFILE_NAME
        p.write_text(content)
        return p

    return _write

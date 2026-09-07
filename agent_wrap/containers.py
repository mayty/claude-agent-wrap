# This file has been edited with the assistance of an AI tool.
"""
Singleton containers with lazy-initialized, dependency-injected members.

Three tiers, each built on the one below it:

``Core``
    Connection factories -- one per database. Constructing a factory is what migrates
    its database, so the laziness is load-bearing: ``agent --help`` opens nothing.
``Repositories``
    Repository instances, each over a factory from ``Core``.
``Services``
    Domain services, wired to each other and to the repositories they need.

This is the composition root, and the only place permitted to import from
``agent_wrap.infrastructure`` at runtime -- everything above reaches a repository
through constructor injection.
"""

from functools import cached_property
from typing import TYPE_CHECKING

from agent_wrap.constants import AGENT_LAUNCHES_DIR
from agent_wrap.infrastructure.constants import (
    BACKUPS_DIRNAME,
    DB_DIRNAME,
    DB_FILE_SUFFIX,
    INFRASTRUCTURE_DIR,
    MIGRATIONS_DIRNAME,
    Databases,
)

if TYPE_CHECKING:
    from pathlib import Path

    from agent_wrap.domain.build.service import BuildService
    from agent_wrap.domain.config.service import ConfigService
    from agent_wrap.domain.create.service import CreateService
    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.launch.service import LaunchService
    from agent_wrap.domain.logs.service import LogsService
    from agent_wrap.domain.pricing.service import PricingService
    from agent_wrap.domain.providers.service import ProviderService
    from agent_wrap.domain.secrets.service import SecretsService
    from agent_wrap.domain.sidecars.service import SidecarService
    from agent_wrap.domain.startup.service import StartupService
    from agent_wrap.domain.stats.service import StatsService
    from agent_wrap.domain.status.service import InspectService
    from agent_wrap.domain.updates.service import UpdateService
    from agent_wrap.infrastructure.connection import ConnectionFactory
    from agent_wrap.infrastructure.projects.repositories.projects import ProjectsRepository


class Core:
    """
    Lazy-initialized container for the storage layer's connection factories.

    Takes its directories as constructor arguments rather than reading them from a
    module-level constant: that is the seam a test overrides, by building its own
    ``Core`` against ``tmp_path`` instead of monkeypatching a path into place.

    Each factory runs its database's migrations when it is first constructed, so a
    command that never touches a database never migrates one.
    """

    def __init__(self, db_dir: Path, backups_dir: Path) -> None:
        self._db_dir = db_dir
        self._backups_dir = backups_dir

    @cached_property
    def projects_db(self) -> ConnectionFactory:
        from agent_wrap.infrastructure.connection import ConnectionFactory

        return ConnectionFactory(
            name=Databases.PROJECTS,
            db_path=self._db_dir / f"{Databases.PROJECTS}{DB_FILE_SUFFIX}",
            migrations_dir=INFRASTRUCTURE_DIR / Databases.PROJECTS / MIGRATIONS_DIRNAME,
            backups_dir=self._backups_dir,
        )


class Repositories:
    """
    Lazy-initialized container for repositories, each over a database from ``Core``.

    A repository is the only thing above the storage layer that knows a database exists.
    Domain services receive one by constructor injection and see app objects, never rows.
    """

    def __init__(self, core: Core) -> None:
        self._core = core

    @cached_property
    def projects_repository(self) -> ProjectsRepository:
        from agent_wrap.infrastructure.projects.repositories.projects import ProjectsRepository

        return ProjectsRepository(connection_factory=self._core.projects_db)


class Services:
    """
    Lazy-initialized singleton container for all domain services.

    Each service is a ``@cached_property`` that creates its dependencies via
    constructor injection. Services that are never accessed are never created.
    """

    def __init__(self, repositories: Repositories) -> None:
        self._repositories = repositories

    @cached_property
    def display_service(self) -> DisplayService:
        from agent_wrap.domain.display.service import DisplayService

        return DisplayService()

    @cached_property
    def provider_service(self) -> ProviderService:
        from agent_wrap.domain.providers.service import ProviderService

        return ProviderService(
            sidecar_service=self.sidecar_service,
            display_service=self.display_service,
        )

    @cached_property
    def sidecar_service(self) -> SidecarService:
        from agent_wrap.domain.sidecars.service import SidecarService

        return SidecarService(display_service=self.display_service)

    @cached_property
    def config_service(self) -> ConfigService:
        from agent_wrap.domain.config.service import ConfigService

        return ConfigService(
            display_service=self.display_service,
            projects_repository=self._repositories.projects_repository,
        )

    @cached_property
    def secrets_service(self) -> SecretsService:
        from agent_wrap.domain.secrets.service import SecretsService

        return SecretsService(
            provider_service=self.provider_service,
            sidecar_service=self.sidecar_service,
            display_service=self.display_service,
        )

    @cached_property
    def pricing_service(self) -> PricingService:
        from agent_wrap.domain.pricing.service import PricingService

        return PricingService(
            provider_service=self.provider_service,
            display_service=self.display_service,
        )

    @cached_property
    def update_service(self) -> UpdateService:
        from agent_wrap.domain.updates.service import UpdateService

        return UpdateService(
            display_service=self.display_service,
            logs_service=self.logs_service,
            sidecar_service=self.sidecar_service,
        )

    @cached_property
    def launch_service(self) -> LaunchService:
        from agent_wrap.domain.launch.service import LaunchService

        return LaunchService(
            config_service=self.config_service,
            secrets_service=self.secrets_service,
            update_service=self.update_service,
            provider_service=self.provider_service,
            sidecar_service=self.sidecar_service,
            build_service=self.build_service,
            startup_service=self.startup_service,
            display_service=self.display_service,
            logs_service=self.logs_service,
        )

    @cached_property
    def startup_service(self) -> StartupService:
        from agent_wrap.domain.startup.service import StartupService

        return StartupService(display_service=self.display_service)

    @cached_property
    def build_service(self) -> BuildService:
        from agent_wrap.domain.build.service import BuildService

        return BuildService(
            update_service=self.update_service,
            display_service=self.display_service,
        )

    @cached_property
    def create_service(self) -> CreateService:
        from agent_wrap.domain.create.service import CreateService

        return CreateService(display_service=self.display_service)

    @cached_property
    def logs_service(self) -> LogsService:
        from agent_wrap.domain.logs.service import LogsService

        return LogsService(
            pricing_service=self.pricing_service,
            stats_service=self.stats_service,
            config_service=self.config_service,
            display_service=self.display_service,
        )

    @cached_property
    def inspect_service(self) -> InspectService:
        from agent_wrap.domain.status.service import InspectService

        return InspectService(
            sidecar_service=self.sidecar_service,
            provider_service=self.provider_service,
            secrets_service=self.secrets_service,
            logs_service=self.logs_service,
            updates_service=self.update_service,
            config_service=self.config_service,
            build_service=self.build_service,
        )

    @cached_property
    def stats_service(self) -> StatsService:
        from agent_wrap.domain.stats.service import StatsService

        return StatsService(
            pricing_service=self.pricing_service,
            config_service=self.config_service,
        )


core = Core(
    db_dir=AGENT_LAUNCHES_DIR / DB_DIRNAME,
    backups_dir=AGENT_LAUNCHES_DIR / DB_DIRNAME / BACKUPS_DIRNAME,
)
repositories = Repositories(core=core)
services = Services(repositories=repositories)

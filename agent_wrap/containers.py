# This file has been created with the assistance of an AI tool.
"""Singleton service container with lazy-initialized, dependency-injected services."""

from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
    from agent_wrap.domain.stats.service import StatsService
    from agent_wrap.domain.updates.service import UpdateService


class Services:
    """
    Lazy-initialized singleton container for all domain services.

    Each service is a ``@cached_property`` that creates its dependencies via
    constructor injection. Services that are never accessed are never created.
    """

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

        return ConfigService(display_service=self.display_service)

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

        return UpdateService(display_service=self.display_service)

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
            display_service=self.display_service,
        )

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
    def stats_service(self) -> StatsService:
        from agent_wrap.domain.stats.service import StatsService

        return StatsService(
            pricing_service=self.pricing_service,
            config_service=self.config_service,
        )


services = Services()

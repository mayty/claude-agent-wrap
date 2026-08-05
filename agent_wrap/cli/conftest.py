# This file has been created with the assistance of an AI tool.
"""
Shared fixtures for all CLI tests.

The autouse ``_mock_all_services`` fixture replaces every
``services.*_service`` with a spec-mocked instance so no CLI test
can accidentally call real domain code. Individual tests configure
specific return values or side effects on the already-mocked services.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import pytest_mock

from agent_wrap.containers import services

# NOTE: The domain-class imports below are an intentional exception to the
# "never import from agent_wrap.domain.xxx directly" rule.  The classes are
# used ONLY as ``spec=`` arguments to ``mocker.Mock(spec=SomeService)`` —
# type metadata for the test harness, not domain logic.  No method is ever
# called on the imported classes.
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
from agent_wrap.domain.status.service import InspectService
from agent_wrap.domain.updates.service import UpdateService


@pytest.fixture(autouse=True)
def _mock_all_services(mocker: pytest_mock.MockFixture) -> None:
    """Replace every services.*_service with a spec-mocked instance."""
    mocker.patch.object(services, "build_service", mocker.Mock(spec=BuildService))
    mocker.patch.object(services, "config_service", mocker.Mock(spec=ConfigService))
    mocker.patch.object(services, "create_service", mocker.Mock(spec=CreateService))
    mocker.patch.object(services, "display_service", mocker.Mock(spec=DisplayService))
    mocker.patch.object(services, "inspect_service", mocker.Mock(spec=InspectService))
    mocker.patch.object(services, "launch_service", mocker.Mock(spec=LaunchService))
    mocker.patch.object(services, "logs_service", mocker.Mock(spec=LogsService))
    mocker.patch.object(services, "pricing_service", mocker.Mock(spec=PricingService))
    mocker.patch.object(services, "secrets_service", mocker.Mock(spec=SecretsService))
    mocker.patch.object(services, "sidecar_service", mocker.Mock(spec=SidecarService))
    mocker.patch.object(services, "stats_service", mocker.Mock(spec=StatsService))
    mocker.patch.object(services, "update_service", mocker.Mock(spec=UpdateService))
    mocker.patch.object(services, "provider_service", mocker.Mock(spec=ProviderService))

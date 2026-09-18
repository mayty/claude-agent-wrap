# This file has been created with the assistance of an AI tool.
"""
Shared fixtures for the stats domain's tests.

Every aggregation test now goes through the request index rather than through a file
scan, so the shape of a test is: write a session's records into the central log tree,
run ingest over it, then ask ``StatsService``. The writing and the ingesting are the
same two steps in every one of them, which is what these fixtures are.

Seeding through the real parser and the real repository rather than by inserting rows is
deliberate. The thing being tested is the agreement between what the sidecar writes and
what the aggregate reports, and a fixture that hand-wrote ``requests`` rows would test
the aggregate against my idea of the schema instead.
"""

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from agent_wrap.constants import LITELLM_LOGS_DIRNAME
from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.logs.constants import MESSAGES_FILENAME
from agent_wrap.domain.logs.service import LogsService
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.stats.service import StatsService
from agent_wrap.lib.path_hash import project_path_hash

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agent_wrap.infrastructure.logs.repositories.ingest import LogIngestRepository
    from agent_wrap.infrastructure.logs.repositories.requests import RequestRepository
    from agent_wrap.infrastructure.logs.repositories.sessions import SessionRepository

# Every fixture below writes under this provider unless a test names another. It is the
# sidecar's directory name, which is what a model renders under -- not a vendor prefix.
DEFAULT_PROVIDER = "litellm-bedrock"


@pytest.fixture
def logs_root(tmp_path: Path) -> Path:
    """
    Return the central log tree, which ``TOOL_DIR`` is redirected to under ``tmp_path``.

    The same path ingest composes for itself, so a session written here is a session the
    index will find.
    """
    root = tmp_path / LITELLM_LOGS_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def write_session(logs_root: Path) -> Callable[..., Path]:
    """
    Return a factory appending records to one session of a project's log subtree.

    The project directory must already exist: ``project_path_hash`` resolves before
    hashing, so a directory created afterwards hashes to a different value and its logs
    would look orphaned.
    """

    def _write(
        project: Path,
        session: str,
        records: list[dict[str, Any]],
        provider: str = DEFAULT_PROVIDER,
    ) -> Path:
        session_dir = logs_root / project_path_hash(project) / provider / session
        session_dir.mkdir(parents=True, exist_ok=True)
        with (session_dir / MESSAGES_FILENAME).open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        return session_dir

    return _write


@pytest.fixture
def link_project(logs_root: Path) -> Callable[[Path], None]:
    """
    Return a factory pointing a project's ``.claude/litellm-logs`` at its own hash dir.

    This symlink is what makes a project's spend its own rather than orphaned, so a test
    that wants an orphaned hash simply does not call this for that project.
    """

    def _link(project: Path) -> None:
        claude = project / ".claude"
        claude.mkdir(parents=True, exist_ok=True)
        (claude / LITELLM_LOGS_DIRNAME).symlink_to(logs_root / project_path_hash(project))

    return _link


@pytest.fixture
def index_logs(
    log_ingest_repository: LogIngestRepository,
    log_session_repository: SessionRepository,
    log_request_repository: RequestRepository,
    display_mock: Mock,
) -> Callable[[], None]:
    """
    Return a callable that ingests the whole central tree into the test's logs database.

    Built on the real ``LogsService.ingest_tree`` rather than on the parser directly, so
    what these tests read back is what ``agent reindex`` and the viewer would have put
    there. Its other dependencies are mocked because an ingest pass touches only the
    ingest repository.
    """
    service = LogsService(
        pricing_service=Mock(spec=PricingService),
        stats_service=Mock(spec=StatsService),
        config_service=Mock(spec=ConfigService),
        display_service=display_mock,
        log_ingest_repository=log_ingest_repository,
        log_session_repository=log_session_repository,
        log_request_repository=log_request_repository,
    )

    def _index() -> None:
        report = service.ingest_tree()
        assert report is not None, "ingest lock was held; nothing was indexed"
        assert report.ok, f"ingest failed: {report.failed}"

    return _index

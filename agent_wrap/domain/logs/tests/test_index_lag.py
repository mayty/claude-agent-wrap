# This file has been created with the assistance of an AI tool.
"""
Tests for reporting how far behind the log files the index is.

The only thing `agent stats` says when it cannot see a request: there is no fallback
that reads the log files to fill the gap, so the number here is the whole remedy. It is
computed from a ``stat()`` per indexed session plus one directory walk, and never opens
a log file — which is what lets a read-only command report staleness at all.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from agent_wrap.constants import LITELLM_LOGS_DIRNAME
from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.logs.constants import MESSAGES_FILENAME, STRINGS_FILENAME
from agent_wrap.domain.logs.service import LogsService
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.stats.service import StatsService

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_wrap.infrastructure.logs.repositories.ingest import LogIngestRepository
    from agent_wrap.infrastructure.logs.repositories.requests import RequestRepository
    from agent_wrap.infrastructure.logs.repositories.sessions import SessionRepository


@pytest.fixture
def logs_svc(
    display_mock: Mock,
    log_ingest_repository: LogIngestRepository,
    log_session_repository: SessionRepository,
    log_request_repository: RequestRepository,
) -> LogsService:
    """Return a LogsService over a real, empty logs database in ``tmp_path``."""
    return LogsService(
        pricing_service=Mock(spec=PricingService),
        stats_service=Mock(spec=StatsService),
        config_service=Mock(spec=ConfigService),
        display_service=display_mock,
        log_ingest_repository=log_ingest_repository,
        log_session_repository=log_session_repository,
        log_request_repository=log_request_repository,
    )


@pytest.fixture
def write_session(tmp_path: Path) -> Callable[..., Path]:
    """Return a factory appending records to one session of the central tree."""

    def _write(
        project_hash: str,
        records: list[dict[str, Any]],
        *,
        provider: str = "litellm-bedrock",
        session: str = "sess-1",
    ) -> Path:
        session_dir = tmp_path / LITELLM_LOGS_DIRNAME / project_hash / provider / session
        session_dir.mkdir(parents=True, exist_ok=True)
        with (session_dir / MESSAGES_FILENAME).open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        return session_dir

    return _write


def _rec() -> dict[str, Any]:
    return {
        "status": "success",
        "model": "bedrock/claude-opus-4-8",
        "timing": {"start": 1_800_000_000.0},
        "response": {"usage": {"input_tokens": 10, "output_tokens": 5}},
    }


def test_an_empty_host_is_not_lagging(logs_svc: LogsService) -> None:
    """No sessions and no index is up to date, not behind."""
    lag = logs_svc.index_lag()

    assert (lag.behind, lag.total) == (0, 0)
    assert lag.is_stale is False


def test_a_fully_indexed_tree_is_not_lagging(
    logs_svc: LogsService, write_session: Callable[..., Path]
) -> None:
    write_session("hashA", [_rec(), _rec()])
    write_session("hashB", [_rec()])
    logs_svc.ingest_tree()

    lag = logs_svc.index_lag()

    assert (lag.behind, lag.total) == (0, 2)
    assert lag.is_stale is False


def test_a_never_indexed_tree_is_entirely_behind(
    logs_svc: LogsService, write_session: Callable[..., Path]
) -> None:
    """
    The state of a host where the viewer has never run — the case that matters most.

    Every session counts as behind, so the warning says "613 of 613" rather than
    reporting nothing and letting an empty report read as zero spend.
    """
    write_session("hashA", [_rec()])
    write_session("hashB", [_rec()])

    lag = logs_svc.index_lag()

    assert (lag.behind, lag.total) == (2, 2)
    assert lag.is_stale is True


def test_records_appended_since_the_last_pass_count_as_behind(
    logs_svc: LogsService, write_session: Callable[..., Path]
) -> None:
    """A file grown past its own watermark is exactly one session's worth of lag."""
    write_session("hashA", [_rec()])
    write_session("hashB", [_rec()])
    logs_svc.ingest_tree()
    write_session("hashA", [_rec()])

    lag = logs_svc.index_lag()

    assert (lag.behind, lag.total) == (1, 2)


def test_a_new_session_counts_as_behind_and_toward_the_total(
    logs_svc: LogsService, write_session: Callable[..., Path]
) -> None:
    """
    A session the index has never seen is not in the watermark table at all.

    So it has to come from the directory walk, and it has to be added to *both* counts —
    reporting "1 of 1" on a host with one indexed session and one unseen one would
    understate how much is missing.
    """
    write_session("hashA", [_rec()])
    logs_svc.ingest_tree()
    write_session("hashB", [_rec()])

    lag = logs_svc.index_lag()

    assert (lag.behind, lag.total) == (1, 2)


def test_a_truncated_file_counts_as_behind(
    logs_svc: LogsService, write_session: Callable[..., Path]
) -> None:
    """
    A file *shorter* than its watermark was replaced, so the index holds stale rows.

    Compared by inequality rather than by ``>``: `agent reindex` is the fix for this too
    (it re-reads a truncated session from zero), so it belongs in the same count rather
    than in a second kind of warning nobody would act on differently.
    """
    session_dir = write_session("hashA", [_rec(), _rec(), _rec()])
    logs_svc.ingest_tree()
    with (session_dir / MESSAGES_FILENAME).open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(_rec()) + "\n")

    lag = logs_svc.index_lag()

    assert (lag.behind, lag.total) == (1, 1)


def test_a_deleted_session_is_not_lag(
    logs_svc: LogsService, write_session: Callable[..., Path]
) -> None:
    """
    Rows for a directory that is gone are `agent cleanup`'s problem, not reindex's.

    Counting them would make the warning permanent on any host that had ever cleaned
    up, and tell the user to run a command that cannot fix it.
    """
    session_dir = write_session("hashA", [_rec()])
    write_session("hashB", [_rec()])
    logs_svc.ingest_tree()
    (session_dir / MESSAGES_FILENAME).unlink()
    (session_dir / STRINGS_FILENAME).unlink(missing_ok=True)
    session_dir.rmdir()

    lag = logs_svc.index_lag()

    assert lag.behind == 0


def test_lag_reads_no_log_file(
    logs_svc: LogsService, write_session: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The load-bearing property: staleness is answered by metadata, never by parsing.

    Asserted by making every open of a log file fail. If this ever needs relaxing, the
    thing to question is the change, not the test — a read-only command that opens the
    log tree is the fallback path the whole index exists to remove.
    """
    write_session("hashA", [_rec()])
    logs_svc.ingest_tree()
    write_session("hashA", [_rec()])
    real_open = Path.open

    def refuse(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name in (MESSAGES_FILENAME, STRINGS_FILENAME):
            msg = f"index_lag opened {self.name}"
            raise AssertionError(msg)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refuse)

    assert logs_svc.index_lag().behind == 1

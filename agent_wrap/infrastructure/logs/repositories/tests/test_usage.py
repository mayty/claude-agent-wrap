# This file has been created with the assistance of an AI tool.
"""
Tests for the usage read side: what the stats aggregate returns, and at what grain.

Rows are inserted directly rather than through the parser, because what is under test
here is the SQL — which rows a window selects, how they group, and what the cell carries
out. The parser's own agreement with the schema is tested next door in
``test_ingest.py``, and end to end in the stats domain's golden report.
"""

from typing import TYPE_CHECKING

import pytest

from agent_wrap.infrastructure.logs.repositories.usage import UsageRepository

if TYPE_CHECKING:
    import sqlite3

    from agent_wrap.containers import Core

# One hour bucket, spelled as hours since the epoch. 2026-07-20T06:00Z.
_HOUR = 1_784_275_200 // 3600
_MICROS_PER_HOUR = 3_600_000_000


@pytest.fixture
def usage(db_core: Core) -> UsageRepository:
    return UsageRepository(connection_factory=db_core.logs_db)


def _seed_session(
    connection: sqlite3.Connection,
    session_id: int = 1,
    *,
    project_hash: str = "hashA",
    provider: str = "litellm-bedrock",
) -> None:
    connection.execute(
        "INSERT INTO sessions (id, project_hash, provider, claude_session_id, last_ingested_at)"
        " VALUES (?, ?, ?, ?, 0)",
        (session_id, project_hash, provider, f"sess-{session_id}"),
    )


def _insert_request(connection: sqlite3.Connection, **overrides: object) -> None:
    """Insert one `requests` row, defaulting to a priced success in ``_HOUR``."""
    row: dict[str, object] = {
        "session_id": 1,
        "ordinal": 0,
        "status": "success",
        "model": "bedrock/claude-opus-4-8",
        "usage_source": "native",
        "started_at_us": _HOUR * _MICROS_PER_HOUR,
        "message_refs": b"",
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_write_tokens": 0,
        "cache_write_5m": 0,
        "cache_write_1h": 0,
        "cache_read": 0,
    }
    row.update(overrides)
    columns = ", ".join(row)
    placeholders = ", ".join(f":{name}" for name in row)
    # S608: the column names are this function's own literals and every value is bound.
    sql = f"INSERT INTO requests ({columns}) VALUES ({placeholders})"  # noqa: S608
    connection.execute(sql, row)


@pytest.fixture
def seeded(db_core: Core) -> None:
    """Seed one session holding two requests in the same cell and one in another hour."""
    factory = db_core.logs_db
    with factory.enable_writes(), factory.rw() as connection:
        _seed_session(connection)
        _insert_request(connection, ordinal=0)
        _insert_request(connection, ordinal=1)
        _insert_request(
            connection, ordinal=2, started_at_us=(_HOUR + 1) * _MICROS_PER_HOUR + 90_000_000
        )


@pytest.mark.usefixtures("seeded")
def test_requests_in_one_hour_and_model_collapse_to_one_cell(usage: UsageRepository) -> None:
    """
    The grain: two requests an hour apart are two cells, two in the same hour are one.

    This is what turns 44k rows into ~1.8k on the real tree, and it is why the fold
    must merge a cell rather than add it -- ``requests`` is 2 here, not 1.
    """
    cells = usage.usage_cells()

    assert len(cells) == 2
    first = next(cell for cell in cells if cell.hour_bucket == _HOUR)
    assert (first.requests, first.input_tokens, first.output_tokens) == (2, 200, 100)


@pytest.mark.usefixtures("seeded")
def test_a_cell_carries_the_exact_newest_instant(usage: UsageRepository) -> None:
    """
    ``last_started_at_us`` is the request's own microsecond, not the hour's start.

    The hour bucket is deliberately too coarse for the "LAST" column: truncating to it
    is wrong by up to an hour, which in a half-hour-offset timezone is enough to move
    the rendered local date by a day.
    """
    cells = usage.usage_cells()

    newest = max(cell.last_started_at_us or 0 for cell in cells)
    assert newest == (_HOUR + 1) * _MICROS_PER_HOUR + 90_000_000


def test_a_cell_is_labelled_with_its_sessions_project_and_provider(
    db_core: Core, usage: UsageRepository
) -> None:
    """
    Both labels come from the session row, and the provider is the sidecar's name.

    The provider is what a model renders under, and it is *not* the vendor prefix inside
    ``model`` -- the same upstream model reached through two sidecars is two rows in the
    report.
    """
    factory = db_core.logs_db
    with factory.enable_writes(), factory.rw() as connection:
        _seed_session(connection, 1, project_hash="hashA", provider="litellm-bedrock")
        _seed_session(connection, 2, project_hash="hashB", provider="litellm-deepseek")
        _insert_request(connection, session_id=1)
        _insert_request(connection, session_id=2)

    labels = {(cell.project_hash, cell.provider) for cell in usage.usage_cells()}

    assert labels == {("hashA", "litellm-bedrock"), ("hashB", "litellm-deepseek")}


def test_failures_are_not_counted(db_core: Core, usage: UsageRepository) -> None:
    """A failed request billed nothing, and the index is partial on exactly that."""
    factory = db_core.logs_db
    with factory.enable_writes(), factory.rw() as connection:
        _seed_session(connection)
        _insert_request(connection, ordinal=0, status="failure", input_tokens=9999)

    assert usage.usage_cells() == []


def test_a_request_with_no_model_is_dropped(db_core: Core, usage: UsageRepository) -> None:
    """
    Matches the file scan this replaced, which dropped it silently in Python.

    Pinned rather than endorsed: an unattributable request arguably deserves an
    "unknown" row. Changing that is a change to reported spend, so it is not this one.
    """
    factory = db_core.logs_db
    with factory.enable_writes(), factory.rw() as connection:
        _seed_session(connection)
        _insert_request(connection, ordinal=0, model="")
        _insert_request(connection, ordinal=1)

    assert len(usage.usage_cells()) == 1


def test_usage_source_splits_cells_rather_than_summing_them(
    db_core: Core, usage: UsageRepository
) -> None:
    """
    A group key, so a whole cell is unrecoverable or none of it is.

    That is what lets the unrecorded footnote count requests without a per-record flag.
    """
    factory = db_core.logs_db
    with factory.enable_writes(), factory.rw() as connection:
        _seed_session(connection)
        _insert_request(connection, ordinal=0, usage_source="native")
        _insert_request(connection, ordinal=1, usage_source="unrecoverable")

    sources = {cell.usage_source: cell.requests for cell in usage.usage_cells()}

    assert sources == {"native": 1, "unrecoverable": 1}


@pytest.mark.usefixtures("seeded")
@pytest.mark.parametrize(
    ("from_hour", "until_hour", "expected"),
    [
        (None, None, {_HOUR, _HOUR + 1}),
        (_HOUR, None, {_HOUR, _HOUR + 1}),
        (_HOUR + 1, None, {_HOUR + 1}),
        (None, _HOUR, {_HOUR}),
        (_HOUR, _HOUR, {_HOUR}),
        (_HOUR + 2, None, set()),
    ],
)
def test_the_hour_bounds_are_inclusive_on_both_sides(
    usage: UsageRepository, from_hour: int | None, until_hour: int | None, expected: set[int]
) -> None:
    """An open side is None, and a given side includes its own hour."""
    cells = usage.usage_cells(from_hour=from_hour, until_hour=until_hour)

    assert {cell.hour_bucket for cell in cells} == expected


def test_a_request_with_no_timestamp_is_its_own_cell(db_core: Core, usage: UsageRepository) -> None:
    """A NULL instant groups to a NULL bucket, which is what feeds the "?" day key."""
    factory = db_core.logs_db
    with factory.enable_writes(), factory.rw() as connection:
        _seed_session(connection)
        _insert_request(connection, ordinal=0, started_at_us=None)

    cells = usage.usage_cells()

    assert len(cells) == 1
    assert cells[0].hour_bucket is None
    assert cells[0].last_started_at_us is None


@pytest.mark.parametrize(("from_hour", "until_hour"), [(_HOUR, None), (None, _HOUR)])
def test_a_request_with_no_timestamp_drops_out_of_any_bounded_window(
    db_core: Core, usage: UsageRepository, from_hour: int | None, until_hour: int | None
) -> None:
    """
    It belongs to no hour, so it cannot be compared to a bound.

    Written as ``:lo IS NULL OR bucket >= :lo`` rather than as an OR-ed IS NULL for
    exactly this: a NULL bucket fails the comparison and is excluded, which is what the
    stats layer's ``"?"`` key does with it in the all-time view only.
    """
    factory = db_core.logs_db
    with factory.enable_writes(), factory.rw() as connection:
        _seed_session(connection)
        _insert_request(connection, ordinal=0, started_at_us=None)

    assert usage.usage_cells(from_hour=from_hour, until_hour=until_hour) == []


def test_an_empty_index_yields_no_cells(usage: UsageRepository) -> None:
    """The state of a host whose logs have never been indexed."""
    assert usage.usage_cells() == []


def test_the_cache_split_is_returned_alongside_the_flat_total(
    db_core: Core, usage: UsageRepository
) -> None:
    """
    Both are carried out, because the consumer deliberately spends only one of them.

    ``usage_from_cell`` charges the whole flat total at the 5m rate today; the split is
    stored and returned so correcting that is a pricing change rather than a re-ingest.
    """
    factory = db_core.logs_db
    with factory.enable_writes(), factory.rw() as connection:
        _seed_session(connection)
        _insert_request(
            connection,
            ordinal=0,
            cache_write_tokens=3200,
            cache_write_5m=1200,
            cache_write_1h=2000,
            cache_read=1500,
        )

    cell = usage.usage_cells()[0]

    assert (cell.cache_write_tokens, cell.cache_write_5m, cell.cache_write_1h) == (3200, 1200, 2000)
    assert cell.cache_read == 1500

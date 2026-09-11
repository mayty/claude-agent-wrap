# This file has been created with the assistance of an AI tool.
"""
Tests for collapsing indexed session rows into the lists the viewer renders.

One row per session *directory* goes in; one entry per session the user recognizes comes
out. The rules that matter are the merge across providers and across the member projects
of a grouped transient project, and the fingerprint the browser polls -- all of which
used to be a walk of the log tree and a ``stat()`` of every record file in it.
"""

from agent_wrap.domain.logs.listing import fingerprint, merge_sessions, rows_by_hash
from agent_wrap.infrastructure.logs.models import SessionKey, SessionRow


def _row(  # noqa: PLR0913 -- one keyword per column of the row under test
    *,
    project_hash: str = "hashA",
    provider: str = "litellm-bedrock",
    session: str = "sess-1",
    last_ingested_ns: int = 1_000,
    record_count: int = 1,
    last_event_at_us: int | None = 1_784_275_200_000_000,
    models: tuple[str, ...] = ("claude-opus-4-8",),
    alias: str | None = None,
    title: str | None = None,
) -> SessionRow:
    return SessionRow(
        key=SessionKey(project_hash=project_hash, provider=provider, claude_session_id=session),
        last_ingested_ns=last_ingested_ns,
        record_count=record_count,
        last_event_at_us=last_event_at_us,
        models=models,
        alias=alias,
        title=title,
    )


def test_one_row_becomes_one_entry() -> None:
    rows = [_row(record_count=4, models=("m1", "m2"), alias="slug", title="Title")]

    assert merge_sessions(rows) == [
        {
            "session_id": "sess-1",
            "alias": "slug",
            "title": "Title",
            "count": 4,
            "last_ts": 1_784_275_200.0,
            "models": ["m1", "m2"],
            "providers": ["litellm-bedrock"],
        }
    ]


def test_the_stored_microseconds_become_the_epoch_seconds_the_page_renders() -> None:
    """
    The index is exact to the microsecond; the wire format the viewer reads is seconds.

    Sub-second precision is kept rather than truncated: the sessions list sorts on this
    value, and two sessions started in the same second would otherwise tie and order
    arbitrarily between refreshes.
    """
    rows = [_row(last_event_at_us=1_784_275_200_123_456)]

    assert merge_sessions(rows)[0]["last_ts"] == 1_784_275_200.123456


def test_a_session_served_by_two_providers_is_one_entry() -> None:
    """A mid-session provider switch is two directories and one conversation."""
    rows = [
        _row(provider="litellm-bedrock", record_count=2, models=("opus",)),
        _row(provider="litellm-dashscope", record_count=3, models=("qwen",)),
    ]

    (merged,) = merge_sessions(rows)

    assert merged["providers"] == ["litellm-bedrock", "litellm-dashscope"]
    assert merged["count"] == 5
    assert merged["models"] == ["opus", "qwen"]


def test_a_session_shared_by_two_group_members_is_counted_once() -> None:
    """
    Two hashes, one session: a grouped transient project whose members share a session.

    This is the double-count the viewer's session number used to disagree with its own
    drill-down over -- the rows are per directory, and the entry is per session.
    """
    rows = [
        _row(project_hash="hashA", record_count=2),
        _row(project_hash="hashB", record_count=1),
    ]

    merged = merge_sessions(rows)

    assert len(merged) == 1
    assert merged[0]["count"] == 3


def test_the_newest_instant_wins_across_rows() -> None:
    rows = [
        _row(provider="p1", last_event_at_us=2_000_000),
        _row(provider="p2", last_event_at_us=9_000_000),
        _row(provider="p3", last_event_at_us=5_000_000),
    ]

    assert merge_sessions(rows)[0]["last_ts"] == 9.0


def test_a_row_with_no_instant_does_not_erase_one() -> None:
    rows = [
        _row(provider="p1", last_event_at_us=7_000_000),
        _row(provider="p2", last_event_at_us=None),
    ]

    assert merge_sessions(rows)[0]["last_ts"] == 7.0


def test_a_label_found_under_one_provider_survives_the_merge() -> None:
    """An alias arrives on a single naming call, which one provider served."""
    rows = [
        _row(provider="litellm-anthropic", alias=None, title=None),
        _row(provider="litellm-bedrock", alias="slug", title="Title"),
    ]

    (merged,) = merge_sessions(rows)

    assert (merged["alias"], merged["title"]) == ("slug", "Title")


def test_the_label_is_the_same_whatever_order_the_rows_arrive_in() -> None:
    """
    Reproducible, because the rows are sorted by key before they are merged.

    Two providers that each named the session disagree, and a table scan makes no
    promise about which comes back first -- so without the sort a session could change
    its label between two refreshes with nothing having happened.
    """
    first = _row(provider="litellm-anthropic", alias="from-anthropic")
    second = _row(provider="litellm-bedrock", alias="from-bedrock")

    assert merge_sessions([first, second]) == merge_sessions([second, first])
    assert merge_sessions([second, first])[0]["alias"] == "from-anthropic"


def test_sessions_come_back_newest_first() -> None:
    rows = [
        _row(session="old", last_event_at_us=1_000_000),
        _row(session="new", last_event_at_us=3_000_000),
        _row(session="mid", last_event_at_us=2_000_000),
    ]

    assert [meta["session_id"] for meta in merge_sessions(rows)] == ["new", "mid", "old"]


def test_a_session_with_no_records_is_not_listed() -> None:
    """
    Ingest writes a row for a session whose only new content was interned strings.

    Listing it would put an entry in the sidebar that opens to nothing, which is the
    same reason the file scan this replaces skipped a session directory it found no
    parseable record in.
    """
    assert merge_sessions([_row(record_count=0)]) == []


def test_an_empty_provider_is_dropped_with_its_row() -> None:
    rows = [
        _row(provider="litellm-bedrock", record_count=2),
        _row(provider="litellm-dashscope", record_count=0),
    ]

    assert merge_sessions(rows)[0]["providers"] == ["litellm-bedrock"]


def test_no_rows_means_no_sessions() -> None:
    assert merge_sessions([]) == []


def test_the_fingerprint_is_the_newest_ingest_and_the_row_count() -> None:
    rows = [_row(last_ingested_ns=1_000), _row(session="s2", last_ingested_ns=4_000)]

    assert fingerprint(rows) == {"rev": 4_000, "count": 2}


def test_an_empty_scope_fingerprints_as_nothing_ingested() -> None:
    """
    The shape a project with no indexed sessions answers with.

    Not an error and not omitted: the browser polls this endpoint for every project it
    lists, so the empty answer has to be a fingerprint that simply never changes.
    """
    assert fingerprint([]) == {"rev": None, "count": 0}


def test_the_fingerprint_counts_a_session_with_no_records() -> None:
    """
    It describes the index, not the rendered list.

    The row is real, and its first record is what moves ``rev`` -- so counting it costs
    nothing and leaving it out would make the marker disagree with the table it summarizes.
    """
    assert fingerprint([_row(record_count=0)]) == {"rev": 1_000, "count": 1}


def test_rows_are_grouped_by_the_project_that_owns_them() -> None:
    rows = [
        _row(project_hash="hashA", session="s1"),
        _row(project_hash="hashB", session="s2"),
        _row(project_hash="hashA", session="s3"),
    ]

    grouped = rows_by_hash(rows)

    assert set(grouped) == {"hashA", "hashB"}
    assert {row.key.claude_session_id for row in grouped["hashA"]} == {"s1", "s3"}


def test_grouping_nothing_yields_nothing() -> None:
    assert rows_by_hash([]) == {}

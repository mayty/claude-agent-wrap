# This file has been created with the assistance of an AI tool.
"""Tests for the project registry repository."""

from typing import TYPE_CHECKING

from agent_wrap.infrastructure.projects.models import Project

if TYPE_CHECKING:
    from agent_wrap.infrastructure.projects.repositories.projects import ProjectsRepository


def test_list_projects_is_empty_on_a_fresh_database(
    projects_repository: ProjectsRepository,
) -> None:
    assert projects_repository.list_projects() == []


def test_record_round_trips(projects_repository: ProjectsRepository) -> None:
    projects_repository.record("/home/dev/thing")

    projects = projects_repository.list_projects()

    assert [p.path for p in projects] == ["/home/dev/thing"]
    assert isinstance(projects[0], Project)
    assert projects[0].first_seen_at == projects[0].last_seen_at


def test_list_projects_is_path_ordered(projects_repository: ProjectsRepository) -> None:
    """
    The logs viewer derives group ids from this sequence's positions.

    Left to the database the order would be arbitrary, and a project's id would shift
    under the browser between requests.
    """
    for path in ("/z/last", "/a/first", "/m/middle"):
        projects_repository.record(path)

    assert [p.path for p in projects_repository.list_projects()] == [
        "/a/first",
        "/m/middle",
        "/z/last",
    ]


def test_record_is_an_upsert(projects_repository: ProjectsRepository) -> None:
    projects_repository.record("/home/dev/thing")
    first = projects_repository.list_projects()[0]

    projects_repository.record("/home/dev/thing")
    second = projects_repository.list_projects()[0]

    assert len(projects_repository.list_projects()) == 1
    assert second.first_seen_at == first.first_seen_at
    assert second.last_seen_at >= first.last_seen_at


def test_record_refreshes_last_seen(projects_repository: ProjectsRepository) -> None:
    """The viewer's ETag reads this, the way it used to read the registry file's mtime."""
    projects_repository.record("/home/dev/thing")
    before = projects_repository.revision()

    projects_repository.record("/home/dev/thing")

    assert projects_repository.revision().last_change_ns != before.last_change_ns


def test_record_many_registers_every_path(projects_repository: ProjectsRepository) -> None:
    projects_repository.record_many(["/z/last", "/a/first"])

    assert [p.path for p in projects_repository.list_projects()] == ["/a/first", "/z/last"]


def test_record_many_stamps_one_timestamp_across_the_batch(
    projects_repository: ProjectsRepository,
) -> None:
    """
    One clock reading, one transaction.

    Recording the paths one at a time would give each its own ``last_seen_at``; a single
    shared value is the observable consequence of the batch being written together.
    """
    projects_repository.record_many(["/a", "/b", "/c"])

    stamps = {p.last_seen_at for p in projects_repository.list_projects()}
    assert len(stamps) == 1


def test_record_many_upserts_over_existing_rows(projects_repository: ProjectsRepository) -> None:
    """The legacy import may run against a table another process has already seeded."""
    projects_repository.record("/a")
    first = projects_repository.list_projects()[0]

    projects_repository.record_many(["/a", "/b"])

    projects = projects_repository.list_projects()
    assert [p.path for p in projects] == ["/a", "/b"]
    assert projects[0].first_seen_at == first.first_seen_at


def test_record_many_of_nothing_is_a_no_op(projects_repository: ProjectsRepository) -> None:
    projects_repository.record("/a")

    projects_repository.record_many([])

    assert [p.path for p in projects_repository.list_projects()] == ["/a"]


def test_delete_removes_only_the_named_paths(projects_repository: ProjectsRepository) -> None:
    for path in ("/a", "/b", "/c"):
        projects_repository.record(path)

    projects_repository.delete(["/a", "/c"])

    assert [p.path for p in projects_repository.list_projects()] == ["/b"]


def test_delete_ignores_unknown_paths(projects_repository: ProjectsRepository) -> None:
    projects_repository.record("/a")

    projects_repository.delete(["/never-registered"])

    assert [p.path for p in projects_repository.list_projects()] == ["/a"]


def test_delete_of_nothing_is_a_no_op(projects_repository: ProjectsRepository) -> None:
    projects_repository.record("/a")

    projects_repository.delete([])

    assert [p.path for p in projects_repository.list_projects()] == ["/a"]


def test_revision_of_an_empty_table(projects_repository: ProjectsRepository) -> None:
    assert projects_repository.revision() == (None, 0)


def test_revision_counts_rows(projects_repository: ProjectsRepository) -> None:
    for path in ("/a", "/b"):
        projects_repository.record(path)

    assert projects_repository.revision().count == 2


def test_revision_changes_on_delete(projects_repository: ProjectsRepository) -> None:
    for path in ("/a", "/b"):
        projects_repository.record(path)
    before = projects_repository.revision()

    projects_repository.delete(["/b"])

    assert projects_repository.revision() != before

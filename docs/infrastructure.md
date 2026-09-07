<!-- This file has been created with the assistance of an AI tool. -->
# Infrastructure layer

The storage layer. SQLite databases, their migrations, and the repositories that read
and write them. It lives in [`agent_wrap/infrastructure/`](../agent_wrap/infrastructure)
and sits *below* [`agent_wrap/domain/`](../agent_wrap/domain): the domain calls into it,
it never calls back.

The point of the boundary is that this directory can be reshaped — a new column, a
different table, a different database entirely — without any other layer changing. That
holds only as long as the rules below do.

## Layout

Shared machinery at the root; one subpackage per database beneath it, named after its
`Databases` member.

```
infrastructure/
├── constants.py        # Databases, PRAGMAs, the migration filename grammar, dir names
├── models.py           # MigrationFile
├── connection.py       # ConnectionFactory -- rw() / ro(), migrates on construction
├── migrations.py       # MigrationRunner -- discover, back up, apply, stamp
├── tests/              # tests for the machinery above, against throwaway scripts
└── projects/           # one database, named for Databases.PROJECTS
    ├── models.py       #   Project, Revision -- the app objects it deals in
    ├── migrations/     #   NNNN_<slug>.sql, applied in order
    │   └── 0001_create_projects.sql
    ├── repositories/
    │   ├── projects.py #   ProjectsRepository
    │   └── tests/
    └── tests/          #   that this database's own scripts apply
```

A database subpackage carries no path constants of its own. `Databases.PROJECTS` is
simultaneously the subpackage's directory name, the database file's stem and the prefix
of its backup filenames, so every path is derived at the composition root from the enum
member alone.

## Rules

1. **Nothing here imports `agent_wrap.domain.*` or `agent_wrap.cli.*`** — at runtime or
   under `TYPE_CHECKING`. Depending upward would make the boundary decorative.
2. **The domain reaches a repository by constructor injection only.** A domain service
   receives one from [`containers.py`](../agent_wrap/containers.py) and annotates it with
   a `TYPE_CHECKING` import; a runtime import from a domain module is a layering break.
   `containers.py` is the composition root and is the sole exception.
3. **Repositories return app objects, never storage shapes.** No `sqlite3.Row`, no
   column names, no SQL leaves this directory, and nothing above ever holds a
   `sqlite3.Connection`.
4. **A database subpackage owns its migrations, repositories and models.** Nothing
   outside it names its tables or columns.
5. **Paths are arguments, never module constants.** `ConnectionFactory` takes its
   database, migrations and backups paths from its caller, which is what lets a test
   point a whole tree at `tmp_path` by building its own `Core`.
6. **`models.py` for data classes, `constants.py` for constants** — the same split the
   domain follows. Note that `make arch-check` does **not** enforce it here: its
   `_models_constants_scope` allow-list is `("domain", "cli")`, so ED001/EE001/EF001 are
   silent under this directory. EB001 (private-name imports) and EG001 (no
   `from __future__ import annotations`) are global and do apply.

## Connections

`ConnectionFactory` opens one database. Constructing it runs that database's pending
migrations, so holding a factory means holding a database at its head version — and
because `Core` exposes each factory as a `@cached_property`, a command that never
touches a database never opens or migrates one.

- `rw()` wraps the block in one transaction: commit on success, rollback on any
  exception. Refuses outright without a write grant — see below.
- `ro()` yields a connection with `PRAGMA query_only = ON`, and needs no grant.

`ro()` deliberately does not use a `file:...?mode=ro` URI. Under WAL a read-only open
needs the `-shm` file to already exist and fails outright when it does not — exactly the
logs daemon's cold start, a second process that may open the database before any writer
has run since boot. `query_only` gives the same guarantee with no such failure mode.

Databases run in WAL so the logs daemon's reads never block on a launch's write. Every
`sqlite3.Error` escaping a factory becomes a
[`StorageError`](../agent_wrap/exceptions.py). That matters on the registry write path,
which must never fail a launch: `sqlite3.OperationalError` is not an `OSError`, so the
`except OSError` guards there would sail straight past it.

## Write permission

`rw()` refuses unless the process is inside an explicit grant for that database:

```python
with core.projects_db.enable_writes():
    ...
```

Outside one it raises `WritesNotEnabledError`, and grants **do not nest** — a second one
is an error rather than a wider permission.

The point is to make read-only consumers read-only *structurally*, rather than by
discipline about which call sites may write. The `agent logs` viewer runs in its own
re-exec'd process (`agent logs --foreground`) whose whole job is to read the registry on
filesystem events; it never takes a grant, so no read it performs can mutate the
database however deep the call goes. That matters because the one-time `projects.txt`
import is reached *from* a read — `ConfigService.read_project_paths()` triggers it when
the table is empty — and only the gate keeps the daemon from performing it.

Three properties of the design worth knowing:

- **The refusal is a `StorageError`.** That is the mechanism, not a convenience: every
  best-effort registry guard already catches `StorageError`, so an ungranted process
  degrades to "no registry" instead of crashing.
- **The flag is per factory and process-wide, not thread-local.** A grant taken at a
  command's entry point has to cover the worker threads that command spawns
  (`InspectService.build_report` probes in a thread pool). The refusal to nest is what
  keeps that comprehensible: one window, opened once at the top.
- **Schema migrations are not gated.** They run in `ConnectionFactory.__init__`, not
  through `rw()`, so a read-only consumer still converges the schema. Gating them would
  leave it unable to open a database that does not exist yet — a crash rather than a
  degradation — and schema convergence was never the hazard; unpermitted *data* writes
  were.

Grants live in the CLI command entries, where "this is a user-invoked foreground
invocation" is a fact rather than an assumption, and each one wraps the phase that
writes rather than the whole command:

- **`agent run`** wraps the `launch()` call. Its write is `record_project()`, three
  levels down in `LaunchService._prepare_config`, so that call is the narrowest window
  the CLI layer can express.
- **`agent cleanup`** takes two, one per writing phase. The survey's grant exists only so
  the one-time `projects.txt` import lands *before* the scope is computed — the scope the
  clean phase deletes from is built there, and an un-imported registry makes every
  project's log dir look orphaned. The clean phase's grant exists for the stale-entry
  prune. `--dry-run` withholds the first and never reaches the second.
- **`agent stats` and `agent inspect`** deliberately get none: a command that only
  reports does not receive write permission in order to migrate data.

The grant cannot move down to the write site, tempting as that looks. A grant taken next
to the write is a statement about the *operation*, which every process satisfies equally
— the legacy import would then grant itself and the daemon would perform it, which is the
defect the gate exists to prevent. It has to stay a statement about the invocation.

Nor can a grant be dropped just because a phase "runs against an already-migrated
database". `prune_stale_projects` suppresses `StorageError`, and the refusal *is* one, so
an ungranted prune returns the paths as though it had removed them and the caller reports
a prune that never happened. `WritesNotEnabledError` degrading quietly is the right
default for a launch; it means a missing grant elsewhere shows up as a wrong report
rather than a crash. Which verbs and phases hold grants is therefore asserted in
`agent_wrap/cli/*/tests/` — in both directions, since a stray grant on `--dry-run` or
`--foreground` is as much a bug as a missing one.

## Migrations

Numbered SQL scripts, and nothing else — there is no Python escape hatch by design. A
step that needs application logic (reading a legacy file, calling a service) is not a
migration; it is application code that runs on top of the finished schema. The one such
case so far is `ConfigService._import_legacy_registry`, which copies a pre-SQLite
`projects.txt` into an empty projects table — reached from a read, and gated by the write
permission above rather than by where it is called from.

The applied version is `PRAGMA user_version`, which lives in the database header and is
transactional. Each script is executed inside the same `BEGIN`/`COMMIT` as its own
version stamp, so there is no state in which the database claims a version it did not
reach.

Before each step, the runner copies the database to
`.agent-launches/db/backups/<database>-v<from>-<UTC timestamp>.db` using SQLite's online
backup API (a file copy could tear against a concurrent writer). Backups are never
pruned — the one that matters is the one you did not expect to need. A database with
nothing in it yet is skipped, judged by an empty `sqlite_master` rather than by file
size: the factory sets `journal_mode` before migrating, which writes the header, so a
size check would pass on every fresh install and bury the real backups under one useless
copy per machine.

## Adding a database

1. Add a member to `Databases` in `constants.py`, then create
   `infrastructure/<that value>/` with `models.py`, `migrations/0001_….sql`,
   `repositories/` and `tests/`. The directory name must match the member's value —
   that is what makes its migrations path derivable.
2. Add a `@cached_property` to `Core` in [`containers.py`](../agent_wrap/containers.py)
   returning a `ConnectionFactory` for it, and one to `Repositories` for each repository
   over it.
3. Inject the repository into the domain service that needs it, in `Services`.

## Adding a migration

Drop the next `NNNN_<slug>.sql` into that database's `migrations/`. Versions must run
contiguously from 1 — a gap or a duplicate is refused up front rather than half-applied,
because either would leave `user_version` describing a schema the database does not
have. Never edit a script that has shipped: it has already run on other people's
machines, and only the next number can change what it did.

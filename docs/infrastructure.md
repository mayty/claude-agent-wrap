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
├── projects/           # one database, named for Databases.PROJECTS
│   ├── models.py       #   Project, Revision -- the app objects it deals in
│   ├── migrations/     #   NNNN_<slug>.sql, applied in order
│   │   └── 0001_create_projects.sql
│   ├── repositories/
│   │   ├── projects.py #   ProjectsRepository
│   │   └── tests/
│   └── tests/          #   that this database's own scripts apply
└── logs/               # one database, named for Databases.LOGS
    ├── models.py       #   SessionKey, SessionRow, UsageCell, IngestChunk, ... -- see the file
    ├── constants.py    #   the blob codec's thresholds and typecodes
    ├── migrations/
    │   └── 0001_create_log_index.sql
    ├── repositories/
    │   ├── ingest.py   #   LogIngestRepository -- every write, plus the watermarks
    │   ├── sessions.py #   SessionRepository -- the viewer's project/session lists
    │   ├── requests.py #   RequestRepository -- one open session's records and content
    │   ├── usage.py    #   UsageRepository -- the one aggregate `agent stats` runs
    │   └── tests/
    └── tests/
```

A database subpackage carries no path constants of its own. `Databases.PROJECTS` is
simultaneously the subpackage's directory name, the database file's stem and the prefix
of its backup filenames, so every path is derived at the composition root from the enum
member alone.

The two databases hold different kinds of thing and are sized accordingly. `projects.db`
is a registry of a few dozen rows; `logs.db` is a content-addressed blob store of a few
hundred megabytes, ingested from the sidecar's append-only JSONL tree and rebuildable
from it at any time. That is why the PRAGMA sets are per database — see
[Connections](#connections).

## Rules

1. **Nothing here imports `agent_wrap.domain.*` or `agent_wrap.cli.*`** — at runtime or
   under `TYPE_CHECKING`. Depending upward would make the boundary decorative.
2. **The domain reaches a repository by constructor injection only.** A domain service
   receives one from [`containers.py`](../agent_wrap/containers.py) and annotates it with
   a `TYPE_CHECKING` import; a runtime import of a *repository* from a domain module is a
   layering break. `containers.py` is the composition root and is the sole exception.

   **Value types are not repositories.** A database subpackage's `models.py` may be
   imported directly and at runtime — `agent_wrap/domain/logs/ingest.py` builds the
   `IngestChunk` it hands to `LogIngestRepository`, and it cannot construct one without
   importing it. What the rule protects is how a *collaborator* is acquired, not whether
   a layer may name a shape defined below it: the dependency still points downward, stays
   acyclic, and bypasses no injection. Keeping the type next to the SQL that defines it
   is also what rule 4 asks for. Note that `make arch-check` enforces none of this —
   EA001 covers cross-*domain* imports only, so this boundary is convention.
3. **Repositories return app objects, never storage shapes.** No `sqlite3.Row`, no
   column names, no SQL leaves this directory, and nothing above ever holds a
   `sqlite3.Connection`. Content counts as an app value: `RequestRepository` returns a
   blob's *decoded text*, never a payload plus a codec, because which codec a row used
   is a storage decision and no caller has a use for it.

   **`requests` is read a row at a time in exactly one place.** `UsageRepository` may
   only aggregate — that is what replaced a 2.2 GB re-parse of the log tree, and keeping
   it aggregate-only is what stops a consumer reintroducing one row at a time.
   `RequestRepository` is the deliberate exception, and its scope is the reason it is
   allowed: one session the user has opened, which is a few hundred rows.

   The blob sweep reads every one of them and is not an exception to the rule it
   qualifies: nothing crosses the boundary. It walks the whole table because it has to
   — the references it follows are inside a packed vector no aggregate can open — and
   what it returns is two numbers. See [The blob sweep](#the-blob-sweep).
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

PRAGMAs are constructor arguments too, defaulting to the shared `DATABASE_PRAGMAS` /
`CONNECTION_PRAGMAS`. `logs.db` overrides both, and the persistent set is **order
sensitive in a way that fails silently**: `page_size` and `auto_vacuum` only take effect
while the database has no pages, and `journal_mode = WAL` writes the header, so WAL must
come last. With WAL first, a fresh `logs.db` keeps 4 KB pages and no auto-vacuum and
raises nothing. For that reason `LOGS_DATABASE_PRAGMAS` is a fresh literal tuple and must
never be spelled `DATABASE_PRAGMAS + (...)`. `infrastructure/logs/tests/` asserts the
resulting values rather than the constant, since only the values prove the order held.

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
discipline about which call sites may write. A grant is **per database**, and the `agent
logs` viewer is why that matters. It runs in its own re-exec'd process (`agent logs
--foreground`) that reads the registry on every filesystem event and *writes* the request
index on every one — so it takes a `logs_db` grant and no `projects_db` grant, and no
read it performs can mutate the registry however deep the call goes. That matters because
the one-time `projects.txt` import is reached *from* a read —
`ConfigService.read_project_paths()` triggers it when the table is empty — and only the
gate keeps the daemon from performing it.

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
- **`agent cleanup`** takes three: one registry grant per writing phase, plus a `logs_db`
  grant on the clean phase, which covers all three of that phase's index writes —
  forgetting a deleted project's requests, deleting the sessions retention has expired,
  and sweeping the content both of them held. The survey's registry grant exists only so the one-time
  `projects.txt` import lands *before* the scope is computed — the scope the clean phase
  deletes from is built there, and an un-imported registry makes every project's log dir
  look orphaned. The clean phase's registry grant exists for the stale-entry prune, and
  its logs grant for forgetting a deleted project's requests. `--dry-run` withholds the
  first and never reaches the others.
- **`agent logs --foreground`** takes a `logs_db` grant and nothing else — see above.
- **`agent reindex`** takes a `logs_db` grant, unconditionally: filling the index is the
  whole verb, and the grant wraps the attempt rather than the outcome. `--prune` takes a
  second one for the reclaim that follows, rather than widening the first: a
  `@contextmanager` instance is single-use, and the two phases are separately reportable.
- **`agent stats` and `agent inspect`** deliberately get none, on either database: a
  command that only reports does not receive write permission in order to migrate data.
  `agent stats` in particular has no fallback either — it reads the index and nothing
  else, and reports how far behind the index is rather than filling it. `agent inspect`
  reads the index's footprint the same way, and both still *open* `logs.db`, which
  converges its schema — see the third property below.

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

## The blob sweep

`logs.db` is a content-addressed store, so nothing in it is deleted by the thing that
stops needing it. Deleting a session cascades its requests and leaves every blob they
referenced; re-indexing a session whose log file was replaced does the same, on purpose
— the content is addressed by its own hash, so the re-ingest re-uses whatever is still
there rather than writing it again. `LogIngestRepository.sweep_blobs()` is what
eventually reclaims the rest, and it is reached only through
`LogsService.reclaim_index()` — from `agent cleanup` and from `agent reindex --prune`.

It computes reachability rather than maintaining a count. A refcount column would have
to be adjusted on every insert and every cascade, and `message_refs` is a packed vector
with no foreign key, so one missed decrement is either a leak forever or content deleted
while a request still points at it. Asking the question from scratch cannot drift.

Three kinds of reference reach a blob, and a sweep that missed any one of them would
delete live content:

1. **A request names it by id** — the `message_refs` vector, or the `system_blob`,
   `tools_blob` and `response_blob` columns.
2. **Another blob's text names it by address** — a `hash:<sha256>` pointer, which the
   sidecar's string hasher writes in place of any string of 70 characters or more. An
   interned string is *never* named by a request; a pointer inside a message's canonical
   JSON is the only thing that reaches it. This one is also transitive: an interned
   original is arbitrary text and may quote a pointer of its own.
3. **The `error` column names it by address** — the same pointer, stored verbatim in a
   `TEXT` column rather than as a blob reference, because a failure's message is either
   short or already a pointer.

Kinds 2 and 3 are invisible to SQL: the payloads are zlib and `LIKE` cannot see into
them. So the closure costs a decompression pass over everything a request still points
at, which is why this is a user-invoked reclaim and never a background job.

Two consequences worth knowing. The sweep **requires the ingest lock** — it decides in
one transaction and deletes in another, and a writer landing between the two could
commit a request referencing a blob it had just judged unreachable, since
content-addressed dedup lets a chunk reference a blob it does not carry. And it ends
with `PRAGMA incremental_vacuum`, which is the whole reason `auto_vacuum = INCREMENTAL`
is set on creation: without it the pages become a free list and the file never shrinks.
That pragma is a *query*, and running it without draining its result frees exactly one
page and reports no error.

## Retention

`AGENT_LOGS_RETENTION_DAYS` deletes old sessions, and it is off by default. The split
between the layers is the interesting part: `expired_sessions(cutoff_us)` answers a
question this layer can answer — which sessions carry a `last_event_at_us` older than an
instant, and what watermark each one holds — while every other part of the decision is
the domain's, because it is about files. The storage layer never learns that the log
tree exists.

Three properties are worth stating because each of them is load-bearing:

1. **Age comes from `last_event_at_us`, never `last_ingested_at`.** The second is when
   *this host* read the session, which a backfill stamps with today across the whole
   tree; dating by it would make retention unable to expire anything until the age had
   elapsed again since the backfill.
2. **The log files go with the rows.** Ingest re-reads any directory the index has
   forgotten from byte zero, so a retention that dropped rows alone would be undone by
   the very next pass and pay for the re-ingest as well. This is the one thing in the
   codebase that deletes a session's files, and it is why the default is off.
3. **Nothing is deleted that the index has not read to the end.** `messages_offset`
   comes back with the key so the domain can compare it against the live `st_size` —
   a `stat()`, not a read, so the rule that only the ingester opens a log file holds.
   The comparison is made again at the moment of deletion rather than trusted from the
   survey.

Retention runs immediately before the sweep, under the same lock, because it is what
makes the content unreachable that the sweep then reclaims. `delete_sessions` is keyed
on the session triple rather than on `project_hash`, which is the whole difference from
`delete_projects`: a project loses old sessions while staying very much alive.

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
   over it. Pass `database_pragmas` / `connection_pragmas` only if the shared defaults
   are wrong for it — and if you pass the persistent set, read the order warning above.
3. Inject the repository into the domain service that needs it, in `Services`.

No test fixture change is needed: the autouse `_isolate_databases` fixture evicts every
`cached_property` on `core` and `repositories` by name, so a new database is redirected
at `tmp_path` the moment it exists.

## Adding a migration

Drop the next `NNNN_<slug>.sql` into that database's `migrations/`. Versions must run
contiguously from 1 — a gap or a duplicate is refused up front rather than half-applied,
because either would leave `user_version` describing a schema the database does not
have. Never edit a script that has shipped: it has already run on other people's
machines, and only the next number can change what it did.

-- Registered project directories, one row per path.
--
-- `path` is stored as the user sees it ($PWD-preserving) and is the primary key, so
-- dedup is the schema's job rather than the caller's. Ordering by it reproduces the
-- sorted, deduplicated order the previous projects.txt encoding produced, which the
-- logs viewer relies on for stable group ids.
--
-- Timestamps are unix nanoseconds so MAX(last_seen_at) can serve as a change counter.
CREATE TABLE projects (
    path          TEXT    NOT NULL PRIMARY KEY,
    first_seen_at INTEGER NOT NULL,
    last_seen_at  INTEGER NOT NULL
) STRICT;

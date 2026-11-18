-- State database, schema version 1. Statuses mirror archive_mirror.domain.status.

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    mode          TEXT NOT NULL CHECK (mode IN ('dry_run', 'mirror')),
    outcome       TEXT NOT NULL CHECK (outcome IN ('running', 'completed', 'failed', 'interrupted')),
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    start_urls    TEXT NOT NULL,  -- JSON array
    config        TEXT NOT NULL,  -- JSON snapshot of the settings used
    tool_version  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS listings (
    url             TEXT PRIMARY KEY,
    status          TEXT NOT NULL CHECK (status IN ('listed', 'failed', 'skipped')),
    format          TEXT CHECK (format IN ('apache', 'nginx', 'iis')),
    depth           INTEGER NOT NULL CHECK (depth >= 0),
    parent_url      TEXT,
    entries         INTEGER NOT NULL CHECK (entries >= 0),
    attempts        INTEGER NOT NULL CHECK (attempts >= 0),
    detail          TEXT,
    first_seen_run  TEXT NOT NULL REFERENCES runs (run_id),
    last_run        TEXT NOT NULL REFERENCES runs (run_id),
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    file_id         TEXT PRIMARY KEY,  -- sha256 of the canonical URL
    url             TEXT NOT NULL UNIQUE,
    relative_path   TEXT NOT NULL,
    name            TEXT NOT NULL,
    size_bytes      INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    size_exact      INTEGER NOT NULL CHECK (size_exact IN (0, 1)),
    modified        TEXT,
    is_archive      INTEGER NOT NULL CHECK (is_archive IN (0, 1)),
    status          TEXT NOT NULL CHECK (status IN ('listed', 'downloading', 'done', 'failed')),
    attempts        INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error      TEXT,
    -- provenance: which listing, in which format, first and last seen in which run
    listing_url     TEXT NOT NULL,
    listing_format  TEXT NOT NULL CHECK (listing_format IN ('apache', 'nginx', 'iis')),
    first_seen_run  TEXT NOT NULL REFERENCES runs (run_id),
    last_seen_run   TEXT NOT NULL REFERENCES runs (run_id),
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS files_by_status ON files (status);
CREATE INDEX IF NOT EXISTS files_by_last_seen_run ON files (last_seen_run);
CREATE INDEX IF NOT EXISTS listings_by_last_run ON listings (last_run);

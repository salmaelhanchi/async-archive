-- State database, schema version 3 (the latest; older files are migrated in state_store.py).
-- Statuses mirror archive_mirror.domain.status.

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
    updated_at      TEXT NOT NULL,
    -- version 2: downloads
    checksum_url        TEXT,
    checksum_algorithm  TEXT CHECK (checksum_algorithm IN ('sha256', 'sha512', 'md5')),
    -- a done file always has a digest and a size: "done" is never claimed without proof.
    -- COALESCE because a CHECK that evaluates to NULL passes.
    sha256              TEXT CHECK (
                            (sha256 IS NULL AND status != 'done')
                            OR COALESCE(length(sha256), 0) = 64),
    bytes_on_disk       INTEGER CHECK (
                            (bytes_on_disk IS NULL AND status != 'done')
                            OR COALESCE(bytes_on_disk, -1) >= 0),
    verified_by         TEXT CHECK (verified_by IN ('checksum', 'size', 'none')),
    etag                TEXT,
    done_run            TEXT REFERENCES runs (run_id),
    -- version 3: unpacking. Only an archive whose download is done has an extract status.
    extract_status      TEXT CHECK (
                            extract_status IS NULL
                            OR (extract_status IN
                                    ('pending', 'extracting', 'done', 'failed', 'unsupported')
                                AND is_archive = 1 AND status = 'done')),
    extract_error       TEXT,
    -- an extracted archive always has its counts: "done" is never claimed without them.
    extracted_files     INTEGER CHECK (
                            (extracted_files IS NULL AND extract_status IS NOT 'done')
                            OR COALESCE(extracted_files, -1) >= 0),
    extracted_bytes     INTEGER CHECK (
                            (extracted_bytes IS NULL AND extract_status IS NOT 'done')
                            OR COALESCE(extracted_bytes, -1) >= 0)
);

CREATE INDEX IF NOT EXISTS files_by_status ON files (status);
CREATE INDEX IF NOT EXISTS files_by_last_seen_run ON files (last_seen_run);
CREATE INDEX IF NOT EXISTS listings_by_last_run ON listings (last_run);
CREATE INDEX IF NOT EXISTS files_by_listing ON files (listing_url);
CREATE INDEX IF NOT EXISTS files_by_extract_status ON files (extract_status);

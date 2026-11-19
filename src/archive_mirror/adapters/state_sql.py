"""SQL text for the state store, kept apart from the Python that runs it."""

INSERT_RUN = (
    "INSERT INTO runs (run_id, mode, outcome, started_at, start_urls, config, tool_version)"
    " VALUES (?, ?, ?, ?, ?, ?, ?)"
)

# A rerun updates in place. A done file goes back to listed only if upstream changed.
UPSERT_FILE = """
INSERT INTO files (
    file_id, url, relative_path, name, size_bytes, size_exact, modified, is_archive, status,
    listing_url, listing_format, first_seen_run, last_seen_run, updated_at,
    checksum_url, checksum_algorithm
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'listed', ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (file_id) DO UPDATE SET
    status = CASE
        WHEN files.status = 'done'
             AND (files.size_bytes IS NOT excluded.size_bytes
                  OR files.modified IS NOT excluded.modified)
        THEN 'listed' ELSE files.status END,
    relative_path = excluded.relative_path,
    name = excluded.name,
    size_bytes = excluded.size_bytes,
    size_exact = excluded.size_exact,
    modified = excluded.modified,
    is_archive = excluded.is_archive,
    listing_url = excluded.listing_url,
    listing_format = excluded.listing_format,
    last_seen_run = excluded.last_seen_run,
    updated_at = excluded.updated_at,
    checksum_url = excluded.checksum_url,
    checksum_algorithm = excluded.checksum_algorithm
"""

UPSERT_LISTING = """
INSERT INTO listings (
    url, status, format, depth, parent_url, entries, attempts, detail,
    first_seen_run, last_run, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (url) DO UPDATE SET
    status = excluded.status,
    format = excluded.format,
    depth = excluded.depth,
    parent_url = excluded.parent_url,
    entries = excluded.entries,
    attempts = excluded.attempts,
    detail = excluded.detail,
    last_run = excluded.last_run,
    updated_at = excluded.updated_at
"""

SELECT_PENDING = """
SELECT file_id, url, relative_path, name, size_bytes, size_exact, modified, is_archive,
       listing_url, listing_format, checksum_url, checksum_algorithm, status
FROM files
WHERE status != 'done' AND file_id > ?
ORDER BY file_id
LIMIT ?
"""

SUMMARY_FILES = """
SELECT COUNT(*), COALESCE(SUM(size_bytes IS NULL), 0),
       COALESCE(SUM(size_bytes IS NOT NULL AND size_exact = 0), 0),
       COALESCE(SUM(size_bytes), 0), COALESCE(SUM(is_archive), 0),
       COALESCE(SUM(status = 'done'), 0)
FROM files WHERE last_seen_run = ?
"""

SUMMARY_LISTINGS = """
SELECT status, format, COUNT(*) FROM listings WHERE last_run = ? GROUP BY status, format
"""

# Upgrades of an existing state file. Version N's list turns version N-1 into N.
# schema.sql always holds the latest version for new files; a test checks they agree.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: (
        "ALTER TABLE files ADD COLUMN checksum_url TEXT",
        "ALTER TABLE files ADD COLUMN checksum_algorithm TEXT"
        " CHECK (checksum_algorithm IN ('sha256', 'sha512', 'md5'))",
        "ALTER TABLE files ADD COLUMN sha256 TEXT CHECK ("
        " (sha256 IS NULL AND status != 'done') OR COALESCE(length(sha256), 0) = 64)",
        "ALTER TABLE files ADD COLUMN bytes_on_disk INTEGER CHECK ("
        " (bytes_on_disk IS NULL AND status != 'done') OR COALESCE(bytes_on_disk, -1) >= 0)",
        "ALTER TABLE files ADD COLUMN verified_by TEXT"
        " CHECK (verified_by IN ('checksum', 'size', 'none'))",
        "ALTER TABLE files ADD COLUMN etag TEXT",
        "ALTER TABLE files ADD COLUMN done_run TEXT REFERENCES runs (run_id)",
        "CREATE INDEX IF NOT EXISTS files_by_listing ON files (listing_url)",
    ),
}

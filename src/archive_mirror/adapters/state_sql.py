"""SQL text for the state store, kept apart from the Python that runs it."""

INSERT_RUN = (
    "INSERT INTO runs (run_id, mode, outcome, started_at, start_urls, config, tool_version)"
    " VALUES (?, ?, ?, ?, ?, ?, ?)"
)

# A rerun updates in place. A done file goes back to listed only if upstream changed, and
# then its extraction starts over too (every SET expression reads the row before the update).
_UPSTREAM_CHANGED = """
        files.status = 'done'
        AND (files.size_bytes IS NOT excluded.size_bytes
             OR files.modified IS NOT excluded.modified)"""

UPSERT_FILE = f"""
INSERT INTO files (
    file_id, url, relative_path, name, size_bytes, size_exact, modified, is_archive, status,
    listing_url, listing_format, first_seen_run, last_seen_run, updated_at,
    checksum_url, checksum_algorithm
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'listed', ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (file_id) DO UPDATE SET
    status = CASE WHEN {_UPSTREAM_CHANGED} THEN 'listed' ELSE files.status END,
    extract_status = CASE WHEN {_UPSTREAM_CHANGED} THEN NULL ELSE files.extract_status END,
    extract_error = CASE WHEN {_UPSTREAM_CHANGED} THEN NULL ELSE files.extract_error END,
    extracted_files = CASE WHEN {_UPSTREAM_CHANGED} THEN NULL ELSE files.extracted_files END,
    extracted_bytes = CASE WHEN {_UPSTREAM_CHANGED} THEN NULL ELSE files.extracted_bytes END,
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

# Archives left for extraction by earlier runs. Archives downloaded by this run are handed to
# the extractor directly, so they are left out here.
SELECT_PENDING_EXTRACTIONS = """
SELECT file_id, relative_path, name, extract_status
FROM files
WHERE extract_status IN ('pending', 'failed') AND done_run IS NOT ? AND file_id > ?
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
    3: (
        "ALTER TABLE files ADD COLUMN extract_status TEXT CHECK ("
        " extract_status IS NULL OR (extract_status IN"
        " ('pending', 'extracting', 'done', 'failed', 'unsupported')"
        " AND is_archive = 1 AND status = 'done'))",
        "ALTER TABLE files ADD COLUMN extract_error TEXT",
        "ALTER TABLE files ADD COLUMN extracted_files INTEGER CHECK ("
        " (extracted_files IS NULL AND extract_status IS NOT 'done')"
        " OR COALESCE(extracted_files, -1) >= 0)",
        "ALTER TABLE files ADD COLUMN extracted_bytes INTEGER CHECK ("
        " (extracted_bytes IS NULL AND extract_status IS NOT 'done')"
        " OR COALESCE(extracted_bytes, -1) >= 0)",
        # Archives finished before extraction existed are queued for it.
        "UPDATE files SET extract_status = 'pending' WHERE is_archive = 1 AND status = 'done'",
        "CREATE INDEX IF NOT EXISTS files_by_extract_status ON files (extract_status)",
    ),
}

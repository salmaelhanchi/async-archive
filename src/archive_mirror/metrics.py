"""Prometheus metrics, served on ``/metrics`` when ``ARCHIVE_MIRROR_METRICS_PORT`` is set."""

from prometheus_client import Counter, Gauge, Histogram, start_http_server

HTTP_REQUESTS = Counter(
    "archive_mirror_http_requests_total",
    "HTTP requests by host and outcome (status code or 'error').",
    ["host", "outcome"],
)
HTTP_RETRIES = Counter("archive_mirror_http_retries_total", "Retried requests.", ["host"])
RATE_LIMIT_WAIT = Histogram(
    "archive_mirror_rate_limit_wait_seconds",
    "Time a request waited for its per-host token.",
    buckets=(0, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)
LISTINGS = Counter(
    "archive_mirror_listings_total", "Directory listings by status.", ["status", "format"]
)
FILES_RECORDED = Counter("archive_mirror_files_recorded_total", "Files written to the state DB.")
QUEUE_DEPTH = Gauge("archive_mirror_queue_depth", "Items waiting in a queue.", ["queue"])

BYTES_RECEIVED = Counter(
    "archive_mirror_download_bytes_total", "File bytes received, by host.", ["host"]
)
RESUMES = Counter(
    "archive_mirror_range_resumes_total", "Responses that continued a .part file (206).", ["host"]
)
FILES_FINISHED = Counter(
    "archive_mirror_files_finished_total",
    "Files leaving the download queue, by outcome (done, failed, skipped).",
    ["outcome"],
)


def serve(port: int) -> None:
    """Expose ``/metrics`` on a background thread."""
    start_http_server(port)

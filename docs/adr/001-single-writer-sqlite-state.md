# 001: One SQLite writer behind a bounded queue

## Status

Accepted.

## Context

Listing tasks run concurrently and each one finds a directory's worth of files. The state
has to survive a crash so a rerun can resume, and it has to be queryable (counts, sizes, what
failed and why). The tool runs on a laptop or a single small container. It needs no database server.

SQLite fits, but one connection used by many tasks at once is a trap. With aiosqlite each
statement runs on a worker thread. A `commit()` issued by one task commits whatever another
task has half written on the same connection.

## Decision

* SQLite in WAL mode via aiosqlite, one connection per run.
* Exactly one task writes. Listers put a `(listing outcome, files)` item on an
  `asyncio.Queue(maxsize=write_queue_size)`, and the writer persists each item in one
  transaction (`record_listing`).
* The queue is bounded, so when the disk is slow, listers block on `put` and stop fetching
  (backpressure). Memory stays flat no matter how large the archive is.
* Counters, logs and metrics move only after the commit returns ("durable before visible").
* Statuses are enforced twice: in `domain/status.py` and by SQL `CHECK` constraints.
* Ids come from the canonical upstream URL (`sha256`), and writes are upserts. A second run
  updates rows in place, keeps `first_seen_run` and moves `last_seen_run`.

## Consequences

* Write throughput is bounded by one connection. Measured: about 2 ms per 25-file listing on
  Windows. Parsing costs more than that, so the writer is not the bottleneck.
* A listing and its files land atomically: a failed write rolls back both (tested).
* Moving to Postgres later means another adapter with the same `CrawlStateWriter` port. The
  services do not change.

# Architecture

## Layers

```
src/archive_mirror/
  domain/      pure types and rules: URLs, sizes, backoff, byte ranges, checksum files,
               archive member paths and limits, both status state machines. No I/O.
  services/    use cases (lister, downloader, extractor), the ports they need
               (services/ports.py), and cancellation-safe status writes (services/writes.py)
  adapters/    all I/O: HTTP fetcher and range downloads, rate limiter, robots.txt, checksum
               files, listing parsers, part files on disk, archive unpacking and the process
               pool it runs in, SQLite store, extraction state and the state lock
  composition.py   wires adapters into services; the only module that knows concrete classes
  settings.py      pydantic-settings; the only reader of environment variables
  cli.py, report.py, progress.py, shutdown.py
                   Typer entry point; tables and JSON; live progress; two-step Ctrl+C
bench/         local archive server (listings, file bodies incl. real .tar.gz, ranges,
               faults), demo runner, kill-and-resume harness
```

Services take their adapters through the constructor (`Lister(fetcher=..., parse=...,
policy=..., writer=...)`, `Downloader(transfer=..., storage=..., checksums=..., state=...)`,
`Extractor(extractor=..., state=...)`). The unit tests hand them fakes, and the integration
tests use the real ones against the local server.

## Data flow of a dry run

```mermaid
flowchart LR
  S[start URLs] --> DQ[[directory queue]]
  DQ --> L1[lister 1] & L2[lister 2] & LN[lister N]
  L1 & L2 & LN -- subdirectories --> DQ
  L1 & L2 & LN -- "listing + files" --> WQ[[bounded write queue]]
  WQ --> W[single writer]
  W --> DB[(SQLite: runs, listings, files)]
  L1 & L2 & LN -.-> F[HttpFetcher]
  F -.-> HL[HostLimiter: semaphore + token bucket per host]
  F -.-> R[RobotsPolicy]
```

1. Each start URL becomes a directory URL with a trailing slash. Duplicates are dropped.
2. A lister takes a directory from the queue and checks, in order: depth limit, robots.txt,
   the listing-page budget. Only then does it fetch.
3. The page is parsed by the detected format. Subdirectories inside the start root go back
   on the directory queue. Files become `RemoteFile`s with id, local path and provenance.
4. The visit goes to the bounded write queue. The single writer stores the listing row and
   its files in one transaction, then updates counters and logs.
5. When the directory queue's `join()` returns, every visit is already in the write queue.
   Sentinels stop the listers and then the writer, and the `TaskGroup` exits.

## Data flow of a mirror run

```mermaid
flowchart LR
  S[start URLs] --> L[listers]
  L --> WQ[[bounded write queue]] --> W[single listing writer]
  W -- commit --> DB[(SQLite)]
  W -- "files not done, after commit" --> DQ[[bounded download queue]]
  DQ --> D1[worker 1] & DN[worker N]
  D1 & DN -- "claim: listed → downloading" --> DB
  D1 & DN --> T[HttpFileTransfer: .part + Range / If-Range]
  T --> V[verify: size, published checksum; hash in a thread]
  V --> R[fsync + atomic rename]
  R -- "downloading → done, with sha256 (archives: extract pending)" --> DB
  R -- "archives, after commit" --> XQ[[bounded extract queue]]
  DB -. "archives earlier runs left pending" .-> XQ
  XQ --> X1[extractor 1] & XN[extractor N]
  X1 & XN -- "claim: pending → extracting" --> DB
  X1 & XN --> PP[ProcessPoolExecutor, spawn: unpack into target.extracting, rename]
  PP -- "extracting → done, with counts" --> DB
```

1. Listing works as in a dry run. After the writer commits a listing, it puts the files that
   are not `done` on the bounded download queue. A full queue blocks the writer, which blocks
   the listers: backpressure from the disk all the way back to the crawl.
2. A worker checks the byte budget and the free-disk floor, then claims the file with a
   compare-and-set (`listed|failed → downloading`, attempts + 1).
3. `HttpFileTransfer` brings `<path>.part` up to the full length. It resumes any bytes
   already on disk with `Range` and `If-Range`; a dropped connection is retried the same way.
4. The part is hashed in a thread and checked against the listing size, the size the server
   announced, and the published checksum if there is one.
5. fsync, `os.replace` onto the final path, then `done` is committed with the digest. For
   an archive, the same commit sets its extract status to `pending`.
6. The worker puts the archive on the bounded extract queue. An extraction worker claims it
   (`pending|failed → extracting`) and awaits the unpacking in a worker process: members go
   to `<extract root>/<path>.extracting/` and are renamed onto `<extract root>/<path>/`
   when complete. Then `done` is committed with the file and byte counts.

Archives that earlier runs downloaded but did not unpack (extraction was off, failed, or the
run stopped) are read from the state file and fed to the same queue alongside the run.

`archive-mirror resume` skips step 1 and feeds the download queue from the state file instead
(`pending_files()`, keyset-paged), so no listing is fetched again.

The resume protocol and its ordering rules are in
[ADR 004](adr/004-resumable-downloads.md).

## State

| Table | Key | Status (`CHECK`) | Provenance |
|---|---|---|---|
| `runs` | `run_id` | `running` · `completed` · `failed` · `interrupted` | settings snapshot, tool version, start URLs |
| `listings` | canonical URL | `listed` · `failed` · `skipped` | depth, parent, format, attempts, reason, first/last run |
| `files` | `sha256(canonical URL)` | `listed` · `downloading` · `done` · `failed`; archives also `pending` · `extracting` · `done` · `failed` · `unsupported` | listing URL, listing format, first/last run, checksum URL, SHA-256, `verified_by`, ETag, done run, extraction error and counts |

Schema version 2 added the download columns, version 3 the extraction columns.
`PRAGMA user_version` drives migrations (`adapters/state_sql.py`). Tests check that migrated
version 1 and version 2 files end up with the same columns and indexes as a new one, and the
version 3 migration queues archives that were already downloaded. `CHECK`s make a `done` row
without a 64-character SHA-256 and a size impossible. An extract status is possible only on a
downloaded archive, and an extracted one must have its counts.

The file state machine (`domain/status.py`):

```
listed -> downloading -> done
              |  \-> failed -> downloading | listed
              \-> listed            (interrupted, rolled back)
done -> listed                      (upstream size or date changed; set by the upsert)
```

The extraction state machine, for archives whose download is `done`:

```
pending -> extracting -> done | unsupported
              |  \-> failed -> extracting (a later run)
              \-> pending                 (interrupted, rolled back)
(none)  -> pending                      (set by mark_done; reset when upstream changes)
```

`SqliteStateStore.transition()` checks the move against that table, then runs it as a
compare-and-set (`UPDATE ... WHERE status = ?`), so two workers cannot both claim a file.
Only `mark_done()` reaches `done`, and it records the proof. Every write runs inside one
`asyncio.Lock`, so the listing writer's transactions and the workers' transitions never
share an open transaction on the single connection.

On start, a run takes an OS lock on `<state db>.lock`, then marks older `running` runs as
`interrupted`, moves `downloading` files back to `listed` (their part files are resumed) and
`extracting` archives back to `pending`. After a graceful stop there is nothing to recover;
this is for hard kills.

## Concurrency

* `asyncio.TaskGroup` owns the listers and the writer, an outer one owns the download
  workers and the crawl, and the outermost owns the extraction workers, the downloads and
  the backlog feed. An unexpected error in any of them cancels the rest and surfaces
  (tested with a failing writer and a failing feed).
* Rate vs concurrency for downloads: a download holds its in-flight slot for as long as its
  body streams, so `max_in_flight` caps open connections, while the token bucket still
  caps how many requests start per second.
* CPU off the loop: listing pages of 8 KiB or more are parsed in a process pool (ADR 006);
  hashing a finished file runs in `asyncio.to_thread`. Writes of each
  network read stay on the loop: they land in the page cache in microseconds. fsync and
  rename run in a thread. Unpacking runs in a `ProcessPoolExecutor` (`spawn`), because the
  decoders hold the GIL. The pool shuts down from a thread, so the loop stays responsive.
* Expected failures (HTTP errors, timeouts, unparseable pages, a failed verification, a local
  error on one file) are data, not exceptions: they become `failed` rows with the reason and
  the attempt count, and the run continues.
* Backpressure: the write queue is bounded. A test with a slow writer and a queue of 1 shows
  listers never get more than `workers + queue + 1` listings ahead of the database. The
  download and extract queues are bounded too, so slow extraction slows downloads, which slow
  the crawl.

## Stopping

`shutdown.py` installs handlers for SIGINT, SIGTERM and (on Windows) SIGBREAK. The first
signal cancels the main task; the cancellation runs through every `TaskGroup`:

| Where the run is | What happens |
|---|---|
| fetching a listing | dropped; it was not committed, so the next run fetches it again |
| downloading a file | row rolled back to `listed`; the `.part` stays for the range resume |
| a file verified, being renamed | finished and committed `done` first (shielded) |
| unpacking an archive | row rolled back to `pending`; the worker process completes into its temporary directory |
| the run | recorded `interrupted`; the CLI exits 130 |

A second signal kills the worker processes and exits at once, which is a hard kill and safe
by ADR 004. Claims and final status writes run under `asyncio.shield` (`services/writes.py`),
because a SQLite commit can land after its caller was cancelled. Without that, a graceful
stop could leave a claimed row behind. See [ADR 005](adr/005-graceful-shutdown-and-extraction-workers.md).

## Measured limits

* Against the local server (85 listings, 2,125 files, 20 ms latency), wall time stops falling
  after 4 workers. Process CPU time equals wall time at that point (1.67 s CPU, 1.71 s wall),
  so the event loop is CPU-bound. The cost is mostly HTML parsing and URL handling, about 5 ms per
  25-entry page. More concurrency cannot help a CPU-bound loop.
* Measured in M3 (`bench/parse_cost.py`): parsing costs about 1.5 to 2 ms per KiB of nginx or
  IIS HTML and 3 to 4 ms per KiB of Apache HTML, so a 1,000-entry Apache page held the loop for
  0.8 s. Pages of 8 KiB or more are now parsed in their own process pool. In a dry run of
  nine 1,000-entry directories the worst loop stall fell from 2.9 s to 0.23 s
  ([ADR 006](adr/006-large-listings-parse-in-a-process-pool.md)).
* Under the politeness cap the network is the limit, not the CPU: from 4 workers on, a run
  sits at the cap's ceiling (README benchmark).
* Profiling also found httpcore re-importing a missing `sniffio` on every request (about 1.1 s per
  400 requests). `sniffio` is now an explicit dependency, see `pyproject.toml`.

## Observability

* structlog to stderr. `run_id` is bound in a context variable and appears on every line, also in
  tasks started by the `TaskGroup`. Set `ARCHIVE_MIRROR_LOG_JSON=true` for JSON lines.
* Live progress (`--progress`, on by default in a terminal): listing, downloads with bytes/s
  over a sliding window, and extraction. The services know nothing about it: composition
  reads their counters four times a second. While it is shown, only warnings and errors are
  logged, above the bars.
* Prometheus on `/metrics` when `ARCHIVE_MIRROR_METRICS_PORT` is set: requests by host and
  outcome, retries, rate-limit wait histogram, listings by status and format, files
  recorded, queue depths (directories, writes, downloads, extractions), file bytes received
  by host, range resumes, files finished by outcome, archives finished by outcome.

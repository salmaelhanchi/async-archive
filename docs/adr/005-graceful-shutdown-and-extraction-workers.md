# 005: Graceful shutdown, and archives unpacked in worker processes

## Status

Accepted. Builds on [ADR 004](004-resumable-downloads.md), which made a hard kill safe.

## Context

Two things arrive together in M2.

**Unpacking archives is CPU work.** Decompressing a large `.tar.xz` holds a core for
seconds. On the event loop that would stall every download stream and every listing for
that long. A thread does not help much: the decoders hold the GIL for most of the work.

**A person or a container runtime will stop the program.** ADR 004 already makes `kill -9`
safe: the next run recovers `downloading` rows and resumes `.part` files. But a hard kill
leaves cleanup to the next run, cuts transfers that were nearly done, and gives no
feedback. Ctrl+C and `docker stop` (SIGTERM, then SIGKILL after a grace period) should
end the run on purpose: stop taking new work, put every row back in a state the next run
understands, record the run as `interrupted`, exit with a known code.

## Decision

**Extraction runs in a `ProcessPoolExecutor`, behind a port.** The `Extractor` service only
sees `ArchiveExtractor.extract(path, name)`. The adapter sends the job to a pool started with
`spawn` on every OS (the parent has threads, and forking a threaded process can deadlock).
A bounded extract queue sits between the download workers and the extraction workers, so a
slow extraction slows the downloads that feed it and memory stays flat.

**Archives get their own state machine,** next to the file's: `pending → extracting →
done | failed | unsupported`, with SQL `CHECK`s (only a downloaded archive has one, and
`done` needs its file and byte counts). `mark_done` for an archive sets `pending` in the same
commit. A new upstream copy resets it. A failure is retried by a later run, like a failed
download. Archives that earlier runs left `pending` are fed in alongside the new downloads.

**Unpacking is untrusted input.** Member names that are absolute, carry a drive letter or
contain `..` are skipped, not repaired. Links and devices are skipped. Bytes and member counts
are capped per archive and counted as they are written, so a lying header cannot get past the
cap. Members go to `<target>.extracting/`, which is renamed onto `<target>` only when complete
(durable before visible). A killed attempt's leftovers are deleted by the next attempt.

**Shutdown has two steps** (`shutdown.py`):

1. The first SIGINT, SIGTERM or Ctrl+Break cancels the main task. The cancellation runs
   through every `TaskGroup`:
   * a download in flight rolls back to `listed` and keeps its `.part` for the range resume;
   * a file already verified is finished first (fsync, rename, `done`), because that step
     is short and throwing the work away would be waste;
   * an extraction in flight rolls back to `pending`. Its worker process finishes the archive
     it holds, into the temporary directory, while the queue behind it is dropped. Worker
     processes ignore SIGINT/SIGBREAK, so the console's Ctrl+C cannot break the pool;
   * the run is recorded `interrupted`, and the CLI exits 130.
2. A second signal kills the worker processes and calls `os._exit(130)`. That is a hard
   kill, which ADR 004 already makes safe.

**Status writes are shielded from cancellation** (`services/writes.py`). A write to SQLite
runs on aiosqlite's thread. If its caller is cancelled mid-write, the commit can still land
after the caller has moved on. Then a claim (`listed → downloading`, `pending → extracting`)
stays claimed and nobody rolls it back. So claims and final writes run to completion under
`asyncio.shield`. If the caller was cancelled meanwhile, it then looks at the outcome: a
claim that landed is undone, a result that landed is kept.

## Consequences

* After a graceful stop, nothing is left for the next run to recover. The integration test
  `test_interrupt_resume.py` sends the real signal to the CLI and asserts no row is
  `downloading` or `extracting`. Then it resumes and compares every file and every unpacked
  member byte for byte.
* The shielding was found by testing, not by design review. Stopping the compose demo with
  `docker compose stop` left one archive in `extracting`. `tests/unit/test_writes.py`
  reproduces the window with a fake whose write lands before it returns. With plain awaits
  in place of the helpers, those tests fail. After the fix, four SIGTERM-and-resume cycles
  in Docker at different points needed no recovery at all.
* A graceful stop waits for extractions already running in worker processes. A huge archive
  can make that take a while; a second Ctrl+C cuts it short.
* Starting a worker process costs about 1.3 s on Windows (spawn plus imports). The pool
  starts workers on first use, so a run without archives pays nothing.
* RAR is recognised and recorded as `unsupported`: no decoder that needs no external tool.
* HTML parsing stays on the event loop for now. Measured on this laptop: a 25-entry page
  takes 4-5 ms to parse for nginx and IIS and about 20 ms for Apache. In a pool of 4 an
  Apache page costs 7.4 ms of wall time, after 1.3 s of process start. That helps large
  Apache-style archives and hurts small runs, so it is a benchmark question for M3, not a
  default to change here.

# 004: Resumable downloads, and when a file counts as done

## Status

Accepted. Amends [ADR 001](001-single-writer-sqlite-state.md): download workers now write
to the state database too.

## Context

Files can be large and runs long, so the process will be stopped part way, by Ctrl+C, a
crash, a reboot or `kill -9`. The next run must pick up where the last one stopped, and it
must never report a file as mirrored when the bytes on disk are wrong. Servers differ: some
ignore `Range`, some send no `ETag`, some change a file between two runs, some compress
responses. A kill can land at any instant, including between two lines of our code.

## Decision

**Bytes go to a part file first.** A download writes `<path>.part`. Next to it,
`<path>.part.json` records the URL and the validator (`ETag`, `Last-Modified`) of the response
the bytes came from. The part is truncated *before* new metadata is written, so old bytes
are never paired with a new validator.

**Resume with a range, guarded by the validator.** Every attempt (a retry after a dropped
connection, or a later run) sends `Range: bytes=<part size>-` and `If-Range: <validator>`.
Only strong ETags are used for `If-Range`, as RFC 9110 requires; otherwise `Last-Modified`.

| Server answer | Meaning | Action |
|---|---|---|
| `206`, `Content-Range` starts at the part size | the rest of the same version | append |
| `206` starting anywhere else | inconsistent | drop the part, retry from 0 |
| `200` | range ignored, or the file changed | truncate, write from 0 |
| `416`, `bytes */<part size>` | the part is already complete | verify it |
| `416`, any other total | the part is longer than the file | drop the part, retry from 0 |

**Raw bytes, written as they arrive.** Requests send `Accept-Encoding: identity`, and the
body is read with `aiter_raw()`, so offsets mean the same thing on both sides and a `.gz` file
served with `Content-Encoding: gzip` is not decoded. Each network read is written
immediately. A first version asked httpx for 256 KiB chunks. A test that cut the connection
halfway showed that the buffered 64 KiB never reached the disk, and the retry started at byte 0.

**Verify before accepting.** The finished part is hashed in a worker thread
(`asyncio.to_thread`): SHA-256 always, plus the published algorithm if there is one. Then:

* the size must match the listing: exact sizes to the byte, rounded ones (`1.2M`) within
  their rounding, and the total the server announced (`Content-Length` / `Content-Range`);
* if the listing publishes a checksum (a `<name>.sha256` sidecar, or `SHA256SUMS` /
  `SHA512SUMS` / `MD5SUMS` in the directory, GNU or BSD lines), the digest must match.
  A shared sums file is fetched once per run.

A resumed copy that fails verification may be an old head joined to a new tail (a server
without validators whose file changed). It is discarded and downloaded once more from byte 0.
A second failure, or a failure of a download that never resumed, marks the file `failed`
with the reason.

`verified_by` records what was actually checked: `checksum`, `size`, or `none` when the
server published neither.

**Durable before visible, in this order.** fsync the part, then `os.replace` it onto the
final path (atomic; on POSIX the directory is fsynced too), and only then commit
`downloading → done` with the SHA-256 and size. SQL `CHECK`s make a `done` row without a
64-character digest and a size impossible, even for code that skips the store's methods.
A file at its final path is therefore always complete. A kill between the rename and the
commit costs one re-download of that file, never a missing or wrong one.

**Recovery needs exclusivity.** Each run holds an OS lock on `<state db>.lock` (`flock`,
or `msvcrt.locking` on Windows). The OS releases it when the process dies, so a kill
never leaves a stale lock. Holding the lock, a run marks older `running` runs as
`interrupted` and moves files left in `downloading` back to `listed`. Their part files are
resumed. A second process on the same state file exits with code 3.

**Transactions take turns.** The listing writer and N download workers share one aiosqlite
connection. ADR 001 relied on a single writer task; now every write runs inside an
`asyncio.Lock`, so one transaction is open at a time and no task's commit can include
another task's half-written batch. Writes are small (one row per transition), so the lock is
not a bottleneck.

**Budget before request.** Before a worker claims a file, it checks the run's byte budget
(`max_download_bytes`, counted by listed size) and a free-disk floor (`min_free_bytes`, also
counting downloads in flight). A file over budget is skipped and stays `listed` for a later run.

**Backpressure end to end.** The download queue is bounded. When downloads fall behind, the
listing writer blocks on it, the write queue fills, and the listers stop fetching.

## Consequences

* The kill-and-resume test (`bench/kill_resume.py`, run by the integration suite) kills
  the real CLI at random moments, one to three times per trial, then resumes. It passes
  20/20 trials byte for byte. The kills left partial files that were resumed with range
  requests.
* Integration tests cover connections cut mid-body (with and without validators, and with a
  server that ignores `Range`), a corrupted body caught by its published checksum, files
  changed upstream, idempotent reruns and `resume` without relisting.
* Not detectable: a file that changes upstream while keeping its size and date, on a server
  that sends no validator and publishes no checksum. That case is outside what HTTP can tell us.
* One process per state file. Two runs against one archive need two state files.
* A hard kill leaves `downloading` rows that the next run fixes. Rolling them back on Ctrl+C,
  so the database is tidy even without a next run, is part of graceful shutdown in M2.

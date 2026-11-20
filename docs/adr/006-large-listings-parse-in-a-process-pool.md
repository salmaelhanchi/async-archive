# 006: Large listing pages are parsed in a process pool

## Status

Accepted. Follows the open question in [ADR 005](005-graceful-shutdown-and-extraction-workers.md),
which kept parsing on the event loop until a benchmark showed where a pool pays off.

## Context

Every listing page is parsed with BeautifulSoup, which is pure Python and holds the GIL.
On the event loop, a parse blocks everything else in the process: downloads stop reading
their sockets, the SQLite writer waits, progress freezes. M0 had already shown the dry
run becoming CPU-bound past 4 listing workers. Public mirrors often have directories with
thousands of entries, so the size of a page matters more than the number of pages.

`bench/parse_cost.py` measured it (Windows laptop, median of 41 runs per cell, results in
[`docs/benchmark/parse_cost.json`](../benchmark/parse_cost.json)):

| Page | Size | On the loop | Warm pool round trip | 200 pages: loop / 4 workers |
|---|---:|---:|---:|---:|
| nginx, 25 entries | 2.7 KiB | 4.6 ms | 6.2 ms | 0.97 s / 0.32 s |
| IIS, 25 entries | 2.5 KiB | 5.5 ms | 7.0 ms | 1.05 s / 0.37 s |
| Apache, 25 entries | 5.4 KiB | 15.3 ms | 18.6 ms | 3.74 s / 1.06 s |
| nginx, 1,000 entries | 105 KiB | 152 ms | 161 ms | 33.9 s / 10.1 s |
| IIS, 1,000 entries | 94 KiB | 197 ms | 208 ms | 39.6 s / 12.6 s |
| Apache, 1,000 entries | 204 KiB | 793 ms | 769 ms | 151 s / 41 s |

Parsing costs about 1.5 to 2 ms per KiB of nginx or IIS HTML and about 3 to 4 ms per KiB of
Apache HTML. A pool round trip adds 1.5 to 3 ms (pickling the page in and the `Listing` out),
and starting 4 spawned workers took 2 to 3.5 s.

End to end, a real dry run of 9 Apache directories with 1,000 entries each (4 listing
workers, no rate cap, server in its own process), with a probe measuring how long the event
loop was stalled:

| Parsing | Wall time | Worst loop stall | p99 loop stall |
|---|---:|---:|---:|
| on the loop | 10.2 s | 2,944 ms | 928 ms |
| pages of 8 KiB or more in a pool of 4 | 10.0 s | 228 ms | 88 ms |

## Decision

* `PooledListingParser` (`adapters/listing_parser.py`) parses a page on the loop when it
  is smaller than `parse_in_pool_min_bytes` (default 8 KiB, about 20 ms of parsing at the
  worst rate measured), and in a process pool otherwise. The lister's parse port is async.
* The parse pool is its own `ProcessPoolExecutor` (`parse_workers`, default 2), separate
  from the extraction pool, so a long archive never delays a listing. Same rules as ADR 005:
  `spawn` start method, workers ignore Ctrl+C.
* With `spawn`, `ProcessPoolExecutor` starts workers on first use, so a crawl of small
  pages never pays the 2 to 3.5 s.
* A worker that dies on a page fails that listing (`UnrecognizedListing`), not the run.

## Consequences

* The event loop stays responsive while large directories are parsed: the worst stall in
  the dry run above fell from 2.9 s to 0.23 s. In a mirror run that is time downloads keep
  reading their sockets. The remaining stall is per-file work that still runs on the loop
  after parsing (building, scoping and recording 1,000 file records per page); it has not
  been profiled yet.
* Wall time hardly changes for a small crawl of big pages, because starting the pool costs
  about what the parallel parsing saves. On longer crawls the pool parses 3 to 4 times as
  many pages per second (the 200-page bursts above).
* A small page costs 1.5 to 3 ms more in a pool than on the loop, which is why the
  threshold exists. Set `parse_in_pool_min_bytes=0` to send every page to the pool (the
  integration tests do, for each format), or a very large value to keep everything on the
  loop.
* Under the default politeness cap (2 requests/s) parsing is never the bottleneck. This
  decision is about latency on the loop, not throughput.

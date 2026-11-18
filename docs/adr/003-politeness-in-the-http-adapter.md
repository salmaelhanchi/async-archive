# 003: Politeness lives in the HTTP adapter

## Status

Accepted.

## Context

The sequential original slept a fixed 0.2 to 0.5 s between requests and rotated browser
User-Agents. A concurrent client needs real limits, and the limits must hold no matter how
many workers are configured. Two different things get mixed up here:

* **Concurrency limit**: how many requests are in flight. It protects *our* sockets and memory.
* **Rate limit**: how many requests start per second per host. It protects *the server*.

## Decision

* Every request goes through `HttpFetcher.fetch`, which takes a `HostLimiter.slot(host)`:
  one global `asyncio.Semaphore(max_in_flight)` slot, then one token from that host's bucket.
* The token bucket is written as GCRA: one "theoretical arrival time" per host. `reserve()`
  books the next slot without awaiting, so tasks on one loop never share a slot. Burst size
  and rate are config.
* The semaphore is taken *before* the token, so a request starts as soon as its token is due
  and the measured rate never exceeds the configured one. The cost: with several hosts, a
  slow host's waiting requests hold slots other hosts could use. Per-host queues would fix that
  if multi-host runs need it.
* Retries happen inside `fetch`: connection errors, timeouts, 429 and 5xx gateway errors, with
  full-jitter exponential backoff. A 429 or 503 **pauses the whole host** for `Retry-After`
  (seconds or HTTP date). A `Retry-After` above `max_retry_after_s` fails fast instead.
* robots.txt follows RFC 9309: rules on 2xx, allow-all on 4xx, disallow-all on 5xx or
  unreachable. It is fetched once per origin behind a lock. `Crawl-delay` slows the host's
  bucket and can never speed it up.
* One honest User-Agent, `async-archive-mirror/<version> (+contact URL)`, with the contact URL
  set by config. The CLI warns when it is missing.
* Listing bodies are streamed and capped at `max_listing_bytes`.

## Consequences

* Services never see rate limits or retries. They only see a page or `FetchFailed`.
* The rate test against the local server measures wall time against `(requests - 1) / rate`.
  The retry tests use a fake clock, so they run in milliseconds.

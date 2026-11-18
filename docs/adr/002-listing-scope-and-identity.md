# 002: Listing scope, canonical URLs and local paths

## Status

Accepted.

## Context

A listing page holds more links than its children: sort links (`?C=N;O=D`), the parent
directory, icons that link to the same file, links to other hosts. Servers disagree on
details. nginx cuts long names in the link text, IIS writes absolute hrefs, and Apache rounds sizes.
A crawler that follows every link loops, escapes the tree, or mirrors files twice under
different spellings.

## Decision

* **Only direct children.** A link counts only if, resolved against the listing's own URL
  (after redirects), it stays on the same scheme and host, has no query string, and adds
  exactly one path segment. Everything else is dropped by the parser.
* **Stay under the start URL.** The lister also checks every child against the root it
  started from, so a redirect cannot widen the crawl.
* **One canonical form per URL** (`domain/urls.normalize_url`): lower-case scheme and host,
  no default port, unreserved escapes decoded, other escapes upper-cased, dot segments
  removed, fragment dropped. Directory URLs always end in `/`. File ids are the SHA-256 of
  this form, so `%7E` and `~` are the same file.
* **Names come from the href, not the link text**, decoded.
* **Local path is `host/decoded/url/path`**, each segment made safe for Windows and POSIX
  (reserved characters and device names replaced). `..` can never survive, which a
  property-based test checks.
* **Sizes carry an `exact` flag.** Rounded sizes (`1.2M`) are kept for the dry-run total but
  must not be used to verify a download.
* **Format detection** goes in order IIS, then Apache, then nginx. IIS and Apache have unambiguous markers. The
  nginx check is the most generic. `ARCHIVE_MIRROR_LISTING_FORMAT` can force one.

## Consequences

* Cycles are impossible by construction, and the visited set only has to handle duplicate
  start URLs and redirects.
* Two URLs that differ only by an encoded slash (`%2F`) stay distinct, as they are upstream.
* A server that lists files in a deeper directory than the page itself is not supported.
  None of the three formats does that.

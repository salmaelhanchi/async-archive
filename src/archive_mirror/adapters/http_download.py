"""Stream one file into its ``.part``, resuming with HTTP range requests.

Each attempt (the first, a retry after a dropped connection, or a later run after a kill)
looks at the part on disk and asks for the rest:

* ``Range: bytes=<size>-`` plus ``If-Range: <validator>``, so a server whose file changed
  sends the whole new file (200) instead of a tail that does not fit the old head;
* ``206`` with a ``Content-Range`` starting exactly at ``<size>``: append;
* ``200``: the server ignored the range or the file changed, so start over from byte 0;
* ``416`` with ``bytes */<size>``: the part is already complete;
* anything else that does not line up: drop the part and retry from 0.

``Accept-Encoding: identity`` and ``aiter_raw`` keep the bytes exactly as stored on the server,
so offsets mean the same thing on both sides. Decoding would also break the classic
misconfiguration of serving ``.gz`` files with ``Content-Encoding: gzip``.
"""

import httpx

from archive_mirror import metrics
from archive_mirror.adapters.http_fetcher import HttpFetcher
from archive_mirror.adapters.storage import MirrorStorage, PartWriter, Validator
from archive_mirror.domain.errors import FetchFailed
from archive_mirror.domain.models import TransferResult
from archive_mirror.domain.ranges import if_range_value, parse_content_range, range_header
from archive_mirror.domain.urls import host_of


class HttpFileTransfer:
    def __init__(self, fetcher: HttpFetcher, storage: MirrorStorage) -> None:
        self._fetcher = fetcher
        self._storage = storage

    async def download(self, url: str, relative_path: str) -> TransferResult:
        storage = self._storage
        host = host_of(url)
        offset = 0
        validator: Validator | None = None
        received = 0

        def headers() -> dict[str, str]:
            nonlocal offset, validator
            state = storage.part_state(relative_path, url)
            offset, validator = state.size, state.validator
            result = {"Accept-Encoding": "identity"}
            if offset > 0:
                result["Range"] = range_header(offset)
                if validator and (value := if_range_value(validator.etag, validator.last_modified)):
                    result["If-Range"] = value
            return result

        async def handle(response: httpx.Response, attempt: int) -> TransferResult:
            nonlocal received
            status = response.status_code
            etag = response.headers.get("etag")
            content_range = response.headers.get("content-range")
            writer: PartWriter
            if status == 200:
                fresh = Validator(etag, response.headers.get("last-modified"))
                writer = storage.start_part(relative_path, url, fresh)
                start = 0
                length = response.headers.get("content-length")
                total = int(length) if length and length.isdigit() else None
            elif status == 206:
                parsed = parse_content_range(content_range)
                if parsed is None or parsed.start != offset:
                    storage.discard_part(relative_path)
                    raise httpx.RemoteProtocolError(
                        f"Content-Range {content_range!r} does not continue at {offset}"
                    )
                writer = storage.append_part(relative_path, offset)
                start, total = offset, parsed.total
                etag = etag or (validator.etag if validator else None)
                metrics.RESUMES.labels(host=host).inc()
            elif status == 416 and offset > 0:
                parsed = parse_content_range(content_range)
                if parsed is not None and parsed.total == offset:  # already complete
                    known = validator.etag if validator else None
                    return TransferResult(offset, offset, offset, received, etag or known)
                storage.discard_part(relative_path)
                raise httpx.RemoteProtocolError(f"416 for offset {offset}, starting over")
            else:
                raise FetchFailed(url, f"HTTP {status}", attempt)

            try:
                # No chunk size: write each network read as it arrives. A sized iterator
                # buffers up to that size in memory, and a cut connection loses the buffer.
                async for chunk in response.aiter_raw():
                    writer.write(chunk)
                    received += len(chunk)
                    metrics.BYTES_RECEIVED.labels(host=host).inc(len(chunk))
            finally:
                writer.close()
            size = start + writer.written
            if total is not None and size != total:
                if size > total:
                    storage.discard_part(relative_path)
                raise httpx.RemoteProtocolError(f"got {size} of {total} bytes")
            return TransferResult(size, total, start, received, etag)

        return await self._fetcher.request(url, handle, headers=headers)

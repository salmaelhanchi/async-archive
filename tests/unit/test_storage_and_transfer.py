"""Part files on disk, and the range-resume protocol against respx."""

import hashlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
import respx

from archive_mirror.adapters.http_download import HttpFileTransfer
from archive_mirror.adapters.http_fetcher import HttpFetcher, RetryPolicy
from archive_mirror.adapters.politeness import HostLimiter
from archive_mirror.adapters.storage import MirrorStorage, Validator
from archive_mirror.domain.errors import FetchFailed
from tests.fakes import FakeClock

URL = "https://archive.example/pub/data.bin"
REL = "archive.example/pub/data.bin"
BODY = bytes(range(256)) * 40  # 10,240 bytes
ETAG = '"v1"'
DATE = "Tue, 05 Mar 2024 17:39:00 GMT"

# --- storage ----------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path: Path) -> MirrorStorage:
    return MirrorStorage(tmp_path / "mirror")


def write_part(storage: MirrorStorage, data: bytes, url: str = URL) -> None:
    writer = storage.start_part(REL, url, Validator(ETAG, DATE))
    writer.write(data)
    writer.close()


def test_part_state_knows_size_and_validator(storage: MirrorStorage) -> None:
    assert storage.part_state(REL, URL).size == 0

    write_part(storage, BODY[:100])

    state = storage.part_state(REL, URL)
    assert (state.size, state.validator) == (100, Validator(ETAG, DATE))
    assert storage.part_state(REL, URL + "?other").size == 0  # a part from another URL


def test_a_torn_metadata_file_means_start_over(storage: MirrorStorage) -> None:
    write_part(storage, BODY[:100])
    meta = storage.path(REL).with_name("data.bin.part.json")
    meta.write_text('{"url": "https://arch', encoding="utf-8")

    assert storage.part_state(REL, URL).size == 0


def test_append_continues_at_the_offset_even_after_a_torn_write(storage: MirrorStorage) -> None:
    write_part(storage, BODY[:150])
    writer = storage.append_part(REL, 100)  # bytes 100-149 are dropped and rewritten
    writer.write(BODY[100:])
    writer.close()

    assert storage.path(REL).with_name("data.bin.part").read_bytes() == BODY


async def test_finalize_renames_and_cleans_up(storage: MirrorStorage) -> None:
    write_part(storage, BODY)

    digests = await storage.digest_part(REL, frozenset({"sha256", "md5"}))
    await storage.finalize(REL)

    assert digests == {
        "sha256": hashlib.sha256(BODY).hexdigest(),
        "md5": hashlib.md5(BODY).hexdigest(),  # noqa: S324 - checking a published md5
    }
    assert storage.path(REL).read_bytes() == BODY
    assert sorted(p.name for p in storage.path(REL).parent.iterdir()) == ["data.bin"]


def test_paths_cannot_leave_the_root(storage: MirrorStorage) -> None:
    with pytest.raises(ValueError, match="escapes"):
        storage.path("../outside")


# --- transfer ---------------------------------------------------------------------------

Responder = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
async def transfer(storage: MirrorStorage) -> AsyncIterator[HttpFileTransfer]:
    clock = FakeClock()
    limiter = HostLimiter(rate=100, burst=10, max_in_flight=4, clock=clock, sleep=clock.sleep)
    retry = RetryPolicy(max_attempts=3, backoff_base=0.1, backoff_cap=1.0, max_retry_after=60)
    async with httpx.AsyncClient() as client:
        fetcher = HttpFetcher(client, limiter, retry, max_body_bytes=1000, sleep=clock.sleep)
        yield HttpFileTransfer(fetcher, storage)


def full(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=BODY, headers={"ETag": ETAG, "Last-Modified": DATE})


def ranged(request: httpx.Request) -> httpx.Response:
    start = int(request.headers["Range"].removeprefix("bytes=").rstrip("-"))
    headers = {"ETag": ETAG, "Content-Range": f"bytes {start}-{len(BODY) - 1}/{len(BODY)}"}
    return httpx.Response(206, content=BODY[start:], headers=headers)


def part_bytes(storage: MirrorStorage) -> bytes:
    return storage.path(REL).with_name("data.bin.part").read_bytes()


async def test_a_new_download_gets_the_whole_body(
    transfer: HttpFileTransfer, storage: MirrorStorage, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(URL).mock(side_effect=full)

    result = await transfer.download(URL, REL)

    assert (result.size, result.total, result.resumed_from, result.etag) == (
        len(BODY),
        len(BODY),
        0,
        ETAG,
    )
    assert part_bytes(storage) == BODY
    sent = route.calls.last.request.headers
    assert "Range" not in sent
    assert sent["Accept-Encoding"] == "identity"


async def test_a_part_is_resumed_with_range_and_if_range(
    transfer: HttpFileTransfer, storage: MirrorStorage, respx_mock: respx.MockRouter
) -> None:
    write_part(storage, BODY[:4000])
    route = respx_mock.get(URL).mock(side_effect=ranged)

    result = await transfer.download(URL, REL)

    sent = route.calls.last.request.headers
    assert (sent["Range"], sent["If-Range"]) == ("bytes=4000-", ETAG)
    assert (result.resumed_from, result.received) == (4000, len(BODY) - 4000)
    assert part_bytes(storage) == BODY


async def test_a_200_answer_to_a_range_request_starts_over(
    transfer: HttpFileTransfer, storage: MirrorStorage, respx_mock: respx.MockRouter
) -> None:
    write_part(storage, b"old version head")
    respx_mock.get(URL).mock(side_effect=full)  # file changed, or server ignores Range

    result = await transfer.download(URL, REL)

    assert result.resumed_from == 0
    assert part_bytes(storage) == BODY


async def test_a_range_that_does_not_line_up_is_dropped_and_retried_from_zero(
    transfer: HttpFileTransfer, storage: MirrorStorage, respx_mock: respx.MockRouter
) -> None:
    write_part(storage, BODY[:4000])
    wrong = httpx.Response(
        206, content=BODY[10:], headers={"Content-Range": "bytes 10-10239/10240"}
    )
    route = respx_mock.get(URL).mock(side_effect=[wrong, full(httpx.Request("GET", URL))])

    result = await transfer.download(URL, REL)

    assert "Range" not in route.calls.last.request.headers
    assert (result.resumed_from, part_bytes(storage)) == (0, BODY)


@pytest.mark.parametrize(
    ("content_range", "complete"), [(f"bytes */{4000}", True), ("bytes */9999", False)]
)
async def test_416_means_complete_or_start_over(
    transfer: HttpFileTransfer,
    storage: MirrorStorage,
    respx_mock: respx.MockRouter,
    content_range: str,
    complete: bool,
) -> None:
    write_part(storage, BODY[:4000])
    not_satisfiable = httpx.Response(416, headers={"Content-Range": content_range})
    respx_mock.get(URL).mock(side_effect=[not_satisfiable, full(httpx.Request("GET", URL))])

    result = await transfer.download(URL, REL)

    if complete:
        assert (result.size, result.received) == (4000, 0)
    else:
        assert (result.size, part_bytes(storage)) == (len(BODY), BODY)


async def test_a_body_cut_short_is_resumed_on_the_next_attempt(
    transfer: HttpFileTransfer, storage: MirrorStorage, respx_mock: respx.MockRouter
) -> None:
    async def cut_stream() -> AsyncIterator[bytes]:
        yield BODY[:3000]
        raise httpx.ReadError("connection reset")

    cut = httpx.Response(200, stream=cut_stream(), headers={"ETag": ETAG})  # type: ignore[arg-type]
    route = respx_mock.get(URL).mock(side_effect=[cut, ranged])

    result = await transfer.download(URL, REL)

    assert route.calls.last.request.headers["Range"] == "bytes=3000-"
    assert (result.resumed_from, part_bytes(storage)) == (3000, BODY)


async def test_a_missing_file_fails_without_retrying(
    transfer: HttpFileTransfer, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(URL).mock(return_value=httpx.Response(404))

    with pytest.raises(FetchFailed, match="HTTP 404"):
        await transfer.download(URL, REL)
    assert route.call_count == 1

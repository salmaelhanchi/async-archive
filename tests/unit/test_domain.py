"""Pure rules: URLs, sizes, backoff and the status state machine."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from archive_mirror.adapters.storage import safe_join
from archive_mirror.domain.backoff import backoff_delay, parse_retry_after
from archive_mirror.domain.errors import InvalidTransition
from archive_mirror.domain.models import is_archive
from archive_mirror.domain.sizes import parse_size
from archive_mirror.domain.status import FileStatus, check_transition
from archive_mirror.domain.urls import (
    as_directory_url,
    file_id,
    is_within,
    local_relative_path,
    normalize_url,
)

# --- URLs -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("HTTP://Mirror.Example.ORG:80/pub/", "http://mirror.example.org/pub/"),
        ("https://h.example:443/a", "https://h.example/a"),
        ("https://h.example:8443/a", "https://h.example:8443/a"),
        ("https://h.example", "https://h.example/"),
        ("https://h.example/%7Euser/%2f", "https://h.example/~user/%2F"),
        ("https://h.example/a b/é", "https://h.example/a%20b/%C3%A9"),
        ("https://h.example/a/./b/../c/", "https://h.example/a/c/"),
        ("https://h.example/a/%2E%2E/b", "https://h.example/b"),
        ("https://h.example/100%.txt", "https://h.example/100%25.txt"),
        ("https://h.example/a#frag", "https://h.example/a"),
    ],
)
def test_normalize_url(raw: str, canonical: str) -> None:
    assert normalize_url(raw) == canonical


@pytest.mark.parametrize("bad", ["ftp://h.example/", "mailto:x@h.example", "/relative", "http://"])
def test_normalize_rejects_non_http(bad: str) -> None:
    with pytest.raises(ValueError, match="URL"):
        normalize_url(bad)


segments = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="/?#"), max_size=12
)
urls = st.builds(
    lambda host, parts, slash: f"https://{host}/" + "/".join(parts) + ("/" if slash else ""),
    st.sampled_from(["h.example", "MIRROR.example.org", "127.0.0.1:8080", "[::1]:9000"]),
    st.lists(segments, max_size=5),
    st.booleans(),
)


@given(urls)
def test_normalize_is_idempotent(url: str) -> None:
    once = normalize_url(url)
    assert normalize_url(once) == once


@given(urls)
def test_local_path_never_escapes_the_mirror_root(url: str) -> None:
    path = local_relative_path(url)

    parts = path.split("/")
    assert all(part not in ("", ".", "..") for part in parts)
    assert not any(c in path for c in '\\:*?"<>|')
    root = Path("mirror-root")
    assert safe_join(root, path).relative_to(root).as_posix() == path  # accepted unchanged


def test_local_path_decodes_names_and_keeps_the_host() -> None:
    assert local_relative_path("https://h.example:8443/pub/a%20b/x%26y.txt") == (
        "h.example_8443/pub/a b/x&y.txt"
    )
    assert local_relative_path("https://h.example/pub/..%2Fetc/CON.txt") == (
        "h.example/pub/.._etc/_CON.txt"
    )


def test_file_id_is_the_same_for_equivalent_urls() -> None:
    assert file_id("HTTPS://H.example:443/a/%7Eb") == file_id("https://h.example/a/~b")
    assert file_id("https://h.example/a") != file_id("https://h.example/b")


def test_is_within() -> None:
    assert is_within("https://h.example/pub/a/b.txt", "https://h.example/pub")
    assert not is_within("https://h.example/pub/", "https://h.example/pub/")
    assert not is_within("https://h.example/public/x", "https://h.example/pub/")
    assert not is_within("https://other.example/pub/x", "https://h.example/pub/")
    assert as_directory_url("https://h.example/pub") == "https://h.example/pub/"


@pytest.mark.parametrize(
    ("name", "expected"),
    [("a.tar.gz", True), ("A.ZIP", True), ("x.7z", True), ("notes.txt", False), ("tar", False)],
)
def test_is_archive(name: str, expected: bool) -> None:
    assert is_archive(name) is expected


# --- sizes -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "size", "exact"),
    [
        ("512", 512, True),
        ("1,258,291", 1_258_291, True),
        (" 14K", 14_336, False),
        ("1.2M", 1_258_291, False),
        ("3.4G", 3_650_722_202, False),
        ("2T", 2 * 1024**4, False),
        ("1.5 KiB", 1536, False),
        ("-", None, False),
        ("<dir>", None, False),
        ("", None, False),
    ],
)
def test_parse_size(text: str, size: int | None, exact: bool) -> None:
    parsed = parse_size(text)
    assert (parsed.size_bytes, parsed.exact) == (size, exact)


# --- backoff -----------------------------------------------------------------------------


@given(
    attempt=st.integers(min_value=0, max_value=10_000),
    base=st.floats(min_value=0.01, max_value=10),
    cap=st.floats(min_value=0.01, max_value=600),
    jitter=st.floats(min_value=0, max_value=1, exclude_max=True),
)
def test_backoff_stays_between_zero_and_cap(
    attempt: int, base: float, cap: float, jitter: float
) -> None:
    assert 0 <= backoff_delay(attempt, base=base, cap=cap, jitter=jitter) <= cap


def test_backoff_doubles_until_the_cap() -> None:
    delays = [backoff_delay(a, base=1, cap=10, jitter=0.999999) for a in range(6)]
    assert [round(d) for d in delays] == [1, 2, 4, 8, 10, 10]


NOW = datetime(2024, 3, 5, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("120", 120.0),
        ("Tue, 05 Mar 2024 12:00:30 GMT", 30.0),
        ("Tue, 05 Mar 2024 11:00:00 GMT", 0.0),
        ("soon", None),
        (None, None),
    ],
)
def test_parse_retry_after(header: str | None, seconds: float | None) -> None:
    assert parse_retry_after(header, NOW) == seconds


# --- state machine -----------------------------------------------------------------------

ALLOWED = {
    (FileStatus.LISTED, FileStatus.DOWNLOADING),
    (FileStatus.DOWNLOADING, FileStatus.DONE),
    (FileStatus.DOWNLOADING, FileStatus.FAILED),
    (FileStatus.DOWNLOADING, FileStatus.LISTED),
    (FileStatus.FAILED, FileStatus.DOWNLOADING),
    (FileStatus.FAILED, FileStatus.LISTED),
    (FileStatus.DONE, FileStatus.LISTED),
}


@pytest.mark.parametrize("current", list(FileStatus))
@pytest.mark.parametrize("target", list(FileStatus))
def test_transitions(current: FileStatus, target: FileStatus) -> None:
    if (current, target) in ALLOWED:
        check_transition(current, target)
    else:
        with pytest.raises(InvalidTransition):
            check_transition(current, target)

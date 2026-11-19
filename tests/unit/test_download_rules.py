"""Pure download rules: byte ranges, checksum files and size tolerance."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from archive_mirror.domain.checksums import checksum_refs, find_digest, make_ref
from archive_mirror.domain.models import ChecksumRef, EntryKind, ListingEntry
from archive_mirror.domain.ranges import (
    ContentRange,
    if_range_value,
    parse_content_range,
    range_header,
)
from archive_mirror.domain.sizes import parse_size, size_matches

DIR = "https://archive.example/pub/"
SHA = "a" * 64
MD5 = "b" * 32

# --- ranges -----------------------------------------------------------------------------


def test_range_header_asks_for_the_rest() -> None:
    assert range_header(1000) == "bytes=1000-"
    with pytest.raises(ValueError, match="positive"):
        range_header(0)


@pytest.mark.parametrize(
    ("value", "parsed"),
    [
        ("bytes 100-199/1000", ContentRange(100, 199, 1000)),
        ("bytes 0-0/*", ContentRange(0, 0, None)),
        ("bytes */1000", ContentRange(None, None, 1000)),
        ("BYTES 5-9/10", ContentRange(5, 9, 10)),
        ("bytes 9-5/10", None),  # end before start
        ("bytes 5-10/10", None),  # end past the total
        ("bytes 5-/10", None),
        ("items 0-1/2", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_content_range(value: str | None, parsed: ContentRange | None) -> None:
    assert parse_content_range(value) == parsed


@given(start=st.integers(0, 10**12), length=st.integers(1, 10**9), extra=st.integers(0, 10**6))
def test_content_range_round_trip(start: int, length: int, extra: int) -> None:
    end, total = start + length - 1, start + length + extra
    assert parse_content_range(f"bytes {start}-{end}/{total}") == ContentRange(start, end, total)


def test_if_range_prefers_a_strong_etag_and_never_sends_a_weak_one() -> None:
    date = "Tue, 05 Mar 2024 17:39:00 GMT"
    assert if_range_value('"abc"', date) == '"abc"'
    assert if_range_value('W/"abc"', date) == date
    assert if_range_value('W/"abc"', None) is None
    assert if_range_value(None, None) is None


# --- checksum files ---------------------------------------------------------------------


def entries(*names: str) -> list[ListingEntry]:
    return [ListingEntry(DIR + n, n, EntryKind.FILE, 1, True, None) for n in names]


def test_a_sidecar_wins_over_a_sums_file() -> None:
    refs = checksum_refs(entries("a.iso", "a.iso.sha256", "b.iso", "SHA256SUMS", "MD5SUMS"))

    assert refs[DIR + "a.iso"] == ChecksumRef(DIR + "a.iso.sha256", "sha256", shared=False)
    assert refs[DIR + "b.iso"] == ChecksumRef(DIR + "SHA256SUMS", "sha256", shared=True)
    assert DIR + "SHA256SUMS" not in refs  # checksum files are not themselves covered
    assert DIR + "a.iso.sha256" not in refs


def test_sums_files_are_preferred_by_strength_and_files_without_any_are_left_out() -> None:
    assert checksum_refs(entries("x", "MD5SUMS", "sha512sums.txt"))[DIR + "x"].algorithm == "sha512"
    assert checksum_refs(entries("x", "x.md5"))[DIR + "x"].algorithm == "md5"
    assert checksum_refs(entries("x", "y")) == {}


def test_make_ref_restores_the_shared_flag_from_the_name() -> None:
    assert make_ref(DIR + "SHA256SUMS", "sha256").shared
    assert not make_ref(DIR + "x.sha256", "sha256").shared


@pytest.mark.parametrize(
    "text",
    [
        f"{SHA}  x.tar.gz\n",  # GNU text mode
        f"{SHA} *x.tar.gz\n",  # GNU binary mode
        f"{'c' * 64}  other\n{SHA}  ./x.tar.gz\n",  # sums file, ./ prefix
        f"# made by a script\nSHA256 (x.tar.gz) = {SHA}\n",  # BSD
        f"{SHA.upper()}\n",  # a sidecar with only the digest
    ],
)
def test_find_digest_reads_common_formats(text: str) -> None:
    assert find_digest(text, "x.tar.gz", "sha256") == SHA


def test_find_digest_does_not_guess() -> None:
    assert find_digest(f"{SHA}  y.tar.gz\n", "x.tar.gz", "sha256") is None
    assert find_digest(f"{MD5}  x.tar.gz\n", "x.tar.gz", "sha256") is None  # wrong length
    assert find_digest(f"MD5 (x.tar.gz) = {MD5}\n", "x.tar.gz", "sha256") is None
    assert find_digest(f"{SHA}  sub/x.tar.gz\n", "x.tar.gz", "sha256") is None
    assert find_digest(f"MD5 (x) = {MD5}\n", "x", "md5") == MD5


# --- sizes ------------------------------------------------------------------------------


def test_exact_sizes_must_match_to_the_byte() -> None:
    assert size_matches(1000, 1000, exact=True)
    assert not size_matches(1001, 1000, exact=True)
    assert size_matches(123, None, exact=False)  # nothing to compare with


@given(actual=st.integers(0, 10**13))
def test_a_rounded_listing_size_always_accepts_the_true_size(actual: int) -> None:
    # What Apache prints for this size, parsed back, must accept the real size.
    shown = _apache_rounded(actual)
    parsed = parse_size(shown)
    assert size_matches(actual, parsed.size_bytes, exact=parsed.exact)


def test_a_rounded_size_still_rejects_a_clearly_wrong_file() -> None:
    parsed = parse_size("45M")
    assert not size_matches(30 * 1024**2, parsed.size_bytes, exact=parsed.exact)


def _apache_rounded(size: int) -> str:
    if size < 1024:
        return str(size)
    value = float(size)
    for unit in "KMGT":
        value /= 1024
        if value < 1024 or unit == "T":
            return f"{value:.1f}{unit}" if value < 9.95 else f"{value:.0f}{unit}"
    raise AssertionError("unreachable")

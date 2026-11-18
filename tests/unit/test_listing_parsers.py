"""Parsers against hand-written fixtures in each server's default output format."""

from datetime import datetime
from pathlib import Path

import pytest
from bench.archive_server import RENDERERS, Node
from hypothesis import given
from hypothesis import strategies as st

from archive_mirror.adapters.listing import detect_format, parse_listing
from archive_mirror.domain.errors import UnrecognizedListing
from archive_mirror.domain.models import EntryKind, ListingFormat

FIXTURES = Path(__file__).parent.parent / "fixtures" / "listings"
BASE = "https://mirror.example.org/pub/project/"

DIR = (EntryKind.DIRECTORY, None, False)
# name -> (kind, size_bytes, size_exact)
APACHE_FANCY = {
    "docs": DIR,
    "releases": DIR,
    "README.txt": (EntryKind.FILE, 512, True),
    "project-1.0.tar.gz": (EntryKind.FILE, 1_258_291, False),
    "release notes.txt": (EntryKind.FILE, 14_336, False),
    "a&b.dat": (EntryKind.FILE, 3_650_722_202, False),
}
EXACT = {
    "docs": DIR,
    "releases": DIR,
    "README.txt": (EntryKind.FILE, 512, True),
    "project-1.0.tar.gz": (EntryKind.FILE, 1_258_291, True),
    "release notes.txt": (EntryKind.FILE, 14_336, True),
    "a&b.dat": (EntryKind.FILE, 3_650_722_201, True),
}
NGINX = EXACT | {
    "project-1.0-with-a-very-long-file-name-for-testing.tar.gz": (EntryKind.FILE, 1_258_300, True)
}
PLAIN = {name: (kind, None, False) for name, (kind, _, _) in APACHE_FANCY.items()}

CASES = [
    ("apache_table.html", ListingFormat.APACHE, APACHE_FANCY),
    ("apache_pre.html", ListingFormat.APACHE, APACHE_FANCY),
    ("apache_plain.html", ListingFormat.APACHE, PLAIN),
    ("nginx.html", ListingFormat.NGINX, NGINX),
    ("iis.html", ListingFormat.IIS, EXACT),
]


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(("fixture", "fmt", "expected"), CASES)
def test_parser_reads_every_entry(fixture: str, fmt: ListingFormat, expected: dict) -> None:
    listing = parse_listing(load(fixture), BASE)

    assert listing.format is fmt
    assert {e.name: (e.kind, e.size_bytes, e.size_exact) for e in listing.entries} == expected


@pytest.mark.parametrize(("fixture", "fmt", "expected"), CASES)
def test_format_is_detected(fixture: str, fmt: ListingFormat, expected: dict) -> None:
    assert detect_format(load(fixture)) is fmt


@pytest.mark.parametrize(("fixture", "fmt", "expected"), CASES)
def test_urls_are_absolute_children_of_the_listing(
    fixture: str, fmt: ListingFormat, expected: dict
) -> None:
    listing = parse_listing(load(fixture), BASE)

    by_name = {e.name: e for e in listing.entries}
    assert by_name["docs"].url == BASE + "docs/"
    assert by_name["release notes.txt"].url == BASE + "release%20notes.txt"
    assert by_name["a&b.dat"].url == BASE + "a%26b.dat"


@pytest.mark.parametrize(
    ("fixture", "modified"),
    [
        ("apache_table.html", datetime(2024, 3, 5, 17, 39)),
        ("apache_pre.html", datetime(2024, 3, 5, 17, 39)),
        ("nginx.html", datetime(2024, 3, 5, 17, 39)),
        ("iis.html", datetime(2024, 3, 5, 17, 39)),
    ],
)
def test_modified_time_is_parsed(fixture: str, modified: datetime) -> None:
    listing = parse_listing(load(fixture), BASE)

    assert {e.name: e.modified for e in listing.entries}["project-1.0.tar.gz"] == modified


def test_iis_root_listing_with_long_dates_and_no_parent_link() -> None:
    html = load("iis_root_long_dates.html")

    listing = parse_listing(html, "https://mirror.example.org/")

    assert detect_format(html) is ListingFormat.IIS
    assert [(e.name, e.size_bytes, e.modified) for e in listing.entries] == [
        ("index.txt", 4096, datetime(2024, 3, 2, 9, 15)),
        ("snapshot.zip", 880_640, datetime(2024, 3, 5, 17, 39)),
    ]


def test_listing_url_without_trailing_slash_is_treated_as_a_directory() -> None:
    listing = parse_listing(load("nginx.html"), BASE.rstrip("/"))

    assert listing.url == BASE
    assert {e.url for e in listing.directories} == {BASE + "docs/", BASE + "releases/"}


def test_links_outside_the_directory_are_ignored() -> None:
    listing = parse_listing(load("apache_table.html"), BASE)

    assert all(e.url.startswith(BASE) for e in listing.entries)
    assert "mirror elsewhere" not in {e.name for e in listing.entries}


def test_forced_format_skips_detection() -> None:
    listing = parse_listing(load("nginx.html"), BASE, forced=ListingFormat.NGINX)

    assert listing.format is ListingFormat.NGINX


def test_page_that_is_not_a_listing_is_rejected() -> None:
    with pytest.raises(UnrecognizedListing):
        parse_listing("<html><body><p>Welcome</p><a href='x'>x</a></body></html>", BASE)


def test_icon_links_do_not_create_duplicates() -> None:
    html = load("apache_pre.html").replace(
        '<img src="/icons/text.gif" alt="[TXT]"> <a href="README.txt">',
        '<a href="README.txt"><img src="/icons/text.gif" alt="[TXT]"></a> <a href="README.txt">',
    )

    listing = parse_listing(html, BASE)

    readmes = [e for e in listing.entries if e.name == "README.txt"]
    assert len(readmes) == 1
    assert readmes[0].size_bytes == 512


# Round trip: any tree rendered in any format parses back to the same names and kinds.
names = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs", "Cc", "Zl", "Zp"), blacklist_characters="/\\"
    ),
    min_size=1,
    max_size=30,
).filter(lambda n: n.strip(" ") and n not in {".", ".."})
nodes = st.lists(
    st.builds(
        Node,
        name=names,
        is_dir=st.booleans(),
        size=st.integers(min_value=0, max_value=10**12),
        modified=st.datetimes(min_value=datetime(1990, 1, 1), max_value=datetime(2090, 1, 1)),
    ),
    max_size=12,
    unique_by=lambda n: n.name,
)


@given(style=st.sampled_from(sorted(RENDERERS)), children=nodes)
def test_rendered_listing_round_trips(style: str, children: list[Node]) -> None:
    html = RENDERERS[style]("/pub/", children)

    listing = parse_listing(html, "http://127.0.0.1:8080/pub/", forced=ListingFormat(style))

    got = {e.name: e.kind for e in listing.entries}
    want = {n.name: EntryKind.DIRECTORY if n.is_dir else EntryKind.FILE for n in children}
    assert got == want
    if style != "apache":  # Apache rounds sizes; nginx and IIS print exact bytes
        sizes = {e.name: e.size_bytes for e in listing.files}
        assert sizes == {n.name: n.size for n in children if not n.is_dir}

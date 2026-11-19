"""Checksum files published next to the data: discovery in a listing, and parsing.

Two conventions are common on public mirrors:

* a sidecar per file: ``x.tar.gz.sha256`` holding ``<hex>  x.tar.gz`` or just ``<hex>``
* one file per directory: ``SHA256SUMS`` with a ``<hex>  <name>`` line per file

Both GNU (``<hex>  name``, ``<hex> *name``) and BSD (``SHA256 (name) = <hex>``) lines are read.
"""

import re
from collections.abc import Iterable

from archive_mirror.domain.models import ChecksumRef, ListingEntry

DIGEST_LENGTH = {"sha256": 64, "sha512": 128, "md5": 32}

# Preference order when a file has more than one sidecar: sha256 is hashed anyway.
_SIDECAR_SUFFIXES = (
    (".sha256", "sha256"),
    (".sha256sum", "sha256"),
    (".sha512", "sha512"),
    (".md5", "md5"),
)
_SUMS_FILES = {
    "sha256sums": "sha256",
    "sha256sums.txt": "sha256",
    "sha512sums": "sha512",
    "sha512sums.txt": "sha512",
    "md5sums": "md5",
    "md5sums.txt": "md5",
}
_SUMS_PREFERENCE = ("sha256", "sha512", "md5")

_GNU_LINE = re.compile(r"^\\?(?P<hex>[0-9a-fA-F]+) [ *](?P<name>.+)$")
_BSD_LINE = re.compile(r"^(?P<algo>[A-Za-z0-9-]+) \((?P<name>.+)\) = (?P<hex>[0-9a-fA-F]+)$")


def is_checksum_file(name: str) -> bool:
    lower = name.lower()
    return lower in _SUMS_FILES or lower.endswith(tuple(s for s, _ in _SIDECAR_SUFFIXES))


def make_ref(url: str, algorithm: str) -> ChecksumRef:
    """Rebuild a reference from its stored URL and algorithm."""
    name = url.rstrip("/").rsplit("/", 1)[-1].lower()
    return ChecksumRef(url, algorithm, shared=name in _SUMS_FILES)


def checksum_refs(files: Iterable[ListingEntry]) -> dict[str, ChecksumRef]:
    """Map each file URL in one listing to the checksum file that covers it, if any.

    A per-file sidecar wins over a directory-wide sums file. Checksum files themselves are
    not covered.
    """
    entries = list(files)
    by_name = {e.name: e for e in entries}
    sums = sorted(
        (
            (_SUMS_PREFERENCE.index(algo), e.url, algo)
            for e in entries
            if (algo := _SUMS_FILES.get(e.name.lower()))
        ),
    )
    refs: dict[str, ChecksumRef] = {}
    for entry in entries:
        if is_checksum_file(entry.name):
            continue
        for suffix, algo in _SIDECAR_SUFFIXES:
            sidecar = by_name.get(entry.name + suffix)
            if sidecar is not None:
                refs[entry.url] = ChecksumRef(sidecar.url, algo, shared=False)
                break
        else:
            if sums:
                _, url, algo = sums[0]
                refs[entry.url] = ChecksumRef(url, algo, shared=True)
    return refs


def find_digest(text: str, file_name: str, algorithm: str) -> str | None:
    """The lower-case hex digest of ``file_name`` in a checksum file, or ``None``."""
    length = DIGEST_LENGTH[algorithm]
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not line.startswith("#")]
    for line in lines:
        bsd = _BSD_LINE.match(line)
        if bsd and bsd["algo"].replace("-", "").lower() == algorithm:
            if bsd["name"] == file_name and len(bsd["hex"]) == length:
                return bsd["hex"].lower()
            continue
        gnu = _GNU_LINE.match(line)
        if gnu and len(gnu["hex"]) == length and gnu["name"].removeprefix("./") == file_name:
            return gnu["hex"].lower()
    if len(lines) == 1 and len(lines[0]) == length and _is_hex(lines[0]):
        return lines[0].lower()  # a sidecar holding only the digest
    return None


def _is_hex(text: str) -> bool:
    return all(c in "0123456789abcdefABCDEF" for c in text)

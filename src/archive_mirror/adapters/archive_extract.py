"""Unpack one archive. Runs inside a worker process (see ``worker_pool.py``), so this is plain
blocking code: no asyncio, picklable arguments and results, and light imports so a fresh
worker starts fast.

Durable before visible: members go into ``<target>.extracting/``, which is renamed onto
``<target>`` only when every member is written. A kill leaves the temporary directory, and
the next attempt deletes it first. What reaches ``<target>`` is always a whole archive.

Untrusted input: names are checked by ``safe_member_path`` before anything is written,
links and devices are skipped, and bytes are counted as they are written, so an archive
that lies in its headers still cannot pass ``max_bytes``.
"""

import bz2
import gzip
import lzma
import os
import shutil
import stat
import tarfile
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import IO

from archive_mirror.domain.errors import ExtractionFailed, UnsupportedArchive
from archive_mirror.domain.extraction import (
    SUPPORTED_KINDS,
    ArchiveKind,
    ExtractLimits,
    ExtractResult,
    archive_kind,
    safe_member_path,
    stream_member_name,
)

TEMP_SUFFIX = ".extracting"
OLD_SUFFIX = ".replaced"
_CHUNK = 1024 * 1024
_ZIP_ENCRYPTED = 0x1


class _Writer:
    """Writes members under ``root`` and enforces the limits as bytes arrive."""

    def __init__(self, root: Path, limits: ExtractLimits) -> None:
        self.root = root
        self.limits = limits
        self.files = 0
        self.bytes = 0
        self.skipped = 0

    def count_member(self) -> None:
        if self.files + self.skipped >= self.limits.max_members:
            raise ExtractionFailed(f"more than {self.limits.max_members} members")

    def skip(self) -> None:
        self.skipped += 1

    def write(self, relative: str, source: IO[bytes]) -> None:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as out:
            while chunk := source.read(_CHUNK):
                self.bytes += len(chunk)
                if self.bytes > self.limits.max_bytes:
                    raise ExtractionFailed(f"unpacks to more than {self.limits.max_bytes} bytes")
                out.write(chunk)
        self.files += 1

    def result(self) -> ExtractResult:
        return ExtractResult(files=self.files, bytes=self.bytes, skipped=self.skipped)


def _zip(source: Path, out: _Writer, _name: str) -> None:
    with zipfile.ZipFile(source) as archive:
        for info in archive.infolist():
            out.count_member()
            if info.is_dir():
                continue
            if info.flag_bits & _ZIP_ENCRYPTED:
                raise UnsupportedArchive("encrypted zip")
            relative = safe_member_path(info.filename)
            if relative is None or stat.S_ISLNK(info.external_attr >> 16):
                out.skip()
                continue
            with archive.open(info) as member:
                out.write(relative, member)


def _tar(source: Path, out: _Writer, _name: str) -> None:
    with tarfile.open(source, "r:*") as archive:
        for member in archive:  # streams the index: a huge member list is never held at once
            out.count_member()
            if member.isdir():
                continue
            relative = safe_member_path(member.name)
            data = archive.extractfile(member) if member.isfile() else None
            if relative is None or data is None:  # links, devices, fifos
                out.skip()
                continue
            with data:
                out.write(relative, data)


_STREAM_MODULES = {".gz": gzip, ".bz2": bz2, ".xz": lzma}


def _stream(source: Path, out: _Writer, name: str) -> None:
    suffix = "." + name.lower().rsplit(".", 1)[-1]
    out.count_member()
    with _STREAM_MODULES[suffix].open(source, "rb") as data:
        out.write(stream_member_name(name), data)


def _seven_zip(source: Path, out: _Writer, _name: str) -> None:
    import py7zr

    try:
        with py7zr.SevenZipFile(source, "r") as archive:
            if archive.needs_password():
                raise UnsupportedArchive("encrypted 7z")
            # py7zr writes each target under its own name, so only members whose name is
            # already safe are extracted; the rest are skipped, not renamed.
            targets: list[str] = []
            declared = 0
            for info in archive.list():
                out.count_member()
                if info.is_directory:
                    continue
                relative = safe_member_path(info.filename)
                if not info.is_file or info.is_symlink or relative != info.filename:
                    out.skip()
                    continue
                declared += info.uncompressed
                if declared > out.limits.max_bytes:
                    raise ExtractionFailed(f"declares more than {out.limits.max_bytes} bytes")
                targets.append(info.filename)
            archive.reset()
            archive.extract(path=out.root, targets=targets)
    except (py7zr.exceptions.ArchiveError, py7zr.exceptions.PasswordRequired) as exc:
        raise ExtractionFailed(f"{type(exc).__name__}: {exc}") from None
    for path in out.root.rglob("*"):  # count what is really on disk
        if path.is_file():
            out.files += 1
            out.bytes += path.stat().st_size
    if out.bytes > out.limits.max_bytes:
        raise ExtractionFailed(f"unpacks to more than {out.limits.max_bytes} bytes")


_EXTRACTORS: dict[ArchiveKind, Callable[[Path, _Writer, str], None]] = {
    ArchiveKind.ZIP: _zip,
    ArchiveKind.TAR: _tar,
    ArchiveKind.STREAM: _stream,
    ArchiveKind.SEVEN_ZIP: _seven_zip,
}

# What corrupt input raises from the standard library decoders.
_CORRUPT = (
    zipfile.BadZipFile,
    tarfile.TarError,
    gzip.BadGzipFile,
    zlib.error,
    lzma.LZMAError,
    EOFError,
    ValueError,  # bz2 and zipfile raise it for some malformed streams
)


def extract_archive(source: str, target: str, name: str, limits: ExtractLimits) -> ExtractResult:
    """Unpack ``source`` (an archive called ``name``) into the directory ``target``.

    Raises ``UnsupportedArchive`` for formats that are recognised but not unpacked, and
    ``ExtractionFailed`` for corrupt archives or ones over the limits.
    """
    kind = archive_kind(name)
    if kind not in SUPPORTED_KINDS:
        raise UnsupportedArchive(f"{kind or 'unknown'} archives are not extracted")
    final = Path(target)
    temp = final.with_name(final.name + TEMP_SUFFIX)
    old = final.with_name(final.name + OLD_SUFFIX)
    for leftover in (temp, old):  # from an attempt that was killed
        shutil.rmtree(leftover, ignore_errors=True)
    temp.mkdir(parents=True)
    writer = _Writer(temp, limits)
    try:
        _EXTRACTORS[kind](Path(source), writer, name)
    except ExtractionFailed:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    except _CORRUPT as exc:
        shutil.rmtree(temp, ignore_errors=True)
        raise ExtractionFailed(f"{type(exc).__name__}: {exc}") from None
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    _publish(temp, final, old)
    return writer.result()


def _publish(temp: Path, final: Path, old: Path) -> None:
    """Move the finished directory onto ``final``. A directory cannot be replaced in one
    rename on Windows, so an earlier copy is moved aside first and deleted after."""
    if final.exists():
        os.replace(final, old)
    os.replace(temp, final)
    shutil.rmtree(old, ignore_errors=True)

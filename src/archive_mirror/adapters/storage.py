"""Local mirror storage: ``.part`` files, their resume metadata, hashing and the final rename.

A file is written to ``<path>.part`` and only renamed to ``<path>`` once it is complete and
verified, so a file at its final path is always whole. Next to each part, ``<path>.part.json``
records the URL and the server's validator (``ETag`` / ``Last-Modified``) that the bytes came
from, so a resume can ask the server for "the rest of *this* version" with ``If-Range``.

Ordering rules that make a kill at any instant safe:

* starting over: truncate the part *before* writing new metadata, so old bytes are never
  paired with a new validator;
* finishing: fsync the part, then ``os.replace`` it onto the final path (atomic), and only
  then does the caller record ``done`` in the database.
"""

import asyncio
import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

PART_SUFFIX = ".part"
META_SUFFIX = ".part.json"


@dataclass(frozen=True, slots=True)
class Validator:
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True, slots=True)
class PartState:
    """What a resume can build on: bytes already on disk and where they came from."""

    size: int
    validator: Validator | None


class PartWriter:
    """Appends to a part file. Plain buffered writes: they land in the page cache in
    microseconds, so they stay on the event loop. fsync (slow) happens in a thread."""

    def __init__(self, handle: BinaryIO) -> None:
        self._handle = handle
        self.written = 0

    def write(self, chunk: bytes) -> None:
        self._handle.write(chunk)
        self.written += len(chunk)

    def close(self) -> None:
        self._handle.close()


class MirrorStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def path(self, relative_path: str) -> Path:
        target = (self.root / relative_path).resolve()
        if not target.is_relative_to(self.root):  # relative paths are made safe upstream too
            raise ValueError(f"path escapes the mirror root: {relative_path!r}")
        return target

    def _part(self, relative_path: str) -> Path:
        target = self.path(relative_path)
        return target.with_name(target.name + PART_SUFFIX)

    def _meta(self, relative_path: str) -> Path:
        target = self.path(relative_path)
        return target.with_name(target.name + META_SUFFIX)

    def part_state(self, relative_path: str, url: str) -> PartState:
        """Bytes on disk for ``url``. A part without metadata, or from another URL, is ignored."""
        part, meta = self._part(relative_path), self._meta(relative_path)
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
            size = part.stat().st_size
        except (OSError, ValueError):
            return PartState(0, None)
        if info.get("url") != url:
            return PartState(0, None)
        return PartState(size, Validator(info.get("etag"), info.get("last_modified")))

    def start_part(self, relative_path: str, url: str, validator: Validator) -> PartWriter:
        """Begin the file from byte 0 (a new download, or the server sent the whole body)."""
        part, meta = self._part(relative_path), self._meta(relative_path)
        part.parent.mkdir(parents=True, exist_ok=True)
        handle = part.open("wb")  # truncate first ...
        payload = {"url": url, "etag": validator.etag, "last_modified": validator.last_modified}
        # ... then describe what the new bytes will be. A write torn by a kill does not
        # parse, which reads as "no metadata" and restarts from 0: safe.
        try:
            meta.write_text(json.dumps(payload), encoding="utf-8")
        except BaseException:
            handle.close()
            raise
        return PartWriter(handle)

    def append_part(self, relative_path: str, expected_size: int) -> PartWriter:
        """Continue a part at ``expected_size`` (the offset the range request asked for)."""
        handle = self._part(relative_path).open("r+b")
        handle.truncate(expected_size)
        handle.seek(expected_size)
        return PartWriter(handle)

    def discard_part(self, relative_path: str) -> None:
        for path in (self._part(relative_path), self._meta(relative_path)):
            path.unlink(missing_ok=True)

    async def digest_part(self, relative_path: str, algorithms: frozenset[str]) -> dict[str, str]:
        """Hash the finished part with each algorithm in one pass, off the event loop."""
        return await asyncio.to_thread(_hash_file, self._part(relative_path), algorithms)

    async def finalize(self, relative_path: str) -> None:
        """fsync the part and atomically rename it onto the final path, off the event loop."""
        await asyncio.to_thread(self._finalize, relative_path)

    def _finalize(self, relative_path: str) -> None:
        part, target = self._part(relative_path), self.path(relative_path)
        with part.open("rb+") as handle:
            os.fsync(handle.fileno())
        _replace(part, target)
        if sys.platform != "win32":  # make the rename itself durable
            fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        self._meta(relative_path).unlink(missing_ok=True)

    def free_bytes(self) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self.root).free


def _hash_file(path: Path, algorithms: frozenset[str]) -> dict[str, str]:
    hashers = {name: hashlib.new(name) for name in algorithms}
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            for hasher in hashers.values():
                hasher.update(chunk)
    return {name: hasher.hexdigest() for name, hasher in hashers.items()}


def _replace(source: Path, target: Path, attempts: int = 6) -> None:
    """``os.replace`` with a short retry. On Windows a virus scanner or indexer can hold a
    just-written file open for a moment and the rename fails with ``PermissionError``."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.02 * 2**attempt)

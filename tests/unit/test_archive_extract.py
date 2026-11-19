"""Unpacking rules and the extraction adapter, run in-process on archives built here."""

import bz2
import gzip
import io
import lzma
import tarfile
import zipfile
from pathlib import Path

import py7zr
import pytest
from hypothesis import given
from hypothesis import strategies as st

from archive_mirror.adapters.archive_extract import OLD_SUFFIX, TEMP_SUFFIX, extract_archive
from archive_mirror.domain.errors import ExtractionFailed, UnsupportedArchive
from archive_mirror.domain.extraction import (
    ArchiveKind,
    ExtractLimits,
    archive_kind,
    safe_member_path,
    stream_member_name,
)

LIMITS = ExtractLimits(max_bytes=10_000_000, max_members=1_000)
MEMBERS = {"README.txt": b"hello\n", "nested/dir/data.bin": bytes(range(256)) * 40}


# --- rules ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("a.zip", ArchiveKind.ZIP),
        ("a.TAR.GZ", ArchiveKind.TAR),
        ("a.tgz", ArchiveKind.TAR),
        ("a.tar.xz", ArchiveKind.TAR),
        ("a.tbz2", ArchiveKind.TAR),
        ("notes.txt.gz", ArchiveKind.STREAM),
        ("a.bz2", ArchiveKind.STREAM),
        ("a.7z", ArchiveKind.SEVEN_ZIP),
        ("a.rar", ArchiveKind.RAR),
        ("a.txt", None),
    ],
)
def test_archive_kind_is_read_from_the_name(name: str, kind: ArchiveKind | None) -> None:
    assert archive_kind(name) is kind


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a/b.txt", "a/b.txt"),
        ("./a//b.txt", "a/b.txt"),
        ("a\\b.txt", "a/b.txt"),
        ("../evil", None),
        ("a/../../evil", None),
        ("/etc/passwd", None),
        ("C:/Windows/evil", None),
        ("C:evil", None),
        ("dir/", "dir"),
        ("", None),
        ("CON.txt", "_CON.txt"),
        ('bad:"name?.txt', "bad__name_.txt"),
    ],
)
def test_member_paths_are_kept_inside_the_target(name: str, expected: str | None) -> None:
    assert safe_member_path(name) == expected


@given(st.text())
def test_a_safe_member_path_is_always_relative_and_never_climbs(name: str) -> None:
    result = safe_member_path(name)
    if result is not None:
        parts = result.split("/")
        assert ".." not in parts
        assert not result.startswith("/")
        assert ":" not in result
        assert all(parts)


def test_a_single_compressed_file_keeps_its_inner_name() -> None:
    assert stream_member_name("notes.txt.gz") == "notes.txt"
    assert stream_member_name("x.gz") == "x"


# --- builders ---------------------------------------------------------------------------


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def make_tar(path: Path, members: dict[str, bytes], mode: str = "w:gz") -> Path:
    with tarfile.open(path, mode) as archive:  # type: ignore[call-overload]
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def read_tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


# --- formats ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "build"),
    [
        ("a.zip", lambda p: make_zip(p, MEMBERS)),
        ("a.tar.gz", lambda p: make_tar(p, MEMBERS, "w:gz")),
        ("a.tar.xz", lambda p: make_tar(p, MEMBERS, "w:xz")),
        ("a.tar.bz2", lambda p: make_tar(p, MEMBERS, "w:bz2")),
        ("a.tar", lambda p: make_tar(p, MEMBERS, "w")),
    ],
)
def test_each_format_unpacks_every_member(tmp_path: Path, name: str, build: object) -> None:
    source = build(tmp_path / name)  # type: ignore[operator]
    target = tmp_path / "out" / name

    result = extract_archive(str(source), str(target), name, LIMITS)

    assert read_tree(target) == MEMBERS
    assert (result.files, result.bytes, result.skipped) == (2, sum(map(len, MEMBERS.values())), 0)


def test_7z_unpacks_every_member(tmp_path: Path) -> None:
    source = tmp_path / "a.7z"
    with py7zr.SevenZipFile(source, "w") as archive:
        for name, data in MEMBERS.items():
            archive.writef(io.BytesIO(data), name)
    target = tmp_path / "out"

    result = extract_archive(str(source), str(target), "a.7z", LIMITS)

    assert read_tree(target) == MEMBERS
    assert result.files == 2


@pytest.mark.parametrize(
    ("name", "compress"),
    [
        ("notes.txt.gz", gzip.compress),
        ("notes.txt.bz2", bz2.compress),
        ("notes.txt.xz", lzma.compress),
    ],
)
def test_a_single_compressed_file_is_unpacked(tmp_path: Path, name: str, compress: object) -> None:
    source = tmp_path / name
    source.write_bytes(compress(b"some notes\n"))  # type: ignore[operator]

    extract_archive(str(source), str(tmp_path / "out"), name, LIMITS)

    assert read_tree(tmp_path / "out") == {"notes.txt": b"some notes\n"}


# --- hostile input ----------------------------------------------------------------------


def test_members_that_try_to_escape_are_skipped(tmp_path: Path) -> None:
    source = make_zip(
        tmp_path / "evil.zip",
        {"../escape.txt": b"x", "/abs.txt": b"x", "C:/win.txt": b"x", "ok.txt": b"fine"},
    )

    result = extract_archive(str(source), str(tmp_path / "out" / "evil"), "evil.zip", LIMITS)

    assert read_tree(tmp_path / "out") == {"evil/ok.txt": b"fine"}
    assert (result.files, result.skipped) == (1, 3)
    assert not (tmp_path / "escape.txt").exists()


def test_links_and_devices_in_a_tar_are_skipped(tmp_path: Path) -> None:
    source = tmp_path / "links.tar"
    with tarfile.open(source, "w") as archive:
        for name, kind in (("link", tarfile.SYMTYPE), ("hard", tarfile.LNKTYPE)):
            info = tarfile.TarInfo(name)
            info.type, info.linkname = kind, "/etc/passwd"
            archive.addfile(info)
        info = tarfile.TarInfo("fifo")
        info.type = tarfile.FIFOTYPE
        archive.addfile(info)
        data = tarfile.TarInfo("real.txt")
        data.size = 2
        archive.addfile(data, io.BytesIO(b"ok"))

    result = extract_archive(str(source), str(tmp_path / "out"), "links.tar", LIMITS)

    assert read_tree(tmp_path / "out") == {"real.txt": b"ok"}
    assert result.skipped == 3


def test_an_archive_that_unpacks_past_the_byte_limit_fails_and_leaves_nothing(
    tmp_path: Path,
) -> None:
    source = make_zip(tmp_path / "bomb.zip", {"zeros.bin": bytes(5_000_000)})  # ~5 kB packed
    target = tmp_path / "out"

    with pytest.raises(ExtractionFailed, match="more than 1000000 bytes"):
        extract_archive(str(source), str(target), "bomb.zip", ExtractLimits(1_000_000, 10))

    assert not target.exists()
    assert not list(tmp_path.glob("*" + TEMP_SUFFIX))


def test_too_many_members_fails(tmp_path: Path) -> None:
    source = make_tar(tmp_path / "many.tar", {f"f{i}": b"" for i in range(20)}, "w")

    with pytest.raises(ExtractionFailed, match="more than 5 members"):
        extract_archive(str(source), str(tmp_path / "out"), "many.tar", ExtractLimits(10**6, 5))


@pytest.mark.parametrize("name", ["bad.zip", "bad.tar.gz", "bad.gz", "bad.7z"])
def test_a_corrupt_archive_fails_with_a_reason(tmp_path: Path, name: str) -> None:
    source = tmp_path / name
    source.write_bytes(b"this is not an archive at all" * 10)

    with pytest.raises(ExtractionFailed) as caught:
        extract_archive(str(source), str(tmp_path / "out"), name, LIMITS)

    assert not isinstance(caught.value, UnsupportedArchive)
    assert not (tmp_path / "out").exists()


def test_rar_is_recognised_but_not_unpacked(tmp_path: Path) -> None:
    source = tmp_path / "a.rar"
    source.write_bytes(b"Rar!\x1a\x07\x00")

    with pytest.raises(UnsupportedArchive, match="rar"):
        extract_archive(str(source), str(tmp_path / "out"), "a.rar", LIMITS)


# --- durable before visible -------------------------------------------------------------


def test_leftovers_of_a_killed_attempt_are_cleared_and_a_new_copy_replaces_the_old(
    tmp_path: Path,
) -> None:
    target = tmp_path / "out" / "a.zip"
    target.mkdir(parents=True)
    (target / "stale.txt").write_text("from the previous copy")
    for suffix in (TEMP_SUFFIX, OLD_SUFFIX):  # what a kill mid-extraction leaves behind
        half = target.with_name(target.name + suffix)
        half.mkdir()
        (half / "half.txt").write_text("half")
    source = make_zip(tmp_path / "a.zip", MEMBERS)

    extract_archive(str(source), str(target), "a.zip", LIMITS)

    assert read_tree(target) == MEMBERS
    assert sorted(p.name for p in target.parent.iterdir()) == ["a.zip"]

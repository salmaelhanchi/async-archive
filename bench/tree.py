"""A deterministic synthetic archive: directory tree, file bodies and their digests.

Everything is derived from ``TreeSpec``, so a test can rebuild the exact bytes the server
sent and compare them with what was mirrored, and with what was unpacked.
"""

import gzip
import hashlib
import io
import random
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cached_property, lru_cache
from urllib.parse import quote

SIDECAR_SUFFIX = ".sha256"


@dataclass(frozen=True)
class TreeSpec:
    depth: int = 2
    dirs_per_dir: int = 3
    files_per_dir: int = 4
    min_size: int = 200
    max_size: int = 5_000_000
    seed: int = 7
    checksums: bool = False  # add a ``<name>.sha256`` sidecar next to every file
    real_archives: bool = False  # ``.tar.gz`` files are real archives, not random bytes


@dataclass(frozen=True)
class Node:
    name: str
    is_dir: bool
    size: int
    modified: datetime
    payload: int = 0  # real archives: size of the random member inside


def archive_members(path: str, seed: int, payload: int) -> dict[str, bytes]:
    """What the real archive served at ``path`` contains: member name -> bytes."""
    data = random.Random(f"{seed}:{path}:payload").randbytes(payload)
    return {"README.txt": f"archive {path}\n".encode(), "payload/data.bin": data}


@lru_cache(maxsize=64)
def tar_gz(path: str, seed: int, payload: int) -> bytes:
    """A deterministic ``.tar.gz``: fixed member times and owners, gzip header time 0.

    Level 0 (stored): the payload is random and would not shrink, and the server builds
    each archive on request, so compressing would make the server the bottleneck."""
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=0, mtime=0, filename="") as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for name, data in archive_members(path, seed, payload).items():
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), 1_700_000_000, 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _sidecar_size(name: str) -> int:
    return 64 + 2 + len(name.encode("utf-8")) + 1  # "<hex>  <name>\n"


def build_tree(spec: TreeSpec) -> dict[str, list[Node]]:
    """Map every directory path (``/``, ``/dir-0/`` ...) to its children."""
    rng = random.Random(spec.seed)
    epoch = datetime(2024, 1, 1, 12, 0)
    tree: dict[str, list[Node]] = {}

    def when() -> datetime:
        return epoch + timedelta(minutes=rng.randint(0, 500_000))

    def fill(path: str, level: int) -> None:
        children: list[Node] = []
        if level < spec.depth:
            for i in range(spec.dirs_per_dir):
                children.append(Node(f"dir-{i}", True, 0, when()))
                fill(f"{path}dir-{i}/", level + 1)
        for i in range(spec.files_per_dir):
            name = (f"data-{i}.tar.gz", f"notes {i}.txt", f"a&b-{i}.bin")[i % 3]
            size = rng.randint(spec.min_size, spec.max_size)
            if spec.real_archives and name.endswith(".tar.gz"):  # drawn size -> payload
                node = Node(name, False, len(tar_gz(path + name, spec.seed, size)), when(), size)
            else:
                node = Node(name, False, size, when())
            children.append(node)
            if spec.checksums:  # no rng draw, so the rest of the tree is unchanged
                sidecar = name + SIDECAR_SUFFIX
                children.append(Node(sidecar, False, _sidecar_size(name), node.modified))
        tree[path] = children

    fill("/", 0)
    return tree


def expected_files(tree: Mapping[str, list[Node]]) -> dict[str, int]:
    """URL path of every file -> its size in bytes."""
    return {
        f"{path}{quote(node.name)}": node.size
        for path, children in tree.items()
        for node in children
        if not node.is_dir
    }


class Archive:
    """The tree plus the bytes of every file, keyed by decoded URL path (``/dir-0/a&b-2.bin``)."""

    def __init__(self, spec: TreeSpec) -> None:
        self.spec = spec
        self.tree = build_tree(spec)
        self.files: dict[str, Node] = {
            path + node.name: node
            for path, children in self.tree.items()
            for node in children
            if not node.is_dir
        }

    @property
    def real_archives(self) -> dict[str, Node]:
        """Paths of the files that are real archives."""
        return {path: node for path, node in self.files.items() if node.payload}

    def members(self, path: str) -> dict[str, bytes]:
        return archive_members(path, self.spec.seed, self.files[path].payload)

    @lru_cache(maxsize=16)  # noqa: B019 - one Archive per server, bounded cache by design
    def body(self, path: str) -> bytes:
        node = self.files[path]
        if self.spec.checksums and path.endswith(SIDECAR_SUFFIX):
            data_path = path.removesuffix(SIDECAR_SUFFIX)
            if data_path in self.files:
                name = data_path.rsplit("/", 1)[-1]
                return f"{self.digest(data_path)}  {name}\n".encode()
        if node.payload:
            return tar_gz(path, self.spec.seed, node.payload)
        return random.Random(f"{self.spec.seed}:{path}").randbytes(node.size)

    def digest(self, path: str) -> str:
        return hashlib.sha256(self.body(path)).hexdigest()

    @cached_property
    def total_bytes(self) -> int:
        return sum(node.size for node in self.files.values())

"""A deterministic synthetic archive: directory tree, file bodies and their digests.

Everything is derived from ``TreeSpec``, so a test can rebuild the exact bytes the server
sent and compare them with what was mirrored.
"""

import hashlib
import random
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


@dataclass(frozen=True)
class Node:
    name: str
    is_dir: bool
    size: int
    modified: datetime


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
            node = Node(name, False, rng.randint(spec.min_size, spec.max_size), when())
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

    @lru_cache(maxsize=16)  # noqa: B019 - one Archive per server, bounded cache by design
    def body(self, path: str) -> bytes:
        node = self.files[path]
        if self.spec.checksums and path.endswith(SIDECAR_SUFFIX):
            data_path = path.removesuffix(SIDECAR_SUFFIX)
            if data_path in self.files:
                name = data_path.rsplit("/", 1)[-1]
                return f"{self.digest(data_path)}  {name}\n".encode()
        return random.Random(f"{self.spec.seed}:{path}").randbytes(node.size)

    def digest(self, path: str) -> str:
        return hashlib.sha256(self.body(path)).hexdigest()

    @cached_property
    def total_bytes(self) -> int:
        return sum(node.size for node in self.files.values())

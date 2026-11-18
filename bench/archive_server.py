"""A local archive server for the demo, the integration tests and the benchmark.

It serves a deterministic synthetic tree as Apache, nginx or IIS directory listings, with
optional latency, robots.txt rules and 503 faults, so every run can be checked against the
exact tree it was served.

    uv run python -m bench.archive_server --style nginx --port 8080
"""

import argparse
import asyncio
import html
import random
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import quote

from aiohttp import web

STYLES = ("apache", "nginx", "iis")
HITS = web.AppKey("hits", Counter[str])  # request count per path, for tests and the benchmark


@dataclass(frozen=True)
class TreeSpec:
    depth: int = 2
    dirs_per_dir: int = 3
    files_per_dir: int = 4
    min_size: int = 200
    max_size: int = 5_000_000
    seed: int = 7


@dataclass(frozen=True)
class Node:
    name: str
    is_dir: bool
    size: int
    modified: datetime


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
            children.append(Node(name, False, rng.randint(spec.min_size, spec.max_size), when()))
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


def _apache_size(size: int) -> str:
    if size < 1024:
        return str(size)
    value = float(size)
    for unit in "KMGT":
        value /= 1024
        if value < 1024 or unit == "T":
            return f"{value:.1f}{unit}" if value < 9.95 else f"{value:.0f}{unit}"
    raise AssertionError("unreachable")


def render_apache(path: str, children: list[Node]) -> str:
    rows = [
        '<tr><td valign="top"><img src="/icons/back.gif" alt="[PARENTDIR]"></td>'
        '<td><a href="../">Parent Directory</a></td><td>&nbsp;</td>'
        '<td align="right">  - </td><td>&nbsp;</td></tr>'
    ]
    for n in children:
        href = quote(n.name) + ("/" if n.is_dir else "")
        alt = "[DIR]" if n.is_dir else "[   ]"
        size = "  - " if n.is_dir else _apache_size(n.size)
        rows.append(
            f'<tr><td valign="top"><img src="/icons/x.gif" alt="{alt}"></td>'
            f'<td><a href="{href}">{html.escape(n.name)}{"/" if n.is_dir else ""}</a></td>'
            f'<td align="right">{n.modified:%Y-%m-%d %H:%M}  </td>'
            f'<td align="right">{size}</td><td>&nbsp;</td></tr>'
        )
    return (
        f"<html><head><title>Index of {path}</title></head><body><h1>Index of {path}</h1>"
        '<table><tr><th><a href="?C=N;O=D">Name</a></th></tr>\n'
        + "\n".join(rows)
        + "\n</table><address>Apache Server</address></body></html>"
    )


def render_nginx(path: str, children: list[Node]) -> str:
    lines = ['<a href="../">../</a>']
    for n in children:
        href = quote(n.name) + ("/" if n.is_dir else "")
        label = html.escape(n.name + ("/" if n.is_dir else ""))
        size = "-" if n.is_dir else str(n.size)
        lines.append(f'<a href="{href}">{label}</a>{" " * 20}{n.modified:%d-%b-%Y %H:%M}{size:>20}')
    return (
        f"<html><head><title>Index of {path}</title></head><body>"
        f"<h1>Index of {path}</h1><hr><pre>" + "\n".join(lines) + "\n</pre><hr></body></html>"
    )


def render_iis(path: str, children: list[Node]) -> str:
    parts = ['<A HREF="/">[To Parent Directory]</A><br><br>'] if path != "/" else []
    for n in children:
        href = path + quote(n.name) + ("/" if n.is_dir else "")
        size = "&lt;dir&gt;" if n.is_dir else str(n.size)
        stamp = f"{n.modified.month}/{n.modified.day}/{n.modified:%Y %I:%M %p}"
        parts.append(f'{stamp:>24} {size:>12} <A HREF="{href}">{html.escape(n.name)}</A><br>')
    return (
        f"<html><head><title>localhost - {path}</title></head><body>"
        f"<H1>localhost - {path}</H1><hr><pre>" + "".join(parts) + "</pre><hr></body></html>"
    )


RENDERERS = {"apache": render_apache, "nginx": render_nginx, "iis": render_iis}


@dataclass
class ServerConfig:
    style: str = "nginx"
    tree: TreeSpec = field(default_factory=TreeSpec)
    latency_ms: int = 0
    robots_txt: str = "User-agent: *\nDisallow:\n"
    flaky_paths: Mapping[str, int] = field(default_factory=dict)  # path -> 503s before success


def make_app(config: ServerConfig) -> web.Application:
    tree = build_tree(config.tree)
    render = RENDERERS[config.style]
    hits: Counter[str] = Counter()
    app = web.Application()
    app[HITS] = hits

    async def handle(request: web.Request) -> web.StreamResponse:
        path = request.path
        hits[path] += 1
        if config.latency_ms:
            await asyncio.sleep(config.latency_ms / 1000)
        if path == "/robots.txt":
            return web.Response(text=config.robots_txt, content_type="text/plain")
        if hits[path] <= config.flaky_paths.get(path, 0):
            return web.Response(status=503, headers={"Retry-After": "0"}, text="busy")
        if path in tree:
            return web.Response(text=render(path, tree[path]), content_type="text/html")
        if path + "/" in tree:
            raise web.HTTPMovedPermanently(path + "/")
        raise web.HTTPNotFound

    app.router.add_route("GET", "/{tail:.*}", handle)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--style", choices=STYLES, default="nginx")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--dirs", type=int, default=4)
    parser.add_argument("--files", type=int, default=25)
    parser.add_argument("--latency-ms", type=int, default=20)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    spec = TreeSpec(
        depth=args.depth, dirs_per_dir=args.dirs, files_per_dir=args.files, seed=args.seed
    )
    files = expected_files(build_tree(spec))
    print(f"serving {len(files)} files, {sum(files.values()):,} bytes as {args.style} listings")
    config = ServerConfig(style=args.style, tree=spec, latency_ms=args.latency_ms)
    web.run_app(make_app(config), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()

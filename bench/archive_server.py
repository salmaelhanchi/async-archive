"""A local archive server for the demo, the integration tests and the benchmark.

It serves a deterministic synthetic tree (``bench/tree.py``) as Apache, nginx or IIS
directory listings, and the files themselves with HTTP range support, so every run can be
checked byte for byte against what was served. Faults are config: latency, a throughput
cap per response, 503s, connections cut mid-body, corrupted bodies, servers that ignore
``Range`` or send no validators.

    uv run python -m bench.archive_server --style nginx --port 8080
"""

import argparse
import asyncio
import html
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC
from email.utils import format_datetime
from urllib.parse import quote

from aiohttp import web

from bench.tree import Archive, Node, TreeSpec, expected_files

STYLES = ("apache", "nginx", "iis")
HITS = web.AppKey("hits", Counter[str])  # request count per path, for tests and the benchmark
RANGE_HITS = web.AppKey("range_hits", Counter[str])  # requests that carried a Range header
_RANGE = re.compile(r"^bytes=(\d+)-(\d*)$")
_CHUNK = 64 * 1024


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
    body_rate: int | None = None  # bytes/s per response, to keep transfers in flight
    cut_paths: Mapping[str, int] = field(default_factory=dict)  # path -> bodies cut halfway
    corrupt_paths: frozenset[str] = frozenset()  # one byte flipped, checksums stay true
    ranges: bool = True  # False: ignore Range and always send the whole body
    validators: bool = True  # False: no ETag or Last-Modified


def make_app(config: ServerConfig) -> web.Application:
    archives: dict[TreeSpec, Archive] = {}

    def current() -> Archive:
        """The config is read per request, so a test can change the tree or the faults
        of a running server and the mirror sees an upstream change at the same URLs."""
        if config.tree not in archives:
            archives[config.tree] = Archive(config.tree)
        return archives[config.tree]

    render = RENDERERS[config.style]
    hits: Counter[str] = Counter()
    range_hits: Counter[str] = Counter()
    cuts = Counter(config.cut_paths)
    app = web.Application()
    app[HITS] = hits
    app[RANGE_HITS] = range_hits

    async def serve_file(
        request: web.Request, archive: Archive, path: str, node: Node
    ) -> web.StreamResponse:
        body = archive.body(path)
        if path in config.corrupt_paths:
            middle = len(body) // 2
            body = body[:middle] + bytes([body[middle] ^ 0xFF]) + body[middle + 1 :]
        headers = {"Content-Type": "application/octet-stream", "Accept-Ranges": "bytes"}
        validators: set[str] = set()
        if config.validators:
            headers["ETag"] = f'"{archive.digest(path)[:20]}"'
            headers["Last-Modified"] = format_datetime(
                node.modified.replace(tzinfo=UTC), usegmt=True
            )
            validators = {headers["ETag"], headers["Last-Modified"]}
        status, start, end = 200, 0, len(body) - 1
        requested = _RANGE.match(request.headers.get("Range", ""))
        if requested:
            range_hits[path] += 1
        if_range = request.headers.get("If-Range")
        if requested and config.ranges and (if_range is None or if_range in validators):
            start = int(requested[1])
            if start >= len(body):
                headers["Content-Range"] = f"bytes */{len(body)}"
                return web.Response(status=416, headers=headers)
            if requested[2]:
                end = min(end, int(requested[2]))
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
        payload = memoryview(body)[start : end + 1]
        response = web.StreamResponse(status=status, headers=headers)
        response.content_length = len(payload)
        await response.prepare(request)
        cut_at = len(payload) // 2 if cuts[path] > 0 else None
        if cut_at is not None:
            cuts[path] -= 1
        for offset in range(0, len(payload), _CHUNK):
            if cut_at is not None and offset >= cut_at:
                raise ConnectionResetError("fault injected: body cut")
            chunk = payload[offset : offset + _CHUNK]
            await response.write(chunk)
            if config.body_rate:
                await asyncio.sleep(len(chunk) / config.body_rate)
        await response.write_eof()
        return response

    async def handle(request: web.Request) -> web.StreamResponse:
        path = request.path
        hits[path] += 1
        if config.latency_ms:
            await asyncio.sleep(config.latency_ms / 1000)
        if path == "/robots.txt":
            return web.Response(text=config.robots_txt, content_type="text/plain")
        if hits[path] <= config.flaky_paths.get(path, 0):
            return web.Response(status=503, headers={"Retry-After": "0"}, text="busy")
        archive = current()
        if path in archive.tree:
            return web.Response(text=render(path, archive.tree[path]), content_type="text/html")
        if path in archive.files:
            return await serve_file(request, archive, path, archive.files[path])
        if path + "/" in archive.tree:
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
    parser.add_argument("--max-size", type=int, default=5_000_000)
    parser.add_argument("--checksums", action="store_true", help="add .sha256 sidecars")
    parser.add_argument(
        "--real-archives", action="store_true", help="serve .tar.gz files as real archives"
    )
    parser.add_argument("--latency-ms", type=int, default=20)
    parser.add_argument("--body-rate", type=int, default=None, help="bytes/s per response")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    spec = TreeSpec(
        depth=args.depth,
        dirs_per_dir=args.dirs,
        files_per_dir=args.files,
        max_size=args.max_size,
        seed=args.seed,
        checksums=args.checksums,
        real_archives=args.real_archives,
    )
    files = expected_files(Archive(spec).tree)
    print(f"serving {len(files)} files, {sum(files.values()):,} bytes as {args.style} listings")
    config = ServerConfig(
        style=args.style, tree=spec, latency_ms=args.latency_ms, body_rate=args.body_rate
    )
    web.run_app(make_app(config), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()

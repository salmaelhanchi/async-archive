"""URL rules: one canonical form per resource, scope checks and safe local paths.

Every id in the state database is derived from the canonical URL, so a rerun over the same
archive produces the same ids and updates rows instead of duplicating them.
"""

import hashlib
import re
import string
from functools import lru_cache
from urllib.parse import quote, unquote, urlsplit, urlunsplit

_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
_PCT_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
_STRAY_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_PATH_SAFE = "/!$&'()*+,;=:@%-._~"
_DEFAULT_PORTS = {("http", 80), ("https", 443)}

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _normalize_escapes(path: str) -> str:
    """Decode escapes of unreserved characters, upper-case the rest, encode the rest."""

    def fix(match: re.Match[str]) -> str:
        char = chr(int(match[1], 16))
        return char if char in _UNRESERVED else "%" + match[1].upper()

    path = _STRAY_PERCENT.sub("%25", path)
    return quote(_PCT_ESCAPE.sub(fix, path), safe=_PATH_SAFE)


def _remove_dot_segments(path: str) -> str:
    kept: list[str] = []
    for segment in path.split("/")[1:]:
        if segment == ".":
            continue
        if segment == "..":
            if kept:
                kept.pop()
            continue
        kept.append(segment)
    result = "/" + "/".join(kept)
    if path.endswith(("/.", "/..")) and not result.endswith("/"):
        result += "/"
    return result


@lru_cache(maxsize=65_536)
def normalize_url(url: str) -> str:
    """Canonical form of an http(s) URL. Raises ``ValueError`` for anything else.

    Cached: every layer re-checks URLs it receives, and a profile of an 85-listing run showed
    11k calls for 2.2k distinct URLs.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"not an http(s) URL: {url!r}")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"URL has no host: {url!r}")
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    netloc = host if port is None or (scheme, port) in _DEFAULT_PORTS else f"{host}:{port}"
    path = _remove_dot_segments(_normalize_escapes(parts.path or "/"))
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def as_directory_url(url: str) -> str:
    """Canonical URL with a trailing slash, the form every listing URL is stored in."""
    normalized = normalize_url(url)
    parts = urlsplit(normalized)
    if parts.path.endswith("/"):
        return normalized
    return urlunsplit((parts.scheme, parts.netloc, parts.path + "/", parts.query, ""))


def host_of(url: str) -> str:
    """``host[:port]`` of a URL; the key for per-host politeness."""
    return urlsplit(normalize_url(url)).netloc


def is_within(child: str, directory: str) -> bool:
    """True when ``child`` lies strictly below the directory URL ``directory``."""
    c, d = urlsplit(normalize_url(child)), urlsplit(as_directory_url(directory))
    if (c.scheme, c.netloc) != (d.scheme, d.netloc):
        return False
    return c.path.startswith(d.path) and c.path != d.path


def file_id(url: str) -> str:
    """Stable id for a file, derived from its upstream location."""
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def _safe_segment(segment: str) -> str:
    cleaned = _UNSAFE_CHARS.sub("_", segment).rstrip(" .")
    if cleaned in ("", ".", ".."):
        return "_"
    if cleaned.split(".", 1)[0].upper() in _RESERVED_NAMES:
        return "_" + cleaned
    return cleaned


def local_relative_path(url: str) -> str:
    """Relative POSIX path a file is mirrored to: ``host/decoded/url/path``.

    Every segment is decoded and then made safe for Windows and POSIX file systems, so the
    result can never climb out of the mirror root.
    """
    parts = urlsplit(normalize_url(url))
    segments = [_safe_segment(unquote(s)) for s in parts.path.split("/") if s]
    return "/".join([_safe_segment(parts.netloc), *segments])

"""Async Archive Mirror: mirror nested directory listings with asyncio."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("async-archive-mirror")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0"

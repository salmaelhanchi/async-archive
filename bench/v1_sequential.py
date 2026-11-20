"""The baseline: a faithful reimplementation of the sequential tool this project replaces.

Same behaviour, written fresh for the benchmark:

* one ``requests.Session`` (connections are reused);
* phase 1, depth-first crawl of IIS ``<pre>`` listings, ``time.sleep(0.2)`` before each
  listing; a link whose preceding text holds ``<dir>`` is a directory, otherwise the date and
  size are read from that text;
* files are de-duplicated by local path and written to a metadata CSV;
* phase 2, downloads one after another, ``time.sleep(0.5)`` before each, streamed in
  16 KiB chunks with ``requests``;
* no retries, no resume, no verification, no robots.txt.

Two things are left out on purpose: rotating browser User-Agents (one honest UA instead;
it does not change the timing) and the source archive's own path rules (local paths are
``<out>/<host>/<url path>``).

    uv run python -m bench.v1_sequential http://127.0.0.1:8080/ --out data/v1
"""

import argparse
import csv
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

LISTING_DELAY_S = 0.2
FILE_DELAY_S = 0.5
CHUNK_BYTES = 16 * 1024
USER_AGENT = "async-archive-mirror-bench/v1"
_IIS_META = re.compile(
    r"(?P<date>\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s+[AP]M)\s+(?P<size>[\d,]+)"
)


class SequentialMirror:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.visited: set[str] = set()
        self.files: list[dict[str, str]] = []

    def crawl(self, directory_url: str) -> None:
        """Depth first, like the original: a subdirectory is crawled as soon as it is seen."""
        if directory_url in self.visited:
            return
        self.visited.add(directory_url)
        time.sleep(LISTING_DELAY_S)
        try:
            response = self.session.get(directory_url, timeout=25)
            response.raise_for_status()
        except requests.RequestException as exc:
            print(f"listing failed: {directory_url}: {exc}", file=sys.stderr)
            return
        pre = BeautifulSoup(response.content, "html.parser").find("pre")
        if not isinstance(pre, Tag):
            return
        for link in pre.find_all("a"):
            href = link.get("href")
            if not isinstance(href, str) or link.text.strip() == "[To Parent Directory]":
                continue
            before = link.previous_sibling
            if not isinstance(before, NavigableString):
                continue
            meta = str(before).strip()
            url = urljoin(directory_url, href)
            if "<dir>" in meta:
                self.crawl(url)
                continue
            match = _IIS_META.search(meta)
            self.files.append(
                {
                    "url": url,
                    "local_path": str(self.local_path(url)),
                    "name": link.text.strip(),
                    "date": match["date"] if match else "N/A",
                    "size": match["size"].replace(",", "") if match else "N/A",
                }
            )

    def local_path(self, url: str) -> Path:
        parts = urlsplit(url)
        return self.out / parts.netloc.replace(":", "_") / unquote(parts.path).lstrip("/")

    def save_metadata(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        with (self.out / "files_metadata.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["url", "local_path", "name", "date", "size"])
            writer.writeheader()
            writer.writerows(self.files)

    def download_all(self) -> tuple[int, int]:
        done = received = 0
        for info in self.files:
            path = Path(info["local_path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            time.sleep(FILE_DELAY_S)
            try:
                with self.session.get(info["url"], stream=True, timeout=120) as response:
                    response.raise_for_status()
                    with path.open("wb") as handle:
                        for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
                            handle.write(chunk)
                            received += len(chunk)
            except (requests.RequestException, OSError) as exc:
                print(f"download failed: {info['url']}: {exc}", file=sys.stderr)
                continue
            done += 1
        return done, received

    def run(self, start_urls: list[str]) -> tuple[int, int]:
        for url in start_urls:
            self.crawl(url)
        unique = {f["local_path"]: f for f in self.files}
        self.files = list(unique.values())
        self.save_metadata()
        return self.download_all()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sequential baseline for the benchmark.")
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--out", default="data/v1")
    args = parser.parse_args()
    done, received = SequentialMirror(Path(args.out)).run(args.urls)
    print(f"v1: {done} files, {received} bytes")


if __name__ == "__main__":
    main()

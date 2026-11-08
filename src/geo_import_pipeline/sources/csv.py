"""CSV from a URL, a yearly URL pattern or a local file, optionally inside a ZIP archive."""

from __future__ import annotations

import asyncio
import csv
import fnmatch
import io
import json
import logging
import os
import re
import subprocess
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Iterator
from urllib.parse import urlsplit

from ..model import FetchError, IncompleteFetchError, RawRecord
from ..transform import compile_filters
from .base import RetryableHTTPError, SourceAdapter, check_status, register, with_retries

log = logging.getLogger(__name__)
PROGRESS_EVERY = 200_000


def _remote_meta(headers) -> dict:
    return {
        "etag": headers.get("etag"),
        "last_modified": headers.get("last-modified"),
        "content_length": headers.get("content-length"),
    }


def _is_same(cached: dict | None, remote: dict) -> bool:
    if not cached:
        return False
    if remote["etag"] or remote["last_modified"]:
        return all(cached.get(k) == remote[k] for k in ("etag", "last_modified", "content_length"))
    return False


def _escape_member(name: str) -> str:
    """unzip treats member names as patterns."""
    return re.sub(r"([*?\[\]\\])", r"\\\1", name)


@register("csv")
class CsvAdapter(SourceAdapter):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.cfg = self.source.csv

    # --- locating and caching the file ----------------------------------------------------

    async def _head(self, url: str):
        async def attempt():
            response = await self.client.head(url, timeout=self.cfg.timeout_s)
            if response.status_code == 429 or response.status_code >= 500 and response.status_code != 501:
                raise RetryableHTTPError(f"HTTP {response.status_code}")
            return response

        return await with_retries(self.cfg, f"HEAD {url}", attempt)

    async def _resolve_url(self) -> tuple[str, dict | None]:
        """Return the URL to load and its metadata. For a pattern, the newest existing year wins."""
        if self.cfg.url is not None:
            response = await self._head(self.cfg.url)
            if response.status_code in (405, 501):  # Server does not offer HEAD.
                return self.cfg.url, None
            check_status(response, f"HEAD {self.cfg.url}")
            return self.cfg.url, _remote_meta(response.headers)

        newest = datetime.now(timezone.utc).year
        tried = []
        for year in range(newest, newest - self.cfg.year_lookback - 1, -1):
            url = self.cfg.url_template.replace("{year}", str(year))
            response = await self._head(url)
            if response.status_code == 404:
                tried.append(str(year))
                continue
            check_status(response, f"HEAD {url}")
            return url, _remote_meta(response.headers)
        raise FetchError(f"No file found for the years {', '.join(tried)}")

    async def _download(self, url: str, target: Path, expected: str | None) -> dict:
        partial = target.with_name(target.name + ".part")

        async def attempt() -> dict:
            size = 0
            async with self.client.stream("GET", url, timeout=self.cfg.timeout_s) as response:
                check_status(response, f"GET {url}")
                announced = response.headers.get("content-length") or expected
                with partial.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        handle.write(chunk)
                        size += len(chunk)
                meta = _remote_meta(response.headers)
            if announced is not None and size != int(announced):
                raise RetryableHTTPError(f"Received {size} of {announced} bytes")
            return meta

        try:
            meta = await with_retries(self.cfg, f"Download {url}", attempt)
        except FetchError as exc:
            partial.unlink(missing_ok=True)
            raise IncompleteFetchError(str(exc)) from exc
        os.replace(partial, target)
        return meta

    async def _local_file(self) -> Path:
        """Provide the file on disk. An unchanged remote file is not downloaded again."""
        if self.cfg.path is not None:
            path = Path(self.cfg.path)
            if not path.is_file():
                raise FetchError(f"File not found: {path}")
            stat = path.stat()
            self.version = f"{path.name}; modified {datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()}"
            return path

        url, remote = await self._resolve_url()
        name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(urlsplit(url).path).name) or "download"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        target = self.cache_dir / name
        sidecar = target.with_name(target.name + ".meta.json")
        cached = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else None

        if target.exists() and remote is not None and _is_same(cached, remote):
            log.info("%s: %s is unchanged, using the cached copy", self.source.id, name)
            meta = remote
        else:
            log.info("%s: downloading %s", self.source.id, url)
            meta = await self._download(url, target, remote["content_length"] if remote else None)
            sidecar.write_text(json.dumps(meta), encoding="utf-8")
            for other in self.cache_dir.iterdir():  # Keep only the current version.
                if other not in (target, sidecar):
                    other.unlink()
        self.version = f"{name}; last-modified {meta['last_modified'] or 'unknown'}"
        return target

    # --- reading --------------------------------------------------------------------------

    def _archive_member(self, archive: Path) -> str:
        try:
            listing = subprocess.run(["unzip", "-Z1", str(archive)], capture_output=True, check=False)
        except FileNotFoundError as exc:
            raise FetchError("The program 'unzip' is not installed") from exc
        if listing.returncode != 0:
            raise IncompleteFetchError(f"Archive cannot be read: {listing.stderr.decode('utf-8', 'replace')[:300]}")
        names = listing.stdout.decode("utf-8", "replace").splitlines()
        matches = [name for name in names if fnmatch.fnmatchcase(Path(name).name, self.cfg.member)]
        if len(matches) != 1:
            raise IncompleteFetchError(
                f"Expected exactly one archive member matching '{self.cfg.member}', found {len(matches)}"
            )
        return matches[0]

    def _rows(self, path: Path) -> Iterator[list[str]]:
        """Yield parsed rows. A damaged archive raises at the end, before anything is compared."""
        if self.cfg.archive == "none":
            with path.open(encoding=self.cfg.encoding, newline="") as handle:
                yield from csv.reader(handle, delimiter=self.cfg.delimiter, quotechar=self.cfg.quotechar)
            return

        member = self._archive_member(path)
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                ["unzip", "-p", str(path), _escape_member(member)], stdout=subprocess.PIPE, stderr=errors
            )
            finished = False
            try:
                text = io.TextIOWrapper(process.stdout, encoding=self.cfg.encoding, newline="")
                yield from csv.reader(text, delimiter=self.cfg.delimiter, quotechar=self.cfg.quotechar)
                finished = True
            finally:
                if not finished:  # Stopped early: unzip would block forever on the full pipe.
                    process.kill()
                code = process.wait()
            if code != 0:
                errors.seek(0)
                message = errors.read().decode("utf-8", "replace")[:300]
                raise IncompleteFetchError(f"Archive is damaged (unzip exit code {code}): {message}")

    async def records(self) -> AsyncIterator[RawRecord]:
        path = await self._local_file()
        with closing(self._rows(path)) as rows:
            async for record in self._parse(rows):
                yield record

    async def _parse(self, rows: Iterator[list[str]]) -> AsyncIterator[RawRecord]:
        try:
            header = next(rows, None)
        except UnicodeDecodeError as exc:
            raise IncompleteFetchError(f"File is not valid {self.cfg.encoding}: {exc}") from exc
        if header is None:
            raise IncompleteFetchError("File is empty")

        index = {name.strip(): position for position, name in enumerate(header)}
        wanted = [spec.path for spec in self.source.fields.values() if spec.path]
        missing = [name for name in wanted if name not in index]
        if missing:
            raise IncompleteFetchError(f"Missing column(s) {missing}; file has {list(index)}")

        columns = [(name, index[name]) for name in wanted]
        filters = [(index[name], predicate) for name, predicate in compile_filters(self.source)]
        width = len(header)
        read = filtered = 0
        try:
            for line, row in enumerate(rows, start=2):
                read += 1
                if read % PROGRESS_EVERY == 0:
                    self.stats.read, self.stats.filtered_out = read, filtered
                    if read % (PROGRESS_EVERY * 25) == 0:
                        log.info("%s: %d rows read", self.source.id, read)
                    await asyncio.sleep(0)
                if len(row) != width:
                    self.stats.skipped += 1
                    self.stats.warnings.add(
                        "malformed_row", f"Row does not have {width} columns and was skipped", f"line {line}"
                    )
                    continue
                for position, predicate in filters:
                    if not predicate(row[position]):
                        filtered += 1
                        break
                else:
                    yield RawRecord(values={name: row[i] for name, i in columns}, ref=f"line {line}")
        except UnicodeDecodeError as exc:
            raise IncompleteFetchError(f"File is not valid {self.cfg.encoding}: {exc}") from exc
        finally:
            self.stats.read, self.stats.filtered_out = read, filtered

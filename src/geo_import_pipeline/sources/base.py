"""The adapter interface and registry.

A new source format is added by a module that registers an adapter class. The core
of the pipeline does not change. Set GIP_PLUGIN_MODULES to load such modules.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Iterable, TypeVar

import httpx
from lxml import etree

from ..config import ConfigError, HttpConfig, SourceConfig
from ..model import FetchError, RawRecord, SourceStats

log = logging.getLogger(__name__)
T = TypeVar("T")

_REGISTRY: dict[str, type["SourceAdapter"]] = {}


def register(type_name: str) -> Callable[[type["SourceAdapter"]], type["SourceAdapter"]]:
    def decorator(cls: type["SourceAdapter"]) -> type["SourceAdapter"]:
        _REGISTRY[type_name] = cls
        return cls

    return decorator


def load_plugins(modules: Iterable[str]) -> None:
    """Import additional adapter modules. Registering happens as a side effect of the import."""
    for name in modules:
        try:
            importlib.import_module(name)
        except ImportError as exc:
            raise ConfigError(f"Adapter module '{name}' cannot be imported: {exc}") from exc


def adapter_for(source: SourceConfig, client: httpx.AsyncClient, stats: SourceStats, cache_dir: Path) -> "SourceAdapter":
    try:
        cls = _REGISTRY[source.type]
    except KeyError:
        raise FetchError(f"No adapter registered for source type '{source.type}'") from None
    return cls(source, client, stats, cache_dir)


class SourceAdapter(ABC):
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient, stats: SourceStats, cache_dir: Path) -> None:
        self.source = source
        self.client = client
        self.stats = stats
        self.cache_dir = cache_dir / source.id
        self.version: str | None = None

    @abstractmethod
    def records(self) -> AsyncIterator[RawRecord]:
        """Yield all records. Raises FetchError or IncompleteFetchError if that is not possible."""


class RetryableHTTPError(Exception):
    pass


RETRYABLE = (httpx.TransportError, RetryableHTTPError, etree.XMLSyntaxError)


async def with_retries(config: HttpConfig, description: str, attempt: Callable[[], Awaitable[T]]) -> T:
    """Run a request with exponential backoff on transient failures."""
    last_error: Exception | None = None
    for number in range(config.retries + 1):
        if number:
            delay = config.backoff_s * 2 ** (number - 1)
            log.warning("%s failed (%s), retry %d in %.0fs", description, last_error, number, delay)
            await asyncio.sleep(delay)
        try:
            return await attempt()
        except RETRYABLE as exc:
            last_error = exc
    raise FetchError(f"{description} failed after {config.retries + 1} attempts: {last_error}") from last_error


def check_status(response: httpx.Response, description: str) -> None:
    if response.status_code == 429 or response.status_code >= 500:
        raise RetryableHTTPError(f"HTTP {response.status_code}")
    if response.status_code >= 400:
        raise FetchError(f"{description}: HTTP {response.status_code}")

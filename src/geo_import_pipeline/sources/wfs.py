"""WFS 2.0.0 and 1.1.0 with paging and a verifiable completeness check."""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from lxml import etree

from ..model import FetchError, IncompleteFetchError, RawRecord
from .base import RetryableHTTPError, SourceAdapter, check_status, register, with_retries

log = logging.getLogger(__name__)


def _local(tag: object) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _new_parser(**kwargs) -> etree.XMLPullParser:
    # huge_tree: large municipalities exceed libxml2's default text node limit.
    # resolve_entities and no_network stay off: responses are untrusted input.
    return etree.XMLPullParser(
        huge_tree=True,
        resolve_entities=False,
        no_network=True,
        remove_comments=True,
        remove_pis=True,
        **kwargs,
    )


def _raise_for_exception_report(root: etree._Element) -> None:
    if _local(root.tag) in ("ExceptionReport", "ServiceExceptionReport"):
        texts = [text.strip() for text in root.itertext() if text and text.strip()]
        raise RetryableHTTPError(f"Service exception: {' '.join(texts)[:500]}")


def _lookup(element: etree._Element, path: str) -> str | None:
    """Resolve 'a/b' by local names; '@id' reads an attribute regardless of its namespace."""
    current = element
    parts = path.split("/")
    for part in parts:
        if part.startswith("@"):
            name = part[1:].split(":")[-1]
            for key, value in current.attrib.items():
                if _local(key) == name:
                    return value
            return None
        for child in current:
            if _local(child.tag) == part:
                current = child
                break
        else:
            return None
    return current.text


def _find_child(element: etree._Element, path: str) -> etree._Element | None:
    current = element
    for part in path.split("/"):
        for child in current:
            if _local(child.tag) == part:
                current = child
                break
        else:
            return None
    return current


@register("wfs")
class WfsAdapter(SourceAdapter):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.cfg = self.source.wfs
        self._paths = [spec.path for spec in self.source.fields.values() if spec.path]
        self._feature_tag = "{*}" + self.cfg.typename.split(":")[-1]

    def _params(self, **extra: str) -> dict[str, str]:
        is_v2 = self.cfg.version == "2.0.0"
        params = {
            "SERVICE": "WFS",
            "VERSION": self.cfg.version,
            "REQUEST": "GetFeature",
            ("TYPENAMES" if is_v2 else "TYPENAME"): self.cfg.typename,
            "SRSNAME": self.cfg.source_crs,
        }
        if self.cfg.output_format:
            params["OUTPUTFORMAT"] = self.cfg.output_format
        if self.cfg.sort_by:
            params["SORTBY"] = self.cfg.sort_by
        params.update(extra)
        return params

    async def _hits(self) -> int:
        async def attempt() -> int:
            response = await self.client.get(
                self.cfg.url, params=self._params(RESULTTYPE="hits"), timeout=self.cfg.timeout_s
            )
            check_status(response, "WFS hits request")
            parser = _new_parser()
            parser.feed(response.content)
            root = parser.close()
            _raise_for_exception_report(root)
            value = root.get("numberMatched") or root.get("numberOfFeatures")
            if value is None or not value.isdigit():
                raise IncompleteFetchError(
                    f"Service reports no usable feature count ({value!r}), completeness cannot be verified"
                )
            return int(value)

        return await with_retries(self.cfg, "WFS hits request", attempt)

    async def _page(self, start: int) -> list[RawRecord]:
        size_param = "COUNT" if self.cfg.version == "2.0.0" else "MAXFEATURES"
        params = self._params(**{size_param: str(self.cfg.page_size), "STARTINDEX": str(start)})

        async def attempt() -> list[RawRecord]:
            parser = _new_parser(events=("end",), tag=self._feature_tag)
            page: list[RawRecord] = []
            async with self.client.stream("GET", self.cfg.url, params=params, timeout=self.cfg.timeout_s) as response:
                check_status(response, f"WFS page at index {start}")
                async for chunk in response.aiter_bytes():
                    parser.feed(chunk)
                    self._collect(parser, page)
            root = parser.close()
            self._collect(parser, page)
            _raise_for_exception_report(root)
            return page

        return await with_retries(self.cfg, f"WFS page at index {start}", attempt)

    def _collect(self, parser: etree.XMLPullParser, page: list[RawRecord]) -> None:
        for _, element in parser.read_events():
            page.append(self._to_record(element))
            element.clear()

    def _to_record(self, element: etree._Element) -> RawRecord:
        holder = _find_child(element, self.cfg.geometry_path)
        gml = None
        if holder is not None and len(holder):
            gml = etree.tostring(holder[0], encoding="unicode", with_tail=False)
        values = {path: _lookup(element, path) for path in self._paths}
        reference = _lookup(element, "@id") or ""
        return RawRecord(values=values, gml=gml, ref=reference)

    async def records(self) -> AsyncIterator[RawRecord]:
        expected = await self._hits()
        log.info("%s: service reports %d features", self.source.id, expected)
        self.version = f"numberMatched={expected}"
        received = 0
        while received < expected:
            if received:
                await asyncio.sleep(self.cfg.page_delay_s)
            page = await self._page(received)
            if not page:
                break
            for record in page:
                yield record
            received += len(page)
            self.stats.read = received
            log.info("%s: %d/%d features", self.source.id, received, expected)
        if received != expected:
            raise IncompleteFetchError(f"Received {received} features, service reported {expected}")

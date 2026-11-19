"""Profile discovery from XML sitemaps.

Sitemaps are the most generic and the most polite way to enumerate an organisation's people:
one or a handful of requests per organisation, published by the site for crawlers, and they
avoid the people-search endpoints that several organisations disallow in robots.txt.
"""

import gzip
import re
from collections.abc import Callable
from dataclasses import dataclass
from xml.etree.ElementTree import Element, ParseError

from defusedxml.ElementTree import fromstring

from radar.config import Discovery
from radar.scraping.fetcher import Fetcher

MAX_CHILD_SITEMAPS = 50


@dataclass(frozen=True)
class SitemapEntry:
    url: str
    lastmod: str | None = None


@dataclass
class ParsedSitemap:
    entries: list[SitemapEntry]
    children: list[str]  # child sitemap URLs when this is a sitemap index


def parse_sitemap(content: bytes) -> ParsedSitemap:
    """Raises ValueError for anything that isn't a readable sitemap, so callers catch one type."""
    try:
        if content[:2] == b"\x1f\x8b":
            content = gzip.decompress(content)
        root: Element = fromstring(content)
    except (ParseError, OSError, EOFError) as exc:  # bad XML; corrupt or truncated gzip
        raise ValueError(f"not a valid sitemap: {exc}") from exc
    entries: list[SitemapEntry] = []
    children: list[str] = []
    is_index = _local(root.tag) == "sitemapindex"
    for node in root:
        loc = lastmod = None
        for child in node:
            name = _local(child.tag)
            if name == "loc" and child.text:
                loc = child.text.strip()
            elif name == "lastmod" and child.text:
                lastmod = child.text.strip()
        if not loc:
            continue
        if is_index:
            children.append(loc)
        else:
            entries.append(SitemapEntry(loc, lastmod))
    return ParsedSitemap(entries, children)


async def discover(
    fetcher: Fetcher, discovery: Discovery, via: Callable[[str], str] = lambda url: url
) -> list[SitemapEntry]:
    """All profile URLs (deduplicated) reachable from the configured sitemaps.

    `via` rewrites each sitemap URL before fetching - used to read an archived copy.
    """
    found: dict[str, SitemapEntry] = {}
    queue = list(discovery.urls)
    seen: set[str] = set()
    while queue and len(seen) < MAX_CHILD_SITEMAPS:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        result = await fetcher.get(via(url))
        if result.status != 200:
            continue
        parsed = parse_sitemap(result.content)
        for child in parsed.children:
            if discovery.child_include is None or re.search(discovery.child_include, child):
                queue.append(child)
        for entry in parsed.entries:
            url = entry.url.rstrip("/")  # one canonical form, whether or not the site uses trailing slashes
            if discovery.matches(url):
                found[url] = SitemapEntry(url, entry.lastmod)
    return list(found.values())


def _local(tag: str) -> str:
    """Strip the XML namespace: '{http://www.sitemaps.org/...}loc' -> 'loc'."""
    return tag.rsplit("}", 1)[-1]

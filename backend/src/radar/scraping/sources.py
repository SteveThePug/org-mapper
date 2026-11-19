"""Profile discovery, whatever the source: one function per `discovery.type`.

Every source answers the same question, "which profile pages exist at this organisation right
now?", as a list of `SitemapEntry(url, lastmod)`. The crawler diffs that list against what it
knows, so the rest of the pipeline doesn't care where the URLs came from.

- `sitemap`: the organisation's XML sitemaps (radar/scraping/sitemap.py). Cheapest and politest.
- `listing`: links on ordinary HTML pages ("Our people", a squad page), following "next" links.

Adding a source = a config model in radar/config.py (`AnyDiscovery`) + a function here.
Sources that report *moves* rather than profiles (news pages, social-media posts: "X joins
from Y") would be a separate kind of source that creates events; they are not built yet, as
each platform needs its own API access and a check of its terms.
"""

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from radar.config import Discovery, ListingDiscovery
from radar.scraping import sitemap
from radar.scraping.fetcher import Fetcher
from radar.scraping.sitemap import SitemapEntry

MAX_LISTING_PAGES = 30

Rewrite = Callable[[str], str]


def _same(url: str) -> str:
    return url


async def discover_listing(
    fetcher: Fetcher, discovery: ListingDiscovery, via: Rewrite = _same
) -> list[SitemapEntry]:
    """Profile links on the listing pages, following the "next page" link if one is configured."""
    found: dict[str, SitemapEntry] = {}
    queue = list(discovery.urls)
    seen: set[str] = set()
    while queue and len(seen) < MAX_LISTING_PAGES:
        page_url = queue.pop(0)
        if page_url in seen:
            continue
        seen.add(page_url)
        result = await fetcher.get(via(page_url))
        if result.status != 200:
            continue
        links, next_pages = listing_links(result.text, page_url, discovery)
        for url in links:
            found[url] = SitemapEntry(url)  # an HTML listing has no lastmod
        queue.extend(next_pages)
    return list(found.values())


def listing_links(html: str, page_url: str, discovery: ListingDiscovery) -> tuple[list[str], list[str]]:
    """(profile URLs, next-page URLs) on one listing page. Pure, so it's easy to test."""
    tree = HTMLParser(html)
    profiles: list[str] = []
    for node in tree.css(discovery.link_selector):
        href = node.attributes.get("href")
        if not href:
            continue
        url = urljoin(page_url, href).split("#")[0].rstrip("/")
        if discovery.matches(url) and url not in profiles:
            profiles.append(url)
    next_pages: list[str] = []
    if discovery.next_selector:
        for node in tree.css(discovery.next_selector):
            href = node.attributes.get("href")
            if href:
                next_pages.append(urljoin(page_url, href))
    return profiles, next_pages


async def _discover_sitemap(fetcher: Fetcher, discovery: Any, via: Rewrite = _same) -> list[SitemapEntry]:
    return await sitemap.discover(fetcher, discovery, via)


async def _discover_listing(fetcher: Fetcher, discovery: Any, via: Rewrite = _same) -> list[SitemapEntry]:
    return await discover_listing(fetcher, discovery, via)


SOURCES: dict[str, Callable[[Fetcher, Any, Rewrite], Awaitable[list[SitemapEntry]]]] = {
    "sitemap": _discover_sitemap,
    "listing": _discover_listing,
}


async def discover(
    fetcher: Fetcher, discovery: Discovery | ListingDiscovery, via: Rewrite = _same
) -> list[SitemapEntry]:
    """All profile URLs this organisation's configured source lists. `via` rewrites each URL
    before fetching (used to read an archived copy)."""
    return await SOURCES[discovery.type](fetcher, discovery, via)

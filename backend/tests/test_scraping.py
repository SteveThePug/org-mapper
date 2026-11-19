"""Politeness rules and sitemap discovery, against a mocked website."""

import gzip

import httpx
import pytest
import respx

from radar.config import Discovery
from radar.scraping.fetcher import (
    Blocked,
    CrawlerNotIdentified,
    DisallowedByRobots,
    Fetcher,
    PrivateAddress,
    check_user_agent,
)
from radar.scraping.sitemap import discover, parse_sitemap
from radar.settings import Settings

SITE = "https://organisation.test"
URLSET = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://organisation.test/people/a</loc><lastmod>2026-11-01</lastmod></url>
<url><loc>https://organisation.test/people/b/</loc></url>
<url><loc>https://organisation.test/news/x</loc></url></urlset>"""
INDEX = """<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://organisation.test/lawyer-sitemap.xml</loc></sitemap>
<sitemap><loc>https://organisation.test/post-sitemap.xml</loc></sitemap></sitemapindex>"""


def fetcher() -> Fetcher:
    return Fetcher("TestBot/1.0", min_delay_s=0)


def test_parse_sitemap_handles_gzip_and_indexes() -> None:
    assert [e.url for e in parse_sitemap(gzip.compress(URLSET.encode())).entries][:1] == [
        "https://organisation.test/people/a"
    ]
    assert parse_sitemap(INDEX.encode()).children == [
        "https://organisation.test/lawyer-sitemap.xml",
        "https://organisation.test/post-sitemap.xml",
    ]


@respx.mock
async def test_discover_follows_only_matching_child_sitemaps() -> None:
    respx.get(f"{SITE}/robots.txt").respond(404)
    respx.get(f"{SITE}/sitemap_index.xml").respond(200, text=INDEX)
    lawyers = respx.get(f"{SITE}/lawyer-sitemap.xml").respond(200, text=URLSET)
    posts = respx.get(f"{SITE}/post-sitemap.xml").respond(200, text=URLSET)
    discovery = Discovery(
        urls=[f"{SITE}/sitemap_index.xml"],
        child_include="lawyer-sitemap",
        include=r"^https://organisation\.test/people/",
    )
    entries = await discover(fetcher(), discovery)
    assert sorted(e.url for e in entries) == [
        "https://organisation.test/people/a",
        "https://organisation.test/people/b",
    ]
    assert lawyers.called and not posts.called


@respx.mock
async def test_robots_disallow_is_respected() -> None:
    respx.get(f"{SITE}/robots.txt").respond(200, text="User-agent: *\nDisallow: /search\n")
    page = respx.get(f"{SITE}/search").respond(200)
    with pytest.raises(DisallowedByRobots):
        await fetcher().get(f"{SITE}/search")
    assert not page.called


@respx.mock
async def test_robots_server_error_means_do_not_crawl() -> None:
    respx.get(f"{SITE}/robots.txt").respond(503)
    with pytest.raises(Blocked):
        await fetcher().get(f"{SITE}/people/a")


@respx.mock
async def test_403_stops_instead_of_retrying() -> None:
    respx.get(f"{SITE}/robots.txt").respond(404)
    route = respx.get(f"{SITE}/people/a").respond(403)
    with pytest.raises(Blocked):
        await fetcher().get(f"{SITE}/people/a")
    assert route.call_count == 1


@respx.mock
async def test_429_honours_retry_after_then_succeeds() -> None:
    respx.get(f"{SITE}/robots.txt").respond(404)
    route = respx.get(f"{SITE}/people/a")
    route.side_effect = [httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, text="ok")]
    result = await fetcher().get(f"{SITE}/people/a")
    assert result.status == 200 and route.call_count == 2


@respx.mock
async def test_conditional_get_headers_are_sent() -> None:
    respx.get(f"{SITE}/robots.txt").respond(404)
    route = respx.get(f"{SITE}/people/a").respond(304)
    result = await fetcher().get(
        f"{SITE}/people/a", etag='"abc"', last_modified="Wed, 01 Jan 2026 00:00:00 GMT"
    )
    assert result.status == 304
    sent = route.calls.last.request.headers
    assert sent["If-None-Match"] == '"abc"' and sent["User-Agent"] == "TestBot/1.0"


@pytest.mark.parametrize(
    "user_agent",
    [
        "PartnerRadarBot/0.1 (+mailto:change-me@example.com)",  # the shipped placeholder
        "PartnerRadarBot/0.1 (+set SCRAPER_USER_AGENT to include a contact address)",  # the code default
        "PartnerRadarBot/0.1",  # no contact at all
    ],
)
def test_crawling_refused_without_operator_contact(user_agent: str) -> None:
    with pytest.raises(CrawlerNotIdentified):
        Fetcher.from_settings(Settings(scraper_user_agent=user_agent))


def test_crawling_allowed_with_operator_contact() -> None:
    check_user_agent("PartnerRadarBot/0.1 (+mailto:ops@recruiter.co.uk)")
    check_user_agent("PartnerRadarBot/0.1 (+https://recruiter.co.uk/bot)")


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1/", "http://169.254.169.254/latest/", "http://[::1]/", "http://10.0.0.5/x"]
)
async def test_private_addresses_are_never_fetched(url: str) -> None:
    guarded = Fetcher("TestBot/1.0", min_delay_s=0, allow_private=False)
    try:
        with pytest.raises(Blocked, match="private or local"):  # caught at robots.txt already
            await guarded.get(url)
        _, reason = await guarded.robots(url)
        assert reason and "private" in reason
    finally:
        await guarded.aclose()


@respx.mock
async def test_a_redirect_cannot_lead_to_a_private_address() -> None:
    respx.get(f"{SITE}/robots.txt").respond(404)
    respx.get(f"{SITE}/people").respond(302, headers={"Location": "http://127.0.0.1/admin"})
    guarded = Fetcher("TestBot/1.0", min_delay_s=0, allow_private=False)
    try:
        with pytest.raises(PrivateAddress):
            await guarded.get(f"{SITE}/people")
    finally:
        await guarded.aclose()


def test_the_app_fetcher_refuses_private_addresses_by_default() -> None:
    hooks = Fetcher.from_settings(Settings(scraper_user_agent="TestBot/1.0 (+https://test.invalid)"))._client
    assert hooks.event_hooks["request"]

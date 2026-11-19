"""AI-assisted organisation setup: the evidence we gather, and how the model's answer is checked."""

import json
from typing import Any

import httpx
import pytest
import respx
from conftest import fixture_html
from test_gateway import FakeCompletions, gateway

from radar.config import Discovery, DomainConfig, ListingDiscovery
from radar.db import Database
from radar.llm.onboard import OrgSuggestion, clean_suggestion, suggest_org
from radar.scraping.fetcher import Fetcher
from radar.scraping.onboard import (
    Evidence,
    OnboardingError,
    gather_evidence,
    group_by_prefix,
    homepage,
    looks_like_url,
    page_skeleton,
    people_links,
)

SITE = "https://club.test"


def test_url_or_name() -> None:
    assert looks_like_url("weil.com")
    assert looks_like_url("https://www.club.co.uk/en/squad")
    assert not looks_like_url("Slaughter and May")
    assert homepage("WWW.Club.test/en/squad") == "https://www.club.test/"


@pytest.mark.parametrize("bad", ["not sure", "https://", "http://[::1", "https://exa mple.com", "localhost"])
def test_homepage_refuses_what_isnt_a_website(bad: str) -> None:
    with pytest.raises(OnboardingError):
        homepage(bad)


def test_nulls_in_the_model_answer_dont_discard_it() -> None:
    raw = OrgSuggestion.model_validate(
        {
            "name": None,
            "kind": None,
            "discovery": None,
            "selectors": {"title": None, "name": "h1"},
            "notes": None,
        }
    )
    assert raw.selectors == {"name": "h1"}
    assert raw.kind == ""


@respx.mock
async def test_an_unreachable_site_is_not_reported_as_blocking_us(domain: DomainConfig) -> None:

    respx.get(f"{SITE}/robots.txt").mock(side_effect=httpx.ConnectError("no such host"))
    fetcher = Fetcher("TestBot/1.0", min_delay_s=0)
    try:
        with pytest.raises(OnboardingError, match="Couldn't reach"):
            await gather_evidence(fetcher, SITE + "/", domain.fields)
    finally:
        await fetcher.aclose()


def test_urls_are_grouped_by_path_biggest_first() -> None:
    urls = [f"{SITE}/players/p{i}" for i in range(5)] + [f"{SITE}/news/n1", f"{SITE}/about"]
    groups = group_by_prefix(urls)
    assert groups[0]["prefix"] == f"{SITE}/players"
    assert groups[0]["count"] == 5
    assert len(groups[0]["examples"]) == 3


def test_skeleton_shows_classes_and_hides_contact_details_and_menus() -> None:
    skeleton = page_skeleton(fixture_html("skadden.html"))
    assert "a.offices-related-name: Abu Dhabi" in skeleton
    assert "@" not in skeleton
    assert "[email]" in skeleton
    assert len(skeleton) <= 6000


def test_people_links_are_same_site_only() -> None:
    html = '<a href="/squad">First team</a><a href="https://other.test/people">People</a><a href="/shop">Shop</a>'
    assert people_links(html, SITE + "/", SITE + "/") == [{"text": "First team", "url": f"{SITE}/squad"}]


def test_clean_suggestion_keeps_a_good_sitemap_answer(domain: DomainConfig) -> None:
    raw = OrgSuggestion(
        name="Club FC",
        kind="Football Club",
        discovery={
            "type": "sitemap",
            "urls": [f"{SITE}/sitemap.xml"],
            "include": r"^https://club\.test/players/[^/]+$",
        },
        selectors={"title": ".player-position", "shoe_size": ".x"},
        notes="Players are in the sitemap.",
    )
    org, warnings = clean_suggestion(raw, SITE + "/", "club", domain)
    assert isinstance(org.discovery, Discovery)
    assert org.kind == "football_club"
    assert org.selectors == {"title": ".player-position"}
    assert any("shoe_size" in w for w in warnings)
    assert org.notes.startswith("Set up with AI")


def test_clean_suggestion_drops_other_sites_and_bad_regexes(domain: DomainConfig) -> None:
    off_site = OrgSuggestion(
        discovery={"type": "sitemap", "urls": ["https://evil.test/s.xml"], "include": "x"}
    )
    org, warnings = clean_suggestion(off_site, SITE + "/", "Club", domain)
    assert org.discovery is None
    assert org.name == "Club"
    assert warnings

    bad_regex = OrgSuggestion(discovery={"type": "sitemap", "urls": [f"{SITE}/sitemap.xml"], "include": "(["})
    assert clean_suggestion(bad_regex, SITE + "/", "Club", domain)[0].discovery is None

    listing = OrgSuggestion(
        discovery={
            "type": "listing",
            "urls": ["https://www.club.test/squad"],
            "link_selector": "a.player-card",
            "next_selector": None,
        }
    )
    assert isinstance(clean_suggestion(listing, SITE + "/", "Club", domain)[0].discovery, ListingDiscovery)


@respx.mock
async def test_evidence_from_a_site_with_a_people_sitemap(domain: DomainConfig) -> None:
    respx.get(f"{SITE}/robots.txt").respond(200, text=f"User-agent: *\nSitemap: {SITE}/sitemap.xml\n")
    respx.get(f"{SITE}/").respond(200, text='<a href="/players">Players</a>')
    locs = "".join(f"<url><loc>{SITE}/players/p{i}</loc></url>" for i in range(4))
    respx.get(f"{SITE}/sitemap.xml").respond(
        200, text=f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>'
    )
    respx.get(url__regex=rf"{SITE}/players/p\d").respond(200, text=fixture_html("skadden.html"))

    fetcher = Fetcher("TestBot/1.0", min_delay_s=0)
    try:
        evidence = await gather_evidence(fetcher, SITE + "/", domain.fields)
    finally:
        await fetcher.aclose()
    assert evidence.url_groups[0]["count"] == 4
    assert evidence.homepage_links == [{"text": "Players", "url": f"{SITE}/players"}]
    assert evidence.sample_url and evidence.sample_url.startswith(f"{SITE}/players/")
    assert evidence.sample_skeleton
    assert "name" in evidence.sample_extracted


async def test_suggest_org_goes_through_the_gateway(db: Database, domain: DomainConfig) -> None:
    answer: dict[str, Any] = {
        "name": "Club FC",
        "kind": "club",
        "discovery": None,
        "selectors": {},
        "notes": "Too thin.",
    }
    completions = FakeCompletions(content=json.dumps(answer))
    suggestion, cost = await suggest_org(
        gateway(db, completions), Evidence(website=SITE + "/"), domain, ["club"], "a@x"
    )
    assert suggestion.name == "Club FC"
    assert cost > 0
    sent = json.loads(completions.calls[0]["messages"][1]["content"])
    assert sent["evidence"]["website"] == SITE + "/"
    assert "selector_fields" in sent

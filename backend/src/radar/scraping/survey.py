"""Organisation onboarding tool: how can (and may) we read its people's profile pages?

`radar survey weil` checks robots.txt, finds the sitemaps, guesses which URLs are
profiles, and test-extracts one profile to show which fields come out and how. Adding organisation
number 16 starts here.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import httpx

from radar.config import OrgConfig, get_config
from radar.scraping.extract import extract_profile
from radar.scraping.fetcher import Blocked, DisallowedByRobots, Fetcher
from radar.scraping.sitemap import parse_sitemap
from radar.scraping.sources import discover

PROFILE_HINT = re.compile(
    r"^(https?://[^/]+(?:/[a-z]{2})?/(?:people|person|lawyers?|attorneys?|professionals?|our-people|team|bios?|players?|squad|staff))"
    r"/[^/?#]+/?$",
    re.I,
)
PEOPLE_SITEMAP_HINT = re.compile(
    r"people|person|lawyer|attorney|professional|bio|team|player|squad|staff", re.I
)
MAX_SITEMAP_FETCHES = 8


@dataclass
class SurveyResult:
    org_id: str
    robots: str = "unknown"
    sitemaps: list[str] = field(default_factory=list)
    people_sitemaps: list[str] = field(default_factory=list)
    profile_prefix: str | None = None
    profile_count: int = 0
    sample_url: str | None = None
    sample_fields: dict[str, str] = field(default_factory=dict)
    sample_values: dict[str, Any] = field(default_factory=dict)
    configured_count: int | None = None
    verdict: str = ""
    errors: list[str] = field(default_factory=list)

    def suggested_include(self) -> str | None:
        return f"^{re.escape(self.profile_prefix)}/[^/]+$" if self.profile_prefix else None


async def survey_organisation(fetcher: Fetcher, organisation: OrgConfig) -> SurveyResult:
    result = SurveyResult(org_id=organisation.id)
    try:
        parser, blocked = await fetcher.robots(organisation.website)
        if blocked:
            result.robots, result.verdict = f"blocked ({blocked})", "BLOCKED"
            return result
        result.robots = "present" if parser else "none (everything allowed)"
        result.sitemaps = list(parser.site_maps() or []) if parser else []
        if not result.sitemaps:
            result.sitemaps = [urljoin(organisation.website, "/sitemap.xml")]

        if organisation.discovery:
            entries = await discover(fetcher, organisation.discovery)
            result.configured_count = len(entries)
            profile_urls = [entry.url for entry in entries]
        else:
            profile_urls = await _explore_sitemaps(fetcher, result)

        prefixes = Counter(m.group(1) for url in profile_urls if (m := PROFILE_HINT.match(url)))
        if prefixes:
            result.profile_prefix, result.profile_count = prefixes.most_common(1)[0]
        elif profile_urls and organisation.discovery:
            result.profile_count = len(profile_urls)
        sample = next(
            (u for u in profile_urls if PROFILE_HINT.match(u)), profile_urls[0] if profile_urls else None
        )
        if sample:
            await _sample_profile(fetcher, organisation, sample, result)
    except Blocked as exc:
        result.verdict = "BLOCKED"
        result.errors.append(str(exc))
    except DisallowedByRobots as exc:
        result.verdict = "DISALLOWED"
        result.errors.append(f"robots.txt disallows {exc}")
    except (httpx.HTTPError, ValueError) as exc:
        result.errors.append(repr(exc))

    if not result.verdict:
        result.verdict = _verdict(result)
    return result


async def _explore_sitemaps(fetcher: Fetcher, result: SurveyResult) -> list[str]:
    queue = list(result.sitemaps)
    urls: list[str] = []
    fetched = 0
    while queue and fetched < MAX_SITEMAP_FETCHES:
        sitemap_url = queue.pop(0)
        fetched += 1
        response = await fetcher.get(sitemap_url)
        if response.status != 200:
            result.errors.append(f"{sitemap_url}: HTTP {response.status}")
            continue
        try:
            parsed = parse_sitemap(response.content)
        except Exception as exc:  # malformed XML is a finding, not a crash
            result.errors.append(f"{sitemap_url}: not a sitemap ({type(exc).__name__})")
            continue
        people_children = [child for child in parsed.children if PEOPLE_SITEMAP_HINT.search(child)]
        result.people_sitemaps.extend(people_children)
        queue = people_children + queue
        urls.extend(entry.url for entry in parsed.entries)
    return urls


async def _sample_profile(fetcher: Fetcher, organisation: OrgConfig, url: str, result: SurveyResult) -> None:
    result.sample_url = url
    response = await fetcher.get(url)
    if response.status != 200:
        result.errors.append(f"sample profile: HTTP {response.status}")
        return
    record = extract_profile(response.text, url, organisation.selectors, get_config().domain.fields)
    result.sample_fields = record.methods
    result.sample_values = {
        key: value
        for key, value in record.model_dump(exclude={"methods", "url", "bio", "email", "phone"}).items()
        if value
    }


def _verdict(result: SurveyResult) -> str:
    fields = result.sample_fields
    if not result.profile_count:
        return "NEEDS CONFIG: no profile URLs found in sitemaps"
    if fields.get("title") and fields.get("groups"):
        return "READY: structured enough for the generic pipeline"
    if fields.get("name"):
        return "PARTIAL: profiles found; add selectors for missing fields"
    return "NEEDS CONFIG: profiles found but extraction failed (JS-rendered?)"

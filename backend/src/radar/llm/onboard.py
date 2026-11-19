"""AI "set up this organisation": turn evidence about a website into scraper settings.

Two small LLM calls, both through `LlmGateway` (so they're cached, budgeted and logged):

1. `resolve_org` - only when the admin typed a name, not a URL: "Slaughter and May" -> its
   official homepage. The page is then fetched, so a wrong guess fails loudly instead of quietly.
2. `suggest_org` - given the `Evidence` from radar/scraping/onboard.py (sitemaps, links, a sample
   profile skeleton), propose discovery settings and CSS selectors.

The model's answer is never trusted as-is: `clean_suggestion` drops URLs on other sites, regexes
that don't compile, selectors that don't parse and fields we don't extract. The admin then sees
the normal survey of the suggestion before deciding to save it.
"""

import re
from typing import Any

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, field_validator
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from radar.api.schemas import OrgIn
from radar.config import AnyDiscovery, DomainConfig
from radar.llm.gateway import LlmGateway
from radar.scraping.onboard import Evidence, homepage, same_site

TASK = "org_setup"
RESOLVE_PROMPT_VERSION = "org-resolve-v1"
SETUP_PROMPT_VERSION = "org-setup-v2"

# Fields extraction can take a selector for, besides the domain's extra fields (see extract.py).
CORE_SELECTOR_FIELDS = ("name", "title", "offices", "groups", "education")

RESOLVE_SYSTEM = (
    "You identify an organisation's official website. You receive JSON "
    '{"query": "<what the user typed>", "tracking": "<who we track there>"}. Return a JSON object '
    '{"name": "<the organisation\'s usual name>", "website": "https://<official homepage>/"}. '
    'If you are not confident which organisation is meant, return {"name": "", "website": ""}. '
    "Never invent a domain."
)

SETUP_SYSTEM = """You configure a web scraper that tracks people (for example lawyers at a law firm, or players at a football club) on an organisation's own website.

You receive JSON with:
- "tracking": who we track and what the site calls them.
- "evidence": what we found on the site: robots status, sitemap URLs, child sitemaps, sitemap URLs grouped by path prefix with counts and examples, people-looking links on the homepage, maybe a listing page's links (with their <a> class and parent element), and a sample profile page as an indented outline of `tag.class#id: text`, plus which fields our generic extractor already read from it ("sample_extracted", field -> method).
- "selector_fields": the field names you may give CSS selectors for, with what each means.
- "known_kinds": kinds already in use, e.g. law_firm. Reuse one if it fits.

Return ONE JSON object:
{
  "name": "<organisation name>",
  "kind": "<short snake_case kind, e.g. law_firm, club>",
  "discovery": <one of the two shapes below, or null if the evidence shows no way to list profiles>,
  "selectors": {"<field>": "<CSS selector>"},
  "notes": "<one or two plain sentences for the admin: what you chose and why, and anything doubtful>"
}

Discovery shapes:
- Sitemap (prefer this whenever a sitemap contains profile URLs):
  {"type": "sitemap", "urls": ["<sitemap or sitemap index URL from the evidence>"], "include": "<regex every profile URL matches>", "exclude": "<regex or null>", "child_include": "<regex for which child sitemaps of an index to read, or null>"}
- Listing page (only when no sitemap lists profiles):
  {"type": "listing", "urls": ["<listing page URL>"], "link_selector": "<CSS selector of the <a> elements linking to profiles>", "next_selector": "<CSS selector of the next-page link, or null>", "include": "<regex profile URLs match, or empty string>", "exclude": null}

Rules:
- Use only URLs that appear in the evidence. Do not invent paths.
- "include" is a Python regex. Anchor it, e.g. "^https://www\\\\.example\\\\.com/people/[^/]+/?$". It must match person profiles only, not the listing page, search pages or news. URLs are checked with any trailing "/" removed, so never require one.
- Only give a selector for a field that is missing from "sample_extracted" and that you can see in the sample outline. Use the classes shown. Leave "selectors" empty rather than guess.
- If the evidence is too thin to decide, set "discovery" to null and explain in "notes".
"""


def _blank_if_null(value: Any) -> Any:
    return "" if value is None else value


class ResolvedOrg(BaseModel):
    name: str = ""
    website: str = ""

    blank_nulls = field_validator("name", "website", mode="before")(_blank_if_null)


class OrgSuggestion(BaseModel):
    """The model's raw answer. `discovery` stays a dict here so one bad field can be dropped
    without throwing away the whole answer (and paying for a retry)."""

    name: str = ""
    kind: str = ""
    discovery: dict[str, Any] | None = None
    selectors: dict[str, str] = Field(default_factory=dict)
    notes: str = ""

    # Models often write null for "nothing"; that shouldn't throw the whole answer away.
    blank_nulls = field_validator("name", "kind", "notes", mode="before")(_blank_if_null)

    @field_validator("selectors", mode="before")
    @classmethod
    def _drop_null_selectors(cls, value: Any) -> Any:
        if value is None:
            return {}
        if isinstance(value, dict):
            return {key: item for key, item in value.items() if isinstance(item, str)}
        return value


async def resolve_org(
    gateway: LlmGateway, query: str, domain: DomainConfig, user: str
) -> tuple[ResolvedOrg, float]:
    """(the organisation the model thinks is meant, what it cost in USD)."""
    result = await gateway.run(
        TASK,
        RESOLVE_PROMPT_VERSION,
        RESOLVE_SYSTEM,
        {"query": query, "tracking": _tracking(domain)},
        ResolvedOrg,
        user=user,
    )
    return result.output, result.cost_usd


async def suggest_org(
    gateway: LlmGateway,
    evidence: Evidence,
    domain: DomainConfig,
    known_kinds: list[str],
    user: str,
) -> tuple[OrgSuggestion, float]:
    """(the model's suggestion, what it cost in USD)."""
    payload = {
        "tracking": _tracking(domain),
        "evidence": evidence.to_payload(),
        "selector_fields": selector_fields(domain),
        "known_kinds": known_kinds,
    }
    result = await gateway.run(TASK, SETUP_PROMPT_VERSION, SETUP_SYSTEM, payload, OrgSuggestion, user=user)
    return result.output, result.cost_usd


def clean_suggestion(
    suggestion: OrgSuggestion, website: str, fallback_name: str, domain: DomainConfig
) -> tuple[OrgIn, list[str]]:
    """The suggestion as a form the admin can review, plus what was dropped and why."""
    warnings: list[str] = []
    discovery = _clean_discovery(suggestion.discovery, website, warnings)
    allowed = set(selector_fields(domain))
    selectors: dict[str, str] = {}
    for field, selector in suggestion.selectors.items():
        selector = selector.strip()
        if not selector:
            continue
        if field not in allowed:
            warnings.append(f"Ignored a selector for unknown field '{field}'.")
        elif not _selector_parses(selector):
            warnings.append(f"Ignored an invalid CSS selector for {field}: {selector}")
        else:
            selectors[field] = selector
    kind = re.sub(r"[^a-z0-9_]+", "_", suggestion.kind.lower()).strip("_") or "organisation"
    org = OrgIn(
        name=suggestion.name.strip() or fallback_name,
        website=homepage(website),
        kind=kind,
        discovery=discovery,
        selectors=selectors,
        notes=(
            f"Set up with AI: {suggestion.notes.strip()}" if suggestion.notes.strip() else "Set up with AI."
        ),
    )
    return org, warnings


def _clean_discovery(raw: dict[str, Any] | None, website: str, warnings: list[str]) -> Any:
    if not raw:
        return None
    raw = {key: value for key, value in raw.items() if value is not None}
    urls = [url for url in raw.get("urls") or [] if isinstance(url, str)]
    kept = [url for url in urls if same_site(url, website)]
    if len(kept) < len(urls):
        warnings.append("Dropped discovery URLs that aren't on the organisation's own site.")
    if not kept:
        warnings.append("The AI gave no usable discovery URL, so discovery is left blank.")
        return None
    raw["urls"] = kept
    for key in ("include", "exclude", "child_include"):
        pattern = raw.get(key)
        if isinstance(pattern, str):
            try:
                re.compile(pattern)
            except re.error:
                warnings.append(f"The AI's {key} regex doesn't compile, so discovery is left blank.")
                return None
    for key in ("link_selector", "next_selector"):
        selector = raw.get(key)
        if isinstance(selector, str) and selector and not _selector_parses(selector):
            warnings.append(f"The AI's {key} isn't valid CSS, so discovery is left blank.")
            return None
    try:
        return TypeAdapter(AnyDiscovery).validate_python(raw)
    except ValidationError as exc:
        warnings.append(f"The AI's discovery settings were incomplete ({exc.error_count()} problems).")
        return None


def selector_fields(domain: DomainConfig) -> dict[str, str]:
    words = domain.vocabulary
    fields = {
        "name": "the person's full name",
        "title": "job title / rank / role",
        "offices": f"{words.locations.lower()} (list)",
        "groups": f"{words.groups.lower()} (list)",
        "education": "degrees and schools (list)",
    }
    return fields | {field.key: field.label for field in domain.fields}


def _tracking(domain: DomainConfig) -> str:
    words = domain.vocabulary
    return f"{words.people} at {words.orgs.lower()}. {domain.brief_context}"


def _selector_parses(selector: str) -> bool:
    try:
        HTMLParser("<p></p>").css(selector)
    except Exception:  # selectolax raises its own errors for bad CSS
        return False
    return True

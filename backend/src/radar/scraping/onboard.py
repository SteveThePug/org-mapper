"""Evidence for the AI "set up this organisation" helper: what does the site actually look like?

An LLM asked "what's the sitemap and profile regex for example.com?" from memory will guess, and
guesses are often wrong or out of date. So we look first, the same way `survey.py` does, and send
the model a short summary of what is really there:

- the sitemaps, and the URLs in them grouped by path ("https://x.com/people/... x 812"),
- links on the homepage that look like they lead to people ("Our people", "Squad"),
- if there's no usable sitemap, the links on the most likely listing page,
- one sample profile page, cut down to its tag/class skeleton, plus what the generic
  extraction already reads from it (so the model only suggests selectors for what's missing).

The model then turns that into discovery settings (radar/llm/onboard.py), and the admin checks
the result with the normal survey before saving. Nothing here calls the LLM.
"""

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from radar.config import ExtraField
from radar.scraping.extract import extract_profile
from radar.scraping.fetcher import Blocked, DisallowedByRobots, Fetcher
from radar.scraping.sitemap import parse_sitemap
from radar.scraping.survey import PEOPLE_SITEMAP_HINT

MAX_SITEMAP_FETCHES = 8
MAX_CHILDREN_SHOWN = 40
MAX_URL_GROUPS = 15
EXAMPLES_PER_GROUP = 3
MAX_LINKS = 40
SKELETON_CHARS = 6000
TEXT_PER_NODE = 80
MAX_DEPTH = 14

# Words in a link's text that suggest it leads to a list of people.
PEOPLE_LINK_TEXT = re.compile(
    r"people|our team|team|lawyers?|attorneys?|professionals?|partners|staff|players?|squad|first team|who we are",
    re.I,
)
_SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "head", "link", "meta"}
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
_DOMAIN_LIKE = re.compile(r"^(https?://)?([a-z0-9-]+\.)+[a-z]{2,}(/.*)?$", re.I)


class OnboardingError(Exception):
    """We can't (or may not) read this site. The message is shown to the admin."""


@dataclass
class Evidence:
    website: str
    robots: str = "unknown"
    sitemaps: list[str] = field(default_factory=list)
    child_sitemaps: list[str] = field(default_factory=list)
    url_groups: list[dict[str, Any]] = field(default_factory=list)
    homepage_links: list[dict[str, str]] = field(default_factory=list)
    listing_page: dict[str, Any] | None = None
    sample_url: str | None = None
    sample_skeleton: str = ""
    sample_extracted: dict[str, str] = field(default_factory=dict)  # field -> extraction method
    errors: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def looks_like_url(query: str) -> bool:
    """'example.com', 'https://www.example.com/en' -> True; 'Slaughter and May' -> False."""
    return " " not in query.strip() and bool(_DOMAIN_LIKE.match(query.strip()))


def homepage(url: str) -> str:
    """Any URL or bare domain -> 'https://host/'. Raises OnboardingError if it isn't one."""
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:  # e.g. an unclosed "[" IPv6 bracket
        host = ""
    if not _DOMAIN_LIKE.match(host):
        raise OnboardingError(f"'{url}' isn't a website address.")
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}/"


def same_site(url: str, website: str) -> bool:
    """Same host, ignoring a leading 'www.'."""

    def host(u: str) -> str:
        return urlsplit(u).netloc.lower().removeprefix("www.")

    return host(url) == host(website)


async def gather_evidence(fetcher: Fetcher, website: str, fields: list[ExtraField]) -> Evidence:
    """Raises OnboardingError if the site blocks us; everything else becomes `errors`."""
    parser, blocked = await fetcher.robots(website)
    if blocked and blocked.startswith("robots.txt unreachable"):
        raise OnboardingError(f"Couldn't reach {website}; check the address. ({blocked})")
    if blocked:
        raise OnboardingError(f"{website} can't be read ({blocked}), so it can't be tracked.")
    evidence = Evidence(website=website, robots="present" if parser else "none (everything allowed)")

    try:
        home = await fetcher.get(website)
    except (Blocked, DisallowedByRobots, httpx.HTTPError) as exc:
        raise OnboardingError(f"Couldn't read {website}: {exc or type(exc).__name__}") from exc
    if home.status != 200:
        raise OnboardingError(f"{website} answered HTTP {home.status}; check the address.")
    evidence.website = website = homepage(home.final_url)  # follow redirects to the real host
    evidence.homepage_links = people_links(home.text, home.final_url, website)

    evidence.sitemaps = list(parser.site_maps() or []) if parser else []
    if not evidence.sitemaps:
        evidence.sitemaps = [urljoin(website, "/sitemap.xml")]
    urls = await _read_sitemaps(fetcher, evidence)
    evidence.url_groups = group_by_prefix(urls)

    try:
        sample = _sample_from_groups(evidence.url_groups)
        if sample is None and evidence.homepage_links:
            sample = await _read_listing(fetcher, evidence)
        if sample:
            await _read_sample(fetcher, sample, fields, evidence)
    except (Blocked, DisallowedByRobots, httpx.HTTPError) as exc:
        evidence.errors.append(f"{type(exc).__name__}: {exc}")  # the model still gets the sitemaps
    return evidence


async def _read_sitemaps(fetcher: Fetcher, evidence: Evidence) -> list[str]:
    """Entry URLs from the sitemaps. In an index, people-looking children are read first; if none
    look like people, the first few are read so the model still sees what's there."""
    queue = list(evidence.sitemaps)
    urls: list[str] = []
    for _ in range(MAX_SITEMAP_FETCHES):
        if not queue:
            break
        sitemap_url = queue.pop(0)
        try:
            response = await fetcher.get(sitemap_url)
        except (Blocked, DisallowedByRobots, httpx.HTTPError) as exc:  # try the others
            evidence.errors.append(f"{sitemap_url}: {exc}")
            continue
        if response.status != 200:
            evidence.errors.append(f"{sitemap_url}: HTTP {response.status}")
            continue
        try:
            parsed = parse_sitemap(response.content)
        except ValueError:
            evidence.errors.append(f"{sitemap_url}: not a sitemap")
            continue
        evidence.child_sitemaps.extend(parsed.children[: MAX_CHILDREN_SHOWN - len(evidence.child_sitemaps)])
        people = [child for child in parsed.children if PEOPLE_SITEMAP_HINT.search(child)]
        queue = people + queue if people else queue + parsed.children[:3]
        urls.extend(entry.url for entry in parsed.entries)
    return urls


def group_by_prefix(urls: list[str]) -> list[dict[str, Any]]:
    """Group URLs by everything before their last path segment, biggest groups first.

    'https://x.com/people/jane-doe' and '.../people/john-roe' -> {"prefix": "https://x.com/people",
    "count": 2, "examples": [...]}. This lets the model see 800 profile URLs as one line.
    """
    groups: dict[str, list[str]] = {}
    for url in urls:
        prefix = url.rstrip("/").rsplit("/", 1)[0]
        groups.setdefault(prefix, []).append(url)
    ranked = sorted(groups.items(), key=lambda item: -len(item[1]))[:MAX_URL_GROUPS]
    return [
        {"prefix": prefix, "count": len(members), "examples": members[:EXAMPLES_PER_GROUP]}
        for prefix, members in ranked
    ]


def _sample_from_groups(groups: list[dict[str, Any]]) -> str | None:
    """A URL from the biggest group whose path sounds like people (/people/, /players/...)."""
    for group in groups:
        if group["count"] >= 3 and PEOPLE_SITEMAP_HINT.search(urlsplit(group["prefix"]).path):
            examples: list[str] = group["examples"]
            return examples[-1]
    return None


def people_links(html: str, page_url: str, website: str) -> list[dict[str, str]]:
    """Same-site links whose text or URL suggests a list of people, e.g. 'Our people'."""
    found: dict[str, str] = {}
    for node in HTMLParser(html).css("a[href]"):
        url = urljoin(page_url, node.attributes.get("href") or "").split("#")[0]
        text = " ".join(node.text().split())[:TEXT_PER_NODE]
        if not same_site(url, website) or url in found:
            continue
        if PEOPLE_LINK_TEXT.search(text) or PEOPLE_SITEMAP_HINT.search(urlsplit(url).path):
            found[url] = text
    return [{"text": text, "url": url} for url, text in list(found.items())[: MAX_LINKS // 2]]


async def _read_listing(fetcher: Fetcher, evidence: Evidence) -> str | None:
    """No usable sitemap: read the most likely 'people' page and return a probable profile URL."""
    page_url = evidence.homepage_links[0]["url"]
    response = await fetcher.get(page_url)
    if response.status != 200:
        evidence.errors.append(f"{page_url}: HTTP {response.status}")
        return None
    links = listing_links(response.text, response.final_url, evidence.website)
    evidence.listing_page = {"url": page_url, "links": links}
    # Profiles usually sit one level below the listing (/people -> /people/jane-doe).
    base = response.final_url.rstrip("/") + "/"
    below = [link["url"] for link in links if link["url"].startswith(base)]
    return below[len(below) // 2] if below else None


def listing_links(html: str, page_url: str, website: str) -> list[dict[str, str]]:
    """The page's same-site links with the classes around them, so the model can pick a selector."""
    tree = HTMLParser(html)
    for node in tree.css("header, nav, footer"):
        node.decompose()
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for node in tree.css("a[href]"):
        url = urljoin(page_url, node.attributes.get("href") or "").split("#")[0]
        if not same_site(url, website) or url in seen or url.rstrip("/") == page_url.rstrip("/"):
            continue
        seen.add(url)
        parent = node.parent
        links.append(
            {
                "text": " ".join(node.text().split())[:TEXT_PER_NODE],
                "url": url,
                "a_class": node.attributes.get("class") or "",
                "parent": _label(parent) if parent is not None else "",
            }
        )
        if len(links) >= MAX_LINKS:
            break
    return links


async def _read_sample(fetcher: Fetcher, url: str, fields: list[ExtraField], evidence: Evidence) -> None:
    evidence.sample_url = url
    response = await fetcher.get(url)
    if response.status != 200:
        evidence.errors.append(f"sample profile {url}: HTTP {response.status}")
        return
    evidence.sample_skeleton = page_skeleton(response.text)
    evidence.sample_extracted = extract_profile(response.text, url, {}, fields).methods


def page_skeleton(html: str, limit: int = SKELETON_CHARS) -> str:
    """The page as an indented outline: `tag.class#id: start of its own text`.

    Elements with no class, id or text of their own are left out (their children still show), so
    a whole profile page fits in a few thousand characters but the selectors are still visible.
    """
    tree = HTMLParser(html)
    for node in tree.css("header, nav, footer, form, aside, [role=navigation], [aria-hidden=true]"):
        node.decompose()  # menus repeat on every page; the profile is what matters
    body = tree.body
    if body is None:
        return ""
    lines: list[str] = []
    size = 0

    def walk(node: Node, depth: int) -> None:
        nonlocal size
        for child in node.iter(include_text=False):
            if size >= limit:
                return
            if child.tag in _SKIP_TAGS:
                continue
            label = _label(child)
            own_text = redact(" ".join((child.text(deep=False) or "").split())[:TEXT_PER_NODE])
            shown = label != child.tag or own_text or child.tag in ("h1", "h2", "h3", "h4", "ul", "dl")
            if shown:
                line = "  " * min(depth, MAX_DEPTH) + label + (f": {own_text}" if own_text else "")
                lines.append(line)
                size += len(line) + 1
            walk(child, depth + 1 if shown else depth)

    walk(body, 0)
    return "\n".join(lines)[:limit]


def redact(text: str) -> str:
    """Contact details aren't needed to choose a selector, so they never reach the model."""
    return _PHONE.sub("[phone]", _EMAIL.sub("[email]", text))


def _label(node: Node) -> str:
    """'div.bio.card#main' style label for an element."""
    label = node.tag or ""
    node_id = node.attributes.get("id")
    classes = (node.attributes.get("class") or "").split()[:3]
    if classes:
        label += "." + ".".join(classes)
    if node_id:
        label += f"#{node_id}"
    return label


def most_common_kinds(kinds: list[str]) -> list[str]:
    """Existing organisation kinds, most used first, so the model reuses them (law_firm, club)."""
    return [kind for kind, _ in Counter(k for k in kinds if k).most_common(8)]

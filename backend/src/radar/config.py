"""Loads the "what do we track" configuration: organisations, and the domain profile.

The domain profile (`config/domains/<RADAR_DOMAIN>.yaml`) says who we track and what we call
them: canonical titles, the group taxonomy (practice areas for lawyers, positions for
footballers), segments, extra profile fields and the product's vocabulary. Retargeting the
product means a new profile, not new code: a segment is "which organisations, which titles,
which groups".
"""

import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import AliasChoices, BaseModel, Discriminator, Field, Tag, model_validator

from radar.settings import get_settings


class Discovery(BaseModel):
    """Where to find the list of profile URLs for an organisation: its XML sitemaps."""

    type: Literal["sitemap"] = "sitemap"
    urls: list[str]
    # Regex a <loc> must match to count as a profile page.
    include: str
    exclude: str | None = None
    # In a sitemap index, only follow child sitemaps whose URL matches this regex.
    child_include: str | None = None

    def matches(self, url: str) -> bool:
        if not re.search(self.include, url):
            return False
        return not (self.exclude and re.search(self.exclude, url))


class ListingDiscovery(BaseModel):
    """Profile URLs linked from ordinary HTML pages: a "Our people" or squad page, with paging.

    For sites without a usable sitemap. `link_selector` picks the links to profiles.
    """

    type: Literal["listing"] = "listing"
    urls: list[str]
    link_selector: str
    # Selector of the "next page" link, if the listing is paginated.
    next_selector: str | None = None
    include: str = ""  # optional extra filter on the profile URLs ("" = every link)
    exclude: str | None = None

    def matches(self, url: str) -> bool:
        if self.include and not re.search(self.include, url):
            return False
        return not (self.exclude and re.search(self.exclude, url))


def _discovery_type(value: Any) -> str:
    """Older configs have no `type`: they are all sitemaps."""
    kind = value.get("type") if isinstance(value, dict) else getattr(value, "type", None)
    return kind or "sitemap"


# Every way we know to discover profile URLs. A new source type (an API, a social-media
# account...) is one more model here plus one function in radar/scraping/sources.py.
AnyDiscovery = Annotated[
    Annotated[Discovery, Tag("sitemap")] | Annotated[ListingDiscovery, Tag("listing")],
    Discriminator(_discovery_type),
]


class OrgConfig(BaseModel):
    """One tracked organisation: a law firm, a bank, a consultancy - anything with people pages.

    `organisations.yaml` only seeds these; the live copy is the `organisations` collection, edited from
    the admin panel (see radar/orgs.py).
    """

    id: str
    name: str
    website: str
    # Free text ("law_firm", "bank", "consultancy"...): shown as a badge and used as a filter.
    kind: str = "law_firm"
    discovery: AnyDiscovery | None = None
    # Optional CSS selectors per field, used when a site has no structured data.
    selectors: dict[str, str] = Field(default_factory=dict)
    min_delay_s: float | None = None
    enabled: bool = True
    notes: str = ""


class GroupTag(BaseModel):
    """A canonical group (a practice area, a position...), recognised by keywords in a site's own label."""

    label: str
    include: list[str]
    exclude: list[str] = Field(default_factory=list)

    def matches(self, raw_label: str) -> bool:
        text = raw_label.lower()
        if not any(re.search(pattern, text) for pattern in self.include):
            return False
        return not any(re.search(pattern, text) for pattern in self.exclude)


class Segment(BaseModel):
    """A target population, e.g. "Corporate & M&A partners at these organisations"."""

    id: str
    name: str
    # "firms" is the old name: profiles saved in Mongo before the rename still use it.
    organisations: list[str] | Literal["all"] = Field(
        "all", validation_alias=AliasChoices("organisations", "firms")
    )
    titles: list[str]
    # Canonical group tags; "all" = any group, or none (for sites that don't label groups).
    groups: list[str] | Literal["all"]

    def contains(self, org_id: str, title_norm: str | None, groups: list[str]) -> bool:
        if self.organisations != "all" and org_id not in self.organisations:
            return False
        if title_norm not in self.titles:
            return False
        return self.groups == "all" or any(tag in self.groups for tag in groups)


class Vocabulary(BaseModel):
    """The words the product uses for its people and organisations (capitalised)."""

    app_name: str = "Radar"
    tagline: str = ""
    person: str = "Person"
    people: str = "People"
    org: str = "Organisation"
    orgs: str = "Organisations"
    group: str = "Group"
    groups: str = "Groups"
    location: str = "Location"
    locations: str = "Locations"


class ExtraField(BaseModel):
    """A domain-specific profile field (bar admissions, nationality...), stored as a list of strings."""

    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    # Section headings on a profile page that introduce this field (lower-case prefixes).
    headings: list[str] = Field(default_factory=list)
    # The schema.org Person property holding the same information, if any.
    jsonld: str | None = None


class Seniority(BaseModel):
    """Seniority proxy: the year of the earliest degree of one of these kinds."""

    label: str
    degrees: list[str]  # lower-case, punctuation removed: "jd", "llb"


class DomainConfig(BaseModel):
    """Who we track and the words we use for them (one `config/domains/*.yaml` file)."""

    vocabulary: Vocabulary = Field(default_factory=Vocabulary)
    brief_context: str = "You prepare a recruiter for a first phone call with this person."
    digest_context: str = "You write a short weekly digest of people moves for recruiters."
    # Canonical title -> keywords, checked in order (first match wins).
    titles: dict[str, list[str]]
    # A change *into* one of these canonical titles is a promotion.
    promotion_to: list[str] = Field(default_factory=list)
    taxonomy: dict[str, GroupTag]
    fields: list[ExtraField] = Field(default_factory=list)
    seniority: Seniority | None = None
    segments: list[Segment]
    default_segment: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "DomainConfig":
        """Catch typos early: everything a segment refers to must exist, and regexes must compile."""
        for tag, rule in self.taxonomy.items():
            for pattern in [*rule.include, *rule.exclude]:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"taxonomy.{tag}: {pattern!r} is not a valid regex: {exc}") from exc
        segment_ids = [s.id for s in self.segments]
        for segment in self.segments:
            unknown = [t for t in segment.titles if t not in self.titles]
            if segment.groups != "all":
                unknown += [g for g in segment.groups if g not in self.taxonomy]
            if unknown:
                raise ValueError(
                    f"segment {segment.id!r} refers to unknown titles/groups: {', '.join(unknown)}"
                )
        unknown_promotions = [t for t in self.promotion_to if t not in self.titles]
        if unknown_promotions:
            raise ValueError(f"promotion_to refers to unknown titles: {', '.join(unknown_promotions)}")
        if self.default_segment and self.default_segment not in segment_ids:
            raise ValueError(f"default_segment {self.default_segment!r} is not one of the segments")
        if len({f.key for f in self.fields}) != len(self.fields):
            raise ValueError("fields: each key must be unique")
        return self

    @property
    def default_segment_id(self) -> str | None:
        return self.default_segment or (self.segments[0].id if self.segments else None)

    def segment_name(self, segment_ids: list[str]) -> str:
        """Name of the first segment in `segment_ids`, else the plural noun ("Partners")."""
        for segment in self.segments:
            if segment.id in segment_ids:
                return segment.name
        return self.vocabulary.people


class AppConfig(BaseModel):
    # Organisations; the attribute keeps its original name. Refreshed from Mongo by radar.orgs.
    organisations: list[OrgConfig]
    # Refreshed from Mongo by radar.domain, since admins can edit it.
    domain: DomainConfig
    domain_name: str = "legal-partners"  # which config/domains/*.yaml it was seeded from

    def organisation(self, org_id: str) -> OrgConfig:
        for organisation in self.organisations:
            if organisation.id == org_id:
                return organisation
        raise KeyError(
            f"Unknown organisation '{org_id}'. Known: {', '.join(f.id for f in self.organisations)}"
        )


def load_domain(config_dir: Path, name: str) -> DomainConfig:
    path = config_dir / "domains" / f"{name}.yaml"
    if not path.exists():
        known = ", ".join(sorted(p.stem for p in (config_dir / "domains").glob("*.yaml")))
        raise FileNotFoundError(f"No domain profile {path}. Set RADAR_DOMAIN to one of: {known}")
    return DomainConfig.model_validate(yaml.safe_load(path.read_text()))


def load_config(config_dir: Path, domain: str = "legal-partners") -> AppConfig:
    organisations = yaml.safe_load((config_dir / "organisations.yaml").read_text())
    return AppConfig(
        organisations=organisations["organisations"],
        domain=load_domain(config_dir, domain),
        domain_name=domain,
    )


@lru_cache
def get_config() -> AppConfig:
    settings = get_settings()
    return load_config(settings.config_dir, settings.radar_domain)

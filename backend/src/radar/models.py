"""Domain models. These are also the MongoDB document shapes (`_id` aliased to `id`)."""

import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Doc(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    def to_mongo(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)


class ProfileRecord(BaseModel):
    """What the extractor read from one profile page, before normalisation."""

    url: str
    name: str | None = None
    title: str | None = None
    offices: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)  # the site's own labels: practice areas, positions...
    education: list[str] = Field(default_factory=list)
    # The domain profile's extra fields (bar admissions, nationality...), by key.
    extra: dict[str, list[str]] = Field(default_factory=dict)
    email: str | None = None
    phone: str | None = None
    bio: str | None = None
    methods: dict[str, str] = Field(default_factory=dict)  # field -> extraction method

    def content_hash(self) -> str:
        """Hash of the extracted facts. Unchanged hash => nothing to store, diff or summarise."""
        payload = self.model_dump_json(exclude={"methods"})
        return hashlib.sha256(payload.encode()).hexdigest()


class Education(BaseModel):
    raw: str
    degree: str | None = None
    school: str | None = None
    year: int | None = None


class Provenance(BaseModel):
    method: str
    source_url: str
    observed_at: datetime


class RoleSnapshot(BaseModel):
    """A previous position, kept on the person when a lateral move is linked."""

    org_id: str
    title: str | None
    offices: list[str]
    groups: list[str] = Field(default_factory=list)
    profile_url: str
    first_seen: datetime
    last_seen: datetime | None


PersonStatus = Literal["pending", "active", "departed"]


def person_id_for(profile_url: str) -> str:
    """Stable id per bio URL, so discovery and fetches can upsert idempotently."""
    return hashlib.sha256(profile_url.rstrip("/").encode()).hexdigest()[:20]


class Person(Doc):
    id: str = Field(alias="_id")
    org_id: str
    profile_url: str
    status: PersonStatus = "pending"
    # True when first seen in an organisation's very first crawl: we can't know when they joined.
    baseline: bool = True

    name: str | None = None
    title: str | None = None
    title_norm: str | None = None
    offices: list[str] = Field(default_factory=list)
    groups_raw: list[str] = Field(default_factory=list)  # the site's labels
    groups: list[str] = Field(default_factory=list)  # canonical tags from the domain taxonomy
    segments: list[str] = Field(default_factory=list)  # segment ids this person is in

    # Detail kept only while the person is in a target segment (data minimisation).
    education: list[Education] = Field(default_factory=list)
    extra: dict[str, list[str]] = Field(default_factory=dict)
    seniority_year: int | None = None
    email: str | None = None
    phone: str | None = None
    bio: str | None = None
    provenance: dict[str, Provenance] = Field(default_factory=dict)

    history: list[RoleSnapshot] = Field(default_factory=list)
    moved_from: str | None = None
    moved_to: str | None = None

    # Crawl bookkeeping.
    first_seen: datetime
    last_seen_in_sitemap: datetime | None = None
    sitemap_lastmod: str | None = None
    missing_runs: int = 0
    last_fetched: datetime | None = None
    last_changed: datetime | None = None
    departed_at: datetime | None = None
    record_hash: str | None = None
    etag: str | None = None
    last_modified: str | None = None

    @property
    def in_scope(self) -> bool:
        return bool(self.segments)


EventType = Literal[
    "joined",
    "left",
    "promoted",
    "title_changed",
    "office_changed",
    "group_changed",
    "moved",
    "possible_move",
    "lift_out",
]


class Event(Doc):
    id: str = Field(alias="_id")
    type: EventType
    person_id: str
    person_name: str
    org_id: str
    detected_at: datetime
    summary: str
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    segments: list[str] = Field(default_factory=list)
    offices: list[str] = Field(default_factory=list)
    related_person_id: str | None = None
    related_org_id: str | None = None
    confidence: float | None = None
    status: Literal["auto", "confirmed", "dismissed"] = "auto"
    evidence_url: str | None = None
    simulated: bool = False
    # "crawl" = seen happen by our own crawls; "archive" = reconstructed from Internet Archive snapshots.
    source: Literal["crawl", "archive"] = "crawl"


class Observation(Doc):
    """Immutable record of one fetch whose content changed. Lets us audit and re-parse."""

    id: str = Field(alias="_id")
    person_id: str
    org_id: str
    url: str
    fetched_at: datetime
    http_status: int
    record_hash: str
    raw_sha256: str | None
    parser_version: str
    record: dict[str, Any]


class LlmCall(Doc):
    """One row of the AI cost ledger: every call *and* every cache hit."""

    id: str = Field(alias="_id")
    ts: datetime
    task: str
    model: str
    prompt_version: str
    input_hash: str
    subject_id: str | None
    user: str | None
    cache_hit: bool
    status: Literal["ok", "error", "disabled", "over_budget"]
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    error: str | None = None


class Note(Doc):
    """Something a user learned about a person that their public page doesn't say (a call, a tip...).

    Notes live in their own collection, not on the person document, so the crawler, which
    rewrites people from their pages, can never overwrite them.
    """

    id: str = Field(alias="_id")
    person_id: str
    text: str
    author: str  # the user's email
    created_at: datetime
    updated_at: datetime | None = None
    # Off for anything the author doesn't want sent to the AI model (it is still shown to colleagues).
    use_in_brief: bool = True

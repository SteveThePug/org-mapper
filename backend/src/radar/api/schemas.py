"""Response/request shapes for the HTTP API (these also generate the frontend's TypeScript types)."""

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from radar.config import AnyDiscovery, DomainConfig, ExtraField, Seniority, Vocabulary
from radar.llm.gateway import FixedCost, TaskSettings
from radar.llm.tasks import Brief, Digest, Fact
from radar.models import Education, EventType, Provenance
from radar.signals import Signal


class DomainOut(BaseModel):
    """The domain profile as stored, for the admin Tracking page."""

    name: str
    profile: DomainConfig
    managed: bool  # true once an admin has edited it (the YAML seed no longer applies)
    updated_at: datetime | None = None
    updated_by: str | None = None


class Me(BaseModel):
    email: str
    role: Literal["recruiter", "admin"]


class LoginIn(BaseModel):
    # Capped so a multi-megabyte "password" can't tie up the CPU in Argon2.
    email: str = Field(max_length=256)
    password: str = Field(max_length=256)


class Option(BaseModel):
    id: str
    name: str


class Meta(BaseModel):
    """Everything the UI needs to label and filter: comes from the domain profile and the data."""

    vocabulary: Vocabulary
    segments: list[Option]
    default_segment: str | None
    organisations: list[Option]
    groups: list[Option]
    fields: list[ExtraField]
    seniority: Seniority | None
    offices: list[str]
    event_types: list[str]


class PersonSummary(BaseModel):
    id: str
    name: str | None
    org_id: str
    org_name: str
    title: str | None
    title_norm: str | None
    offices: list[str]
    groups: list[str]
    groups_raw: list[str]
    segments: list[str]
    status: str
    seniority_year: int | None
    first_seen: datetime
    baseline: bool
    last_fetched: datetime | None
    last_changed: datetime | None
    profile_url: str
    note_count: int = 0  # colleagues' notes on this person


class PeoplePage(BaseModel):
    items: list[PersonSummary]
    total: int
    page: int
    page_size: int


class RoleOut(BaseModel):
    org_id: str
    org_name: str
    title: str | None
    offices: list[str]
    profile_url: str
    first_seen: datetime
    last_seen: datetime | None


class EventOut(BaseModel):
    id: str
    type: EventType
    person_id: str
    person_name: str
    org_id: str
    org_name: str
    detected_at: datetime
    summary: str
    offices: list[str]
    segments: list[str]
    related_person_id: str | None
    related_org_name: str | None
    confidence: float | None
    status: Literal["auto", "confirmed", "dismissed"]
    evidence_url: str | None
    simulated: bool
    source: Literal["crawl", "archive"]


class BriefOut(BaseModel):
    brief: Brief
    facts: list[Fact]
    output_id: str
    cached: bool
    model: str
    cost_usd: float
    created_at: datetime
    stale: bool = False
    feedback: int | None = None


class NoteIn(BaseModel):
    # Whitespace is stripped before the length check, so a blank note is rejected.
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    use_in_brief: bool = True


class NoteOut(BaseModel):
    id: str
    text: str
    author: str
    created_at: datetime
    updated_at: datetime | None
    use_in_brief: bool
    can_edit: bool  # the author and admins may edit or delete a note


class PersonDetail(PersonSummary):
    education: list[Education]
    extra: dict[str, list[str]]
    email: str | None
    phone: str | None
    bio: str | None
    provenance: dict[str, Provenance]
    history: list[RoleOut]
    moved_from: str | None
    moved_to: str | None
    departed_at: datetime | None
    events: list[EventOut]
    signals: list[Signal]
    notes: list[NoteOut]
    brief: BriefOut | None
    ai_brief_enabled: bool


class FeedbackIn(BaseModel):
    rating: Literal[-1, 1]
    comment: str | None = Field(default=None, max_length=1000)


class ReviewIn(BaseModel):
    action: Literal["confirm", "dismiss"]


class DigestIn(BaseModel):
    since_days: int = Field(default=7, ge=1, le=90)


class DigestOut(BaseModel):
    digest: Digest
    cached: bool
    model: str
    cost_usd: float
    event_count: int


class RunOut(BaseModel):
    id: str
    org_id: str
    started_at: datetime
    finished_at: datetime | None
    health: str
    discovered: int | None = None
    new: int | None = None
    queued: int | None = None
    missing: int | None = None
    requests: int | None = None
    error: str | None = None


class OrganisationOut(BaseModel):
    id: str
    name: str
    website: str
    kind: str
    enabled: bool
    status: str
    status_reason: str | None
    configured: bool
    notes: str
    last_discovery_at: datetime | None
    discovered: int
    fetched: int
    in_scope: int
    departed: int
    queued_jobs: int
    extraction_failures: int
    fill_rates: dict[str, float]


class SpendRow(BaseModel):
    key: str
    calls: int
    cache_hits: int
    refused: int
    cost_usd: float
    tokens_in: int
    tokens_out: int


class DayRow(BaseModel):
    day: str
    cost_usd: float
    calls: int
    cache_hits: int


class LlmCallOut(BaseModel):
    ts: datetime
    task: str
    model: str
    status: str
    cache_hit: bool
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    user: str | None
    error: str | None


class UsageOut(BaseModel):
    budget_today_usd: float
    spent_today_usd: float
    spent_7d_usd: float
    spent_30d_usd: float
    projected_month_usd: float
    calls_30d: int
    cache_hits_30d: int
    cache_hit_rate: float
    saved_30d_usd: float
    by_task: list[SpendRow]
    by_model: list[SpendRow]
    by_day: list[DayRow]
    feedback: dict[str, int]
    recent: list[LlmCallOut]


class RuntimeSettingsIO(BaseModel):
    daily_budget_usd: float = Field(ge=0)
    tasks: dict[str, TaskSettings]
    fixed_costs: list[FixedCost]


class CostsOut(BaseModel):
    fixed: list[FixedCost]
    fixed_monthly_usd: float
    llm_30d_usd: float
    total_monthly_usd: float
    in_scope_profiles: int
    cost_per_profile_month_usd: float | None
    briefs_generated_30d: int
    briefs_served_30d: int
    cost_per_brief_served_usd: float | None
    scrape_requests_30d: int


class FeatureUsageRow(BaseModel):
    kind: str
    count_30d: int
    users_30d: int
    last_used: datetime | None


class OrgIn(BaseModel):
    """An organisation as edited in the admin panel (the same shape as an entry in organisations.yaml)."""

    name: str = Field(min_length=1)
    website: str = Field(pattern=r"^https?://")
    kind: str = "law_firm"
    discovery: AnyDiscovery | None = None
    selectors: dict[str, str] = Field(default_factory=dict)
    min_delay_s: float | None = Field(default=None, ge=0)
    enabled: bool = True
    notes: str = ""

    @model_validator(mode="after")
    def _regexes_compile(self) -> "OrgIn":
        if self.discovery:
            for field in ("include", "exclude", "child_include"):
                pattern = getattr(self.discovery, field, None)
                try:
                    re.compile(pattern or "")
                except re.error as exc:
                    raise ValueError(f"discovery.{field} is not a valid regex: {exc}") from exc
        return self


class OrgDetail(OrgIn):
    id: str
    managed: bool


class AiSetupIn(BaseModel):
    """What the admin typed: an organisation's name or its website."""

    query: str = Field(min_length=2, max_length=200)


class SurveyOut(BaseModel):
    verdict: str
    robots: str
    sitemaps: list[str]
    people_sitemaps: list[str]
    configured_count: int | None
    profile_count: int
    profile_prefix: str | None
    suggested_include: str | None
    sample_url: str | None
    sample_fields: dict[str, str]
    sample_values: dict[str, Any]
    errors: list[str]


class AiSetupOut(BaseModel):
    """An AI-suggested organisation for the admin to review (not saved), and a survey of it."""

    suggestion: OrgIn
    survey: SurveyOut
    warnings: list[str]
    cost_usd: float


# --- The admin "Worker" page: job queue and worker status ---------------------------------------

JobStatus = Literal["queued", "running", "done", "failed", "cancelled"]


class JobOut(BaseModel):
    id: str
    kind: str
    status: JobStatus
    org_id: str
    org_name: str | None = None
    person_id: str | None = None
    person_name: str | None = None
    reason: str | None = None  # why a fetch was queued (new, changed, stale, user, ...)
    priority: int
    attempts: int
    created_at: datetime
    run_after: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    lease_until: datetime | None = None
    note: str | None = None
    error: str | None = None


class LaneOut(BaseModel):
    lane: int
    job_id: str | None = None  # None = the lane is idle
    kind: str | None = None
    org_id: str | None = None
    org_name: str | None = None
    person_id: str | None = None
    person_name: str | None = None
    started_at: datetime | None = None


class WorkerOut(BaseModel):
    id: str
    # running/idle/stopped is what the worker last said; "stale" = it has gone quiet.
    status: Literal["running", "idle", "stopped", "stale"]
    reason: str | None = None
    started_at: datetime
    last_seen: datetime
    lanes: list[LaneOut]
    paused_orgs: list[str]
    user_agent: str | None = None


class QueueRow(BaseModel):
    kind: str
    due: int  # queued and ready to run now
    waiting: int  # queued for later (a retry backoff, or scheduled)
    running: int
    done_24h: int
    failed_24h: int


class OrgQueueRow(BaseModel):
    """Work waiting per organisation. One lane serves an organisation at a time, so this is
    what limits how many lanes can be busy."""

    org_id: str
    name: str | None = None
    due: int
    waiting: int
    running: int


class BlockedOrg(BaseModel):
    org_id: str
    name: str | None = None
    reason: str | None = None
    blocked_until: datetime | None = None


class HourRow(BaseModel):
    hour: datetime  # the start of the hour (UTC)
    done: int
    failed: int


class WorkerEventOut(BaseModel):
    id: str
    ts: datetime
    level: str
    logger: str
    message: str
    traceback: str | None = None
    worker_id: str


class WorkerStatusOut(BaseModel):
    now: datetime  # the server's clock, so ages on the page don't depend on the browser's
    workers: list[WorkerOut]
    queue: list[QueueRow]
    by_org: list[OrgQueueRow]  # organisations with queued or running work, most ready first
    oldest_due_at: datetime | None  # how long the longest-waiting ready job has waited
    running: list[JobOut]
    blocked: list[BlockedOrg]
    throughput: list[HourRow]  # the last 24 hours, oldest first, one row per hour

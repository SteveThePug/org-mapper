"""Organisation coverage/health for everyone; org editing, spend, toggles, budgets and usage for admins."""

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from radar.api.deps import gateway, refusal, track
from radar.api.schemas import (
    AiSetupIn,
    AiSetupOut,
    CostsOut,
    DayRow,
    DomainOut,
    FeatureUsageRow,
    LlmCallOut,
    OrganisationOut,
    OrgDetail,
    OrgIn,
    RunOut,
    RuntimeSettingsIO,
    SpendRow,
    SurveyOut,
    UsageOut,
)
from radar.auth import AdminUser, CurrentUser
from radar.config import DomainConfig, OrgConfig, get_config
from radar.db import Database, get_db
from radar.domain import load_domain, refresh_domain, save_domain
from radar.llm.gateway import (
    LlmRefused,
    RuntimeSettings,
    load_runtime_settings,
    midnight_utc,
    save_runtime_settings,
    spend_since,
)
from radar.llm.onboard import clean_suggestion, resolve_org, suggest_org
from radar.orgs import from_doc, refresh_orgs, save_org, slugify
from radar.scraping.fetcher import CrawlerNotIdentified, Fetcher
from radar.scraping.onboard import (
    OnboardingError,
    gather_evidence,
    homepage,
    looks_like_url,
    most_common_kinds,
)
from radar.scraping.survey import SurveyResult, survey_organisation
from radar.settings import get_settings
from radar.worker import jobs

router = APIRouter(prefix="/api", tags=["admin"])

_ACTIVE = {"$eq": ["$status", "active"]}


def _count_if(*conditions: Any) -> dict[str, Any]:
    return {"$sum": {"$cond": [{"$and": list(conditions)}, 1, 0]}}


def _non_empty(field: str) -> dict[str, Any]:
    return {"$gt": [{"$size": field}, 0]}


# Per-organisation counts of people, plus how many profiles yielded each field (for fill rates).
_PEOPLE_STATS_BY_ORG = {
    "$group": {
        "_id": "$org_id",
        "discovered": _count_if({"$ne": ["$status", "departed"]}),
        "fetched": _count_if(_ACTIVE),
        "in_scope": _count_if(_ACTIVE, _non_empty("$segments")),
        "departed": _count_if({"$eq": ["$status", "departed"]}),
        "extraction_failures": _count_if({"$eq": ["$extraction_error", True]}),
        "title": _count_if(_ACTIVE, {"$ne": ["$title", None]}),
        "offices": _count_if(_ACTIVE, _non_empty("$offices")),
        "groups": _count_if(_ACTIVE, _non_empty("$groups_raw")),
        "education": _count_if(_ACTIVE, _non_empty("$segments"), _non_empty("$education")),
    }
}


async def _aggregate(db: Database, collection: str, pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return await (await db[collection].aggregate(pipeline)).to_list()


@router.get("/orgs", response_model=list[OrganisationOut])
async def list_orgs(user: CurrentUser) -> list[OrganisationOut]:
    db = get_db()
    await refresh_orgs(db, get_config())  # another API process may have saved an edit
    stats = {row["_id"]: row for row in await _aggregate(db, "people", [_PEOPLE_STATS_BY_ORG])}
    queued = {
        row["_id"]: row["n"]
        for row in await _aggregate(
            db, "jobs", [{"$match": {"status": "queued"}}, {"$group": {"_id": "$org_id", "n": {"$sum": 1}}}]
        )
    }
    orgs = {doc["_id"]: doc async for doc in db["organisations"].find()}
    organisations = []
    for organisation in get_config().organisations:
        org, row = orgs.get(organisation.id, {}), stats.get(organisation.id, {})
        organisations.append(
            OrganisationOut(
                id=organisation.id,
                name=organisation.name,
                website=organisation.website,
                kind=organisation.kind,
                enabled=organisation.enabled,
                status=org.get("status", "new") if organisation.discovery else "not_configured",
                status_reason=org.get("status_reason") if organisation.discovery else None,
                configured=organisation.discovery is not None,
                notes=organisation.notes,
                last_discovery_at=org.get("last_discovery_at"),
                discovered=row.get("discovered", 0),
                fetched=row.get("fetched", 0),
                in_scope=row.get("in_scope", 0),
                departed=row.get("departed", 0),
                queued_jobs=queued.get(organisation.id, 0),
                extraction_failures=row.get("extraction_failures", 0),
                fill_rates=_fill_rates(row),
            )
        )
    return organisations


def _fill_rates(row: dict[str, Any]) -> dict[str, float]:
    """Share of profiles that yielded each field. Education is only kept for in-scope people."""
    rates: dict[str, float] = {}
    if fetched := row.get("fetched", 0):
        rates = {field: round(row.get(field, 0) / fetched, 3) for field in ("title", "offices", "groups")}
    if in_scope := row.get("in_scope", 0):
        rates["education"] = round(row.get("education", 0) / in_scope, 3)
    return rates


# --- organisation editing ---------------------------------------------------------------------

_survey_lock = asyncio.Lock()  # a survey makes real requests to the site: one at a time
# A survey or AI setup holds the lock above; never let one slow site hold it for long.
SURVEY_TIMEOUT_S = 300
_TIMED_OUT = "The website was too slow to read in five minutes; try again later, or fill the form in by hand."


async def _org_doc(org_id: str) -> dict[str, Any]:
    doc = await get_db()["organisations"].find_one({"_id": org_id})
    if doc is None or "name" not in doc:
        raise HTTPException(404, f"Unknown organisation '{org_id}'")
    return doc


@router.get("/admin/orgs/{org_id}", response_model=OrgDetail)
async def get_org(org_id: str, user: AdminUser) -> OrgDetail:
    doc = await _org_doc(org_id)
    return OrgDetail(**from_doc(doc).model_dump(), managed=bool(doc.get("managed")))


@router.post("/admin/orgs", response_model=OrgDetail, status_code=201)
async def create_org(body: OrgIn, user: AdminUser) -> OrgDetail:
    org_id = slugify(body.name)
    if not org_id:
        raise HTTPException(422, "The name needs at least one letter or digit")
    db = get_db()
    if await db["organisations"].find_one({"_id": org_id}):
        raise HTTPException(409, f"An organisation with id '{org_id}' already exists")
    org = OrgConfig(id=org_id, **body.model_dump())
    await save_org(db, org, create=True)
    await refresh_orgs(db, get_config())
    await track(db, user.email, "org_change", org_id, action="create")
    return OrgDetail(**org.model_dump(), managed=True)


@router.put("/admin/orgs/{org_id}", response_model=OrgDetail)
async def update_org(org_id: str, body: OrgIn, user: AdminUser) -> OrgDetail:
    await _org_doc(org_id)
    db = get_db()
    org = OrgConfig(id=org_id, **body.model_dump())
    await save_org(db, org, create=False)
    await refresh_orgs(db, get_config())
    await track(db, user.email, "org_change", org_id, action="update")
    return OrgDetail(**org.model_dump(), managed=True)


@router.post("/admin/orgs/survey", response_model=SurveyOut)
async def survey_org(body: OrgIn, user: AdminUser) -> SurveyOut:
    """Dry run of a (possibly unsaved) config: what would discovery find, and what extracts?"""
    if _survey_lock.locked():
        raise HTTPException(429, "Another survey is running; try again in a moment")
    draft = OrgConfig(id=slugify(body.name) or "draft", **body.model_dump())
    async with _survey_lock:
        fetcher = _fetcher()
        try:
            async with asyncio.timeout(SURVEY_TIMEOUT_S):
                result = await survey_organisation(fetcher, draft)
        except TimeoutError:
            raise HTTPException(504, _TIMED_OUT) from None
        finally:
            await fetcher.aclose()
    return _survey_out(result)


@router.post("/admin/orgs/ai-setup", response_model=AiSetupOut)
async def ai_setup_org(body: AiSetupIn, user: AdminUser) -> AiSetupOut:
    """Name or website in, suggested settings out: we read the site, the AI proposes discovery
    settings and selectors, and we survey the proposal. Nothing is saved; the admin reviews it."""
    if _survey_lock.locked():
        raise HTTPException(429, "Another survey is running; try again in a moment")
    config, query = get_config(), body.query.strip()
    async with _survey_lock:
        fetcher = _fetcher()
        try:
            async with asyncio.timeout(SURVEY_TIMEOUT_S):
                name, cost_usd = "", 0.0
                if looks_like_url(query):
                    website = homepage(query)
                else:
                    resolved, cost_usd = await resolve_org(gateway(), query, config.domain, user.email)
                    if not looks_like_url(resolved.website):
                        raise HTTPException(
                            422, f"The AI couldn't tell which website '{query}' is; type its address instead."
                        )
                    name, website = resolved.name, homepage(resolved.website)
                evidence = await gather_evidence(fetcher, website, config.domain.fields)
                kinds = most_common_kinds([organisation.kind for organisation in config.organisations])
                raw, setup_cost = await suggest_org(gateway(), evidence, config.domain, kinds, user.email)
                cost_usd += setup_cost
                suggestion, warnings = clean_suggestion(raw, evidence.website, name or query, config.domain)
                draft = OrgConfig(id=slugify(suggestion.name) or "draft", **suggestion.model_dump())
                survey = await survey_organisation(fetcher, draft)
        except TimeoutError:
            raise HTTPException(504, _TIMED_OUT) from None
        except LlmRefused as exc:
            raise refusal(exc) from exc
        except OnboardingError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            await fetcher.aclose()
    await track(get_db(), user.email, "org_ai_setup", draft.id, verdict=survey.verdict)
    return AiSetupOut(suggestion=suggestion, survey=_survey_out(survey), warnings=warnings, cost_usd=cost_usd)


def _fetcher() -> Fetcher:
    try:
        return Fetcher.from_settings(get_settings())
    except CrawlerNotIdentified as exc:
        raise HTTPException(409, str(exc)) from None


def _survey_out(result: SurveyResult) -> SurveyOut:
    data = asdict(result)
    data.pop("org_id")
    return SurveyOut(**data, suggested_include=result.suggested_include())


@router.post("/admin/orgs/{org_id}/discover", status_code=202)
async def discover_org(org_id: str, user: AdminUser) -> dict[str, str]:
    """Queue a discovery now (the worker picks it up; it must be running)."""
    doc = await _org_doc(org_id)
    if not doc.get("configured"):
        raise HTTPException(422, "Set up discovery (sitemap URLs and include regex) first")
    db = get_db()
    await jobs.enqueue(db, "discover", f"discover:{org_id}", org_id, {}, jobs.PRIORITY_DISCOVER)
    await track(db, user.email, "org_discover", org_id)
    return {"status": "queued"}


# --- domain profile (who we track, and what we call them) ------------------------------------


@router.get("/admin/domain", response_model=DomainOut)
async def get_domain(user: AdminUser) -> DomainOut:
    config = get_config()
    loaded = await load_domain(get_db(), config.domain_name)
    doc = loaded[1] if loaded else {}
    return DomainOut(
        name=config.domain_name,
        profile=loaded[0] if loaded else config.domain,
        managed=bool(doc.get("managed")),
        updated_at=doc.get("updated_at"),
        updated_by=doc.get("updated_by"),
    )


@router.put("/admin/domain", response_model=DomainOut)
async def update_domain(body: DomainConfig, user: AdminUser) -> DomainOut:
    """Save an edited profile. `DomainConfig` validation rejects bad regexes and dangling references.

    Applies to pages fetched from now on; `POST /admin/reprocess` re-classifies stored people.
    """
    db, config = get_db(), get_config()
    await save_domain(db, config.domain_name, body, user.email)
    await refresh_domain(db, config)
    await track(db, user.email, "domain_change", config.domain_name)
    return await get_domain(user)


@router.post("/admin/reprocess", status_code=202)
async def reprocess_all(user: AdminUser) -> dict[str, int]:
    """Queue a re-parse of every organisation's stored pages with the current profile (no network)."""
    db = get_db()
    orgs = [organisation.id for organisation in get_config().organisations]
    for org_id in orgs:
        await jobs.enqueue(db, "reprocess", f"reprocess:{org_id}", org_id, {}, jobs.PRIORITY_REPROCESS)
    await track(db, user.email, "reprocess", orgs=len(orgs))
    return {"queued": len(orgs)}


@router.get("/admin/runs", response_model=list[RunOut])
async def list_runs(user: AdminUser, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[RunOut]:
    cursor = get_db()["scrape_runs"].find().sort([("started_at", -1)]).limit(limit)
    return [RunOut(id=doc.pop("_id"), **doc) async for doc in cursor]


def _spend_group(key: Any) -> dict[str, Any]:
    return {
        "$group": {
            "_id": key,
            "calls": _count_if({"$eq": ["$cache_hit", False]}, {"$eq": ["$status", "ok"]}),
            "cache_hits": _count_if({"$eq": ["$cache_hit", True]}),
            "refused": _count_if({"$ne": ["$status", "ok"]}),
            "cost_usd": {"$sum": "$cost_usd"},
            "tokens_in": {"$sum": "$tokens_in"},
            "tokens_out": {"$sum": "$tokens_out"},
        }
    }


@router.get("/admin/usage", response_model=UsageOut)
async def llm_usage(user: AdminUser) -> UsageOut:
    db, settings = get_db(), get_settings()
    now = datetime.now(UTC)
    since30 = now - timedelta(days=30)
    match30 = {"$match": {"ts": {"$gte": since30}}}
    by_task = await _aggregate(db, "llm_calls", [match30, _spend_group("$task"), {"$sort": {"cost_usd": -1}}])
    by_model = await _aggregate(
        db, "llm_calls", [match30, _spend_group("$model"), {"$sort": {"cost_usd": -1}}]
    )
    by_day = await _aggregate(
        db,
        "llm_calls",
        [
            match30,
            _spend_group({"$dateToString": {"format": "%Y-%m-%d", "date": "$ts"}}),
            {"$sort": {"_id": 1}},
        ],
    )
    saved_rows = await _aggregate(
        db,
        "llm_calls",
        [
            {"$match": {"ts": {"$gte": since30}, "cache_hit": True}},
            {"$lookup": {"from": "ai_outputs", "localField": "input_hash", "foreignField": "_id", "as": "o"}},
            {"$unwind": "$o"},
            {"$group": {"_id": None, "saved": {"$sum": "$o.cost_usd"}}},
        ],
    )
    feedback_rows = await _aggregate(db, "brief_feedback", [{"$group": {"_id": "$rating", "n": {"$sum": 1}}}])
    runtime = await load_runtime_settings(db, settings)
    spent_today = await spend_since(db, midnight_utc())
    spent_7d = await spend_since(db, now - timedelta(days=7))
    calls = sum(r["calls"] for r in by_task)
    hits = sum(r["cache_hits"] for r in by_task)
    recent = [
        LlmCallOut.model_validate(doc) async for doc in db["llm_calls"].find().sort([("ts", -1)]).limit(50)
    ]
    return UsageOut(
        budget_today_usd=runtime.daily_budget_usd,
        spent_today_usd=spent_today,
        spent_7d_usd=spent_7d,
        spent_30d_usd=sum(r["cost_usd"] for r in by_task),
        projected_month_usd=round(spent_7d / 7 * 30, 4),
        calls_30d=calls,
        cache_hits_30d=hits,
        cache_hit_rate=round(hits / (calls + hits), 3) if calls + hits else 0.0,
        saved_30d_usd=saved_rows[0]["saved"] if saved_rows else 0.0,
        by_task=[SpendRow(key=r.pop("_id") or "-", **r) for r in by_task],
        by_model=[SpendRow(key=r.pop("_id") or "-", **r) for r in by_model],
        by_day=[
            DayRow(day=r["_id"], cost_usd=r["cost_usd"], calls=r["calls"], cache_hits=r["cache_hits"])
            for r in by_day
        ],
        feedback={"up" if r["_id"] == 1 else "down": r["n"] for r in feedback_rows},
        recent=recent,
    )


@router.get("/admin/settings", response_model=RuntimeSettingsIO)
async def get_runtime_settings(user: AdminUser) -> RuntimeSettingsIO:
    runtime = await load_runtime_settings(get_db(), get_settings())
    return RuntimeSettingsIO.model_validate(runtime.model_dump())


@router.put("/admin/settings", response_model=RuntimeSettingsIO)
async def put_runtime_settings(body: RuntimeSettingsIO, user: AdminUser) -> RuntimeSettingsIO:
    db = get_db()
    current = await load_runtime_settings(db, get_settings())
    unknown = set(body.tasks) - set(current.tasks)
    if unknown:
        raise HTTPException(422, f"Unknown tasks: {', '.join(sorted(unknown))}")
    await save_runtime_settings(db, RuntimeSettings(**body.model_dump()))
    await track(db, user.email, "settings_change", **body.model_dump())
    return body


@router.get("/admin/costs", response_model=CostsOut)
async def costs(user: AdminUser) -> CostsOut:
    db = get_db()
    since30 = datetime.now(UTC) - timedelta(days=30)
    runtime = await load_runtime_settings(db, get_settings())
    fixed = sum(c.monthly_usd for c in runtime.fixed_costs)
    llm_30d = await spend_since(db, since30)
    in_scope = await db["people"].count_documents({"status": "active", "segments": {"$ne": []}})
    brief_rows = await _aggregate(
        db,
        "llm_calls",
        [{"$match": {"ts": {"$gte": since30}, "task": "call_prep_brief"}}, _spend_group(None)],
    )
    brief = brief_rows[0] if brief_rows else {"calls": 0, "cache_hits": 0, "cost_usd": 0.0}
    served = brief["calls"] + brief["cache_hits"]
    crawl = await _aggregate(
        db,
        "crawl_stats",
        [
            {"$match": {"day": {"$gte": f"{since30:%Y-%m-%d}"}}},
            {"$group": {"_id": None, "requests": {"$sum": "$requests"}}},
        ],
    )
    total = fixed + llm_30d
    return CostsOut(
        fixed=runtime.fixed_costs,
        fixed_monthly_usd=fixed,
        llm_30d_usd=llm_30d,
        total_monthly_usd=total,
        in_scope_profiles=in_scope,
        cost_per_profile_month_usd=round(total / in_scope, 4) if in_scope else None,
        briefs_generated_30d=brief["calls"],
        briefs_served_30d=served,
        cost_per_brief_served_usd=round(brief["cost_usd"] / served, 5) if served else None,
        scrape_requests_30d=crawl[0]["requests"] if crawl else 0,
    )


@router.get("/admin/feature-usage", response_model=list[FeatureUsageRow])
async def feature_usage(user: AdminUser) -> list[FeatureUsageRow]:
    since30 = datetime.now(UTC) - timedelta(days=30)
    rows = await _aggregate(
        get_db(),
        "usage_events",
        [
            {"$match": {"ts": {"$gte": since30}}},
            {
                "$group": {
                    "_id": "$kind",
                    "n": {"$sum": 1},
                    "users": {"$addToSet": "$user"},
                    "last": {"$max": "$ts"},
                }
            },
            {"$sort": {"n": -1}},
        ],
    )
    return [
        FeatureUsageRow(kind=r["_id"], count_30d=r["n"], users_30d=len(r["users"]), last_used=r["last"])
        for r in rows
    ]

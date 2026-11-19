"""Directory, profiles, call-prep briefs and on-demand refreshes."""

import csv
import io
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from radar.api.deps import event_out, gateway, organisation_names, refusal, track
from radar.api.notes import note_counts, note_out, person_notes
from radar.api.schemas import BriefOut, FeedbackIn, PeoplePage, PersonDetail, PersonSummary, RoleOut
from radar.auth import CurrentUser
from radar.config import get_config
from radar.db import get_db
from radar.llm.gateway import LlmRefused, load_runtime_settings
from radar.llm.tasks import Brief, BriefItem, Fact, brief_facts, generate_brief, note_fact_id
from radar.models import Event, Note, Person
from radar.settings import get_settings
from radar.signals import Signal, person_signals
from radar.worker import jobs

router = APIRouter(prefix="/api", tags=["people"])

# `segment` query value meaning "the domain profile's default segment" (the parameter's default).
DEFAULT_SEGMENT = "default"


def _summary(person: Person, org_names: dict[str, str], note_count: int = 0) -> dict[str, Any]:
    data = person.model_dump(include=set(PersonSummary.model_fields) - {"org_name", "note_count"})
    return data | {"org_name": org_names.get(person.org_id, person.org_id), "note_count": note_count}


def _people_query(
    segment: str | None,
    organisation: str | None,
    office: str | None,
    group: str | None,
    status: str,
    q: str | None,
) -> dict[str, Any]:
    query: dict[str, Any] = {"status": status} if status != "any" else {"status": {"$ne": "pending"}}
    if segment == DEFAULT_SEGMENT:
        segment = get_config().domain.default_segment_id
    if segment and segment != "all":
        query["segments"] = segment
    if organisation:
        query["org_id"] = organisation
    if office:
        query["offices"] = office
    if group:
        query["groups"] = group
    if q:
        query["name"] = {"$regex": re.escape(q.strip()), "$options": "i"}
    return query


@router.get("/people", response_model=PeoplePage)
async def list_people(
    user: CurrentUser,
    segment: str | None = DEFAULT_SEGMENT,
    organisation: str | None = None,
    office: str | None = None,
    group: str | None = None,
    status: str = "active",
    q: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PeoplePage:
    db, org_names = get_db(), organisation_names()
    query = _people_query(segment, organisation, office, group, status, q)
    total = await db["people"].count_documents(query)
    cursor = db["people"].find(query).sort([("name", 1)]).skip((page - 1) * page_size).limit(page_size)
    page_people = [Person.model_validate(doc) async for doc in cursor]
    counts = await note_counts([p.id for p in page_people])
    items = [PersonSummary(**_summary(p, org_names, counts.get(p.id, 0))) for p in page_people]
    if q:
        await track(db, user.email, "search", q=q)
    return PeoplePage(items=items, total=total, page=page, page_size=page_size)


@router.get("/people/export.csv")
async def export_people(
    user: CurrentUser,
    segment: str | None = DEFAULT_SEGMENT,
    organisation: str | None = None,
    office: str | None = None,
    group: str | None = None,
    status: str = "active",
    q: str | None = None,
) -> StreamingResponse:
    db, org_names = get_db(), organisation_names()
    query = _people_query(segment, organisation, office, group, status, q)
    await track(db, user.email, "export", **{k: v for k, v in query.items() if isinstance(v, str)})

    async def rows() -> AsyncIterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        # Contact details are deliberately not exported: the profile URL is the source of truth.
        domain = get_config().domain
        words = domain.vocabulary
        seniority = [domain.seniority.label.lower() + " year"] if domain.seniority else []
        header = ["name", words.org.lower(), "title", words.locations.lower(), words.groups.lower()]
        writer.writerow([*header, *seniority, "profile", "last verified"])
        async for doc in db["people"].find(query).sort([("org_id", 1), ("name", 1)]):
            p = Person.model_validate(doc)
            writer.writerow(
                [
                    p.name,
                    org_names.get(p.org_id, p.org_id),
                    p.title,
                    "; ".join(p.offices),
                    "; ".join(p.groups_raw),
                    *([p.seniority_year or ""] if seniority else []),
                    p.profile_url,
                    f"{p.last_fetched:%Y-%m-%d}" if p.last_fetched else "",
                ]
            )
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
        yield buffer.getvalue()  # the header alone, when nothing matched

    return StreamingResponse(
        rows(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="people-{datetime.now(UTC):%Y%m%d}.csv"'},
    )


async def _load(person_id: str) -> Person:
    doc = await get_db()["people"].find_one({"_id": person_id})
    if doc is None:
        raise HTTPException(404, "Person not found")
    return Person.model_validate(doc)


async def _context(person: Person) -> tuple[list[Event], list[Signal], list[Note], list[Fact]]:
    db, org_names = get_db(), organisation_names()
    events = [
        Event.model_validate(doc)
        async for doc in db["events"]
        .find(
            {
                "$or": [{"person_id": person.id}, {"related_person_id": person.id}],
                "status": {"$ne": "dismissed"},
            }
        )
        .sort([("detected_at", -1)])
        .limit(20)
    ]
    signals = await person_signals(db, person, org_names, get_config().domain)
    notes = await person_notes(person.id)
    facts = brief_facts(person, get_config(), [e for e in events if not e.simulated], signals, notes)
    return events, signals, notes, facts


async def _feedback(output_id: str) -> int | None:
    doc = await get_db()["brief_feedback"].find_one({"_id": output_id})
    return int(doc["rating"]) if doc else None


async def _latest_brief(
    person: Person, events: list[Event], notes: list[Note], facts: list[Fact]
) -> BriefOut | None:
    """The most recent brief for this person, flagged stale if the profile, its events or the
    notes it may use are newer, or if a note it cites has been deleted."""
    latest = await get_db()["ai_outputs"].find_one(
        {"task": "call_prep_brief", "subject_id": person.id}, sort=[("created_at", -1)]
    )
    if latest is None:
        return None
    written_at = latest["created_at"]
    profile_changed = bool(person.last_changed and person.last_changed > written_at)
    new_events = any(event.detected_at > written_at for event in events)
    brief = Brief.model_validate(latest["output"])
    new_notes = any((n.updated_at or n.created_at) > written_at for n in notes if n.use_in_brief)
    usable_note_ids = {note_fact_id(n) for n in notes if n.use_in_brief}
    cited = {fid for section in _sections(brief) for item in section for fid in item.facts}
    note_gone = any(fid.startswith("N-") and fid not in usable_note_ids for fid in cited)
    return BriefOut(
        brief=brief,
        facts=facts,
        output_id=latest["_id"],
        cached=True,
        model=latest["model"],
        cost_usd=latest.get("cost_usd", 0.0),
        created_at=written_at,
        stale=profile_changed or new_events or new_notes or note_gone,
        feedback=await _feedback(latest["_id"]),
    )


def _sections(brief: Brief) -> list[list[BriefItem]]:
    return [brief.career, brief.recent_changes, brief.be_aware_of, brief.talking_points]


@router.get("/people/{person_id}", response_model=PersonDetail)
async def get_person(person_id: str, user: CurrentUser) -> PersonDetail:
    db, org_names, settings = get_db(), organisation_names(), get_settings()
    person = await _load(person_id)
    events, signals, notes, facts = await _context(person)
    runtime = await load_runtime_settings(db, settings)
    await track(db, user.email, "profile_view", person.id)

    # Each past role stores only an org id; add the org's display name (or fall back to the id).
    history = []
    for role in person.history:
        org_name = org_names.get(role.org_id, role.org_id)
        history.append(RoleOut(**role.model_dump(), org_name=org_name))

    return PersonDetail(
        **_summary(person, org_names, len(notes)),
        **person.model_dump(
            include={
                "education",
                "extra",
                "email",
                "phone",
                "bio",
                "provenance",
                "moved_from",
                "moved_to",
                "departed_at",
            }
        ),
        history=history,
        events=[event_out(e, org_names) for e in events],
        signals=signals,
        notes=[note_out(note, user) for note in notes],
        brief=await _latest_brief(person, events, notes, facts),
        ai_brief_enabled=runtime.tasks["call_prep_brief"].enabled and settings.llm_enabled,
    )


@router.post("/people/{person_id}/brief", response_model=BriefOut)
async def create_brief(person_id: str, user: CurrentUser) -> BriefOut:
    db, config = get_db(), get_config()
    person = await _load(person_id)
    _, _, _, facts = await _context(person)
    domain = config.domain
    segment = domain.segment_name(person.segments)
    try:
        result = await generate_brief(gateway(), person, facts, segment, domain.brief_context, user.email)
    except LlmRefused as exc:
        await track(db, user.email, "brief_refused", person.id, reason=exc.status)
        raise refusal(exc) from exc
    await track(db, user.email, "brief", person.id, cached=result.cached)
    return BriefOut(
        brief=result.output,
        facts=facts,
        output_id=result.output_id,
        cached=result.cached,
        model=result.model,
        cost_usd=result.cost_usd,
        created_at=result.created_at,
        feedback=await _feedback(result.output_id),
    )


@router.post("/briefs/{output_id}/feedback", status_code=204)
async def brief_feedback(output_id: str, body: FeedbackIn, user: CurrentUser) -> None:
    await get_db()["brief_feedback"].replace_one(
        {"_id": output_id},
        {
            "_id": output_id,
            "rating": body.rating,
            "comment": body.comment,
            "user": user.email,
            "ts": datetime.now(UTC),
        },
        upsert=True,
    )


@router.post("/people/{person_id}/refresh", status_code=202)
async def refresh_person(person_id: str, user: CurrentUser) -> dict[str, str]:
    db = get_db()
    person = await _load(person_id)
    await jobs.enqueue(
        db,
        "fetch",
        f"fetch:{person.id}",
        person.org_id,
        {"person_id": person.id, "reason": "user"},
        jobs.PRIORITY_USER,
    )
    await track(db, user.email, "refresh", person.id)
    return {"status": "queued"}

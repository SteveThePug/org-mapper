"""The market-moves feed: who joined, left, moved or was promoted."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from radar.api.deps import event_out, gateway, organisation_names, refusal, track
from radar.api.schemas import DigestIn, DigestOut, EventOut, ReviewIn
from radar.auth import CurrentUser
from radar.config import get_config
from radar.db import get_db
from radar.llm.gateway import LlmRefused
from radar.llm.tasks import generate_digest
from radar.models import Event, Person
from radar.worker.crawler import POSSIBLE_MATCH_SUFFIX, link_move

router = APIRouter(prefix="/api/events", tags=["events"])


def _events_query(
    since_days: int,
    types: list[str] | None,
    organisation: str | None,
    office: str | None,
    segment: str | None,
) -> dict[str, Any]:
    query: dict[str, Any] = {
        "detected_at": {"$gte": datetime.now(UTC) - timedelta(days=since_days)},
        "status": {"$ne": "dismissed"},
    }
    if types:
        query["type"] = {"$in": types}
    if organisation:
        query["$or"] = [{"org_id": organisation}, {"related_org_id": organisation}]
    if office:
        query["offices"] = office
    if segment and segment != "all":
        query["segments"] = segment
    return query


@router.get("", response_model=list[EventOut])
async def list_events(
    user: CurrentUser,
    since_days: Annotated[int, Query(ge=1, le=3650)] = 30,
    types: Annotated[list[str] | None, Query()] = None,
    organisation: str | None = None,
    office: str | None = None,
    segment: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[EventOut]:
    db, names = get_db(), organisation_names()
    query = _events_query(since_days, types, organisation, office, segment)
    cursor = db["events"].find(query).sort([("detected_at", -1)]).limit(limit)
    events = [event_out(Event.model_validate(doc), names) async for doc in cursor]
    await track(db, user.email, "feed_view")
    return events


@router.post("/{event_id}/review", response_model=EventOut)
async def review_event(event_id: str, body: ReviewIn, user: CurrentUser) -> EventOut:
    """Human in the loop for uncertain lateral-move matches."""
    db = get_db()
    doc = await db["events"].find_one({"_id": event_id})
    if doc is None:
        raise HTTPException(404, "Event not found")
    event = Event.model_validate(doc)
    update: dict[str, Any] = {
        "status": "confirmed" if body.action == "confirm" else "dismissed",
        "reviewed_by": user.email,
    }
    if body.action == "confirm" and event.type == "possible_move" and event.related_person_id:
        old_doc = await db["people"].find_one({"_id": event.related_person_id})
        new_doc = await db["people"].find_one({"_id": event.person_id})
        if old_doc and new_doc:
            await link_move(db, Person.model_validate(old_doc), Person.model_validate(new_doc))
        update["type"] = "moved"
        update["summary"] = event.summary.split(POSSIBLE_MATCH_SUFFIX)[0]
    await db["events"].update_one({"_id": event_id}, {"$set": update})
    await track(db, user.email, "event_review", event.person_id, action=body.action)
    reviewed = await db["events"].find_one({"_id": event_id})
    assert reviewed is not None
    return event_out(Event.model_validate(reviewed), organisation_names())


@router.post("/digest", response_model=DigestOut)
async def market_digest(body: DigestIn, user: CurrentUser) -> DigestOut:
    db = get_db()
    query = _events_query(body.since_days, None, None, None, None)
    query["simulated"] = {"$ne": True}
    events = [
        Event.model_validate(d) async for d in db["events"].find(query).sort([("detected_at", -1)]).limit(60)
    ]
    if not events:
        raise HTTPException(404, "No market moves in that period yet.")
    try:
        result = await generate_digest(gateway(), events, get_config().domain.digest_context, user.email)
    except LlmRefused as exc:
        raise refusal(exc) from exc
    await track(db, user.email, "digest", cached=result.cached)
    return DigestOut(
        digest=result.output,
        cached=result.cached,
        model=result.model,
        cost_usd=result.cost_usd,
        event_count=len(events),
    )

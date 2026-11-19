"""Shared helpers for the API routes."""

from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from fastapi import HTTPException

from radar.api.schemas import EventOut
from radar.config import get_config
from radar.db import Database, get_db
from radar.llm.gateway import LlmGateway, LlmRefused
from radar.models import Event
from radar.settings import get_settings

REFUSAL_STATUS = {"disabled": 403, "unavailable": 503, "over_budget": 429, "error": 502}


def organisation_names() -> dict[str, str]:
    return {organisation.id: organisation.name for organisation in get_config().organisations}


def event_out(event: Event, names: dict[str, str]) -> EventOut:
    return EventOut(
        **event.model_dump(include=set(EventOut.model_fields) - {"org_name", "related_org_name"}),
        org_name=names.get(event.org_id, event.org_id),
        related_org_name=(
            names.get(event.related_org_id, event.related_org_id) if event.related_org_id else None
        ),
    )


async def track(db: Database, user: str, kind: str, subject_id: str | None = None, **meta: Any) -> None:
    """Feature-usage log: which features earn their keep (and an access log for accountability)."""
    await db["usage_events"].insert_one(
        {"ts": datetime.now(UTC), "user": user, "kind": kind, "subject_id": subject_id, "meta": meta}
    )


def refusal(exc: LlmRefused) -> HTTPException:
    return HTTPException(REFUSAL_STATUS.get(exc.status, 400), exc.reason)


@lru_cache
def gateway() -> LlmGateway:
    """One gateway per API process (it holds the OpenRouter client)."""
    return LlmGateway(get_db(), get_settings())

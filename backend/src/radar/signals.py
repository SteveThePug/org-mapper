"""Derived signals: cheap, explainable facts computed from our own history (no LLM).

These feed the "be aware of" part of a brief and are shown on the profile as-is. History
only exists since we started tracking an organisation, so signals say so instead of over-claiming.
"""

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from radar.config import DomainConfig
from radar.db import Database
from radar.models import Person

RECENT_MOVE = timedelta(days=548)  # ~18 months
TEAM_WINDOW = timedelta(days=180)
FLOW_WINDOW = timedelta(days=365)


class Signal(BaseModel):
    id: str
    kind: Literal["seniority", "tenure", "mobility", "team", "organisation", "freshness"]
    text: str
    caution: bool = False  # true = something the recruiter should be careful about


async def person_signals(
    db: Database, person: Person, organisation_names: dict[str, str], domain: DomainConfig
) -> list[Signal]:
    now = datetime.now(UTC)
    org_name = organisation_names.get(person.org_id, person.org_id)
    people = domain.vocabulary.people.lower()
    signals: list[Signal] = []

    if person.seniority_year and domain.seniority:
        year = person.seniority_year
        signals.append(
            Signal(
                id="S-seniority",
                kind="seniority",
                text=f"{domain.seniority.label} in {year} (~{now.year - year} years since).",
            )
        )

    org = await db["organisations"].find_one({"_id": person.org_id}) or {}
    tracking_since: datetime | None = org.get("first_discovery_at") or org.get("last_discovery_at")
    if person.moved_from:
        previous = await db["people"].find_one({"_id": person.moved_from}, {"org_id": 1})
        elsewhere = f"another {domain.vocabulary.org.lower()}"
        previous_org = organisation_names.get(previous["org_id"], elsewhere) if previous else elsewhere
        signals.append(
            Signal(
                id="S-moved",
                kind="mobility",
                text=f"Moved to {org_name} from {previous_org} around {person.first_seen:%b %Y}. "
                "Recent movers rarely move again within ~2 years.",
                caution=True,
            )
        )
    elif not person.baseline:
        recent = now - person.first_seen < RECENT_MOVE
        text = f"Appeared at {org_name} around {person.first_seen:%b %Y}"
        text += " - a recent arrival." if recent else "."
        signals.append(Signal(id="S-tenure", kind="tenure", text=text, caution=recent))

    if person.offices:
        departures = await db["events"].count_documents(
            {
                "type": "left",
                "org_id": person.org_id,
                "offices": {"$in": person.offices},
                "person_id": {"$ne": person.id},
                "detected_at": {"$gte": now - TEAM_WINDOW},
                "simulated": {"$ne": True},
            }
        )
        if departures:
            signals.append(
                Signal(
                    id="S-team",
                    kind="team",
                    text=f"{departures} {people} have left {org_name} ({', '.join(person.offices)}) "
                    "in the last 6 months.",
                    caution=departures >= 2,
                )
            )

    inflow = await db["events"].count_documents(
        {
            "org_id": person.org_id,
            "type": {"$in": ["joined", "moved"]},
            "detected_at": {"$gte": now - FLOW_WINDOW},
        }
    )
    outflow = await db["events"].count_documents(
        {"org_id": person.org_id, "type": "left", "detected_at": {"$gte": now - FLOW_WINDOW}}
    )
    if inflow or outflow:
        since = f" (tracking since {tracking_since:%b %Y})" if tracking_since else ""
        signals.append(
            Signal(
                id="S-flow",
                kind="organisation",
                text=f"{org_name} {domain.vocabulary.person.lower()} flow, last 12 months{since}: +{inflow} joined / -{outflow} left.",
                caution=outflow > inflow,
            )
        )

    if person.last_fetched:
        text = f"Profile last verified on the {domain.vocabulary.org.lower()}'s website {person.last_fetched:%d %b %Y}"
        # A change on the first day is just the initial fetch, not the page being edited.
        if person.last_changed and person.last_changed - person.first_seen > timedelta(days=1):
            text += f"; its content last changed {person.last_changed:%d %b %Y}."
        else:
            text += "."
        signals.append(Signal(id="S-fresh", kind="freshness", text=text))
    return signals

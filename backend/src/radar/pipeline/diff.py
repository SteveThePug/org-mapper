"""Turns "person before" vs "person after" into market events. Pure functions, no I/O."""

from dataclasses import dataclass, field
from typing import Any

from radar.config import DomainConfig
from radar.models import EventType, Person


@dataclass
class EventDraft:
    type: EventType
    summary: str
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)


def diff_person(before: Person, after: Person, org_name: str, domain: DomainConfig) -> list[EventDraft]:
    """Events caused by a fresh fetch of a profile.

    Only people inside a target segment (before or after) generate events: an associate
    changing office is not news for this product, an associate becoming partner is.
    A change into one of the domain's `promotion_to` titles is a promotion.
    """
    who = after.name or "Unknown"
    where = _where(after, org_name)

    if before.status == "pending":  # first successful fetch of this profile
        if after.baseline or not after.in_scope:
            return []
        return [
            EventDraft(
                "joined",
                f"{who} appeared at {where} as {after.title or domain.vocabulary.person.lower()}{_group_suffix(after)}",
                after=_snapshot(after),
            )
        ]

    if not (before.in_scope or after.in_scope):
        return []

    drafts: list[EventDraft] = []
    if before.title_norm and after.title_norm and before.title_norm != after.title_norm:
        if after.title_norm in domain.promotion_to:
            drafts.append(
                EventDraft(
                    "promoted",
                    f"{who} was promoted to {after.title} at {where}",
                    before={"title": before.title},
                    after={"title": after.title},
                )
            )
        else:
            drafts.append(
                EventDraft(
                    "title_changed",
                    f"{who} changed title from {before.title} to {after.title} at {org_name}",
                    before={"title": before.title},
                    after={"title": after.title},
                )
            )
    elif before.title and after.title and before.title != after.title:
        drafts.append(
            EventDraft(
                "title_changed",
                f"{who} is now {after.title} (was {before.title}) at {org_name}",
                before={"title": before.title},
                after={"title": after.title},
            )
        )

    if before.offices and after.offices and set(before.offices) != set(after.offices):
        added = sorted(set(after.offices) - set(before.offices))
        removed = sorted(set(before.offices) - set(after.offices))
        change = ", ".join([*(f"+{o}" for o in added), *(f"-{o}" for o in removed)])
        drafts.append(
            EventDraft(
                "office_changed",
                f"{who} ({org_name}) office change: {change}",
                before={"offices": before.offices},
                after={"offices": after.offices},
            )
        )

    if before.groups and after.groups and set(before.groups) != set(after.groups):
        drafts.append(
            EventDraft(
                "group_changed",
                f"{who} ({org_name}) {domain.vocabulary.groups.lower()} changed: {', '.join(before.groups)} -> "
                f"{', '.join(after.groups)}",
                before={"groups": before.groups_raw},
                after={"groups": after.groups_raw},
            )
        )
    return drafts


def left_event(person: Person, org_name: str) -> EventDraft:
    return EventDraft(
        "left",
        f"{person.name or 'Unknown'} left {_where(person, org_name)} (profile removed)",
        before=_snapshot(person),
    )


def _snapshot(person: Person) -> dict[str, Any]:
    return {"title": person.title, "offices": person.offices, "groups": person.groups_raw}


def _where(person: Person, org_name: str) -> str:
    return f"{org_name} ({', '.join(person.offices)})" if person.offices else org_name


def _group_suffix(person: Person) -> str:
    return f" - {', '.join(person.groups_raw[:3])}" if person.groups_raw else ""

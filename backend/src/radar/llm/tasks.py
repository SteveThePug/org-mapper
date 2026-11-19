"""The AI features. Each one = inputs built from stored facts + a prompt version + an output schema.

Inputs are numbered facts with their sources, and outputs must cite those ids. The UI turns the
citations back into links, so every sentence in a brief is traceable to a verifiable source.
"""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from radar.config import AppConfig
from radar.llm.gateway import LlmGateway, LlmResult
from radar.models import Event, Note, Person
from radar.signals import Signal

PROMPTS = Path(__file__).parent / "prompts"
BRIEF_PROMPT_VERSION = "brief-v4"
DIGEST_PROMPT_VERSION = "digest-v2"
BIO_CHARS_IN_PROMPT = 1500
# Colleagues' notes sent to the model: the newest few, each cut to a sensible length.
NOTES_IN_PROMPT = 10
NOTE_CHARS_IN_PROMPT = 1000


class Fact(BaseModel):
    id: str
    text: str
    source: str | None = None


class BriefItem(BaseModel):
    text: str
    facts: list[str] = Field(default_factory=list)


class Brief(BaseModel):
    summary: str
    career: list[BriefItem] = Field(default_factory=list)
    recent_changes: list[BriefItem] = Field(default_factory=list)
    be_aware_of: list[BriefItem] = Field(default_factory=list)
    talking_points: list[BriefItem] = Field(default_factory=list)
    opening_line: str = ""
    unknowns: list[str] = Field(default_factory=list)


# Fact ids whose text changes without the person's situation changing.
VOLATILE_FACTS = {"S-fresh"}


def note_fact_id(note: Note) -> str:
    """Stable per note, so adding or deleting another note doesn't change what a citation points at."""
    return f"N-{note.id[:8]}"


def brief_facts(
    person: Person,
    config: AppConfig,
    events: list[Event],
    signals: list[Signal],
    notes: list[Note] | None = None,
) -> list[Fact]:
    organisation = config.organisation(person.org_id).name
    words = config.domain.vocabulary
    url = person.profile_url
    facts = [
        Fact(id="F1", text=f"{person.name}: {person.title or 'title unknown'} at {organisation}", source=url),
    ]
    if person.offices:
        facts.append(Fact(id="F2", text=f"{words.locations}: {', '.join(person.offices)}", source=url))
    if person.groups_raw:
        facts.append(Fact(id="F3", text=f"{words.groups}: {', '.join(person.groups_raw)}", source=url))
    for i, edu in enumerate(person.education, start=1):
        facts.append(Fact(id=f"F-edu{i}", text=f"Education: {edu.raw}", source=url))
    for extra in config.domain.fields:
        if person.extra.get(extra.key):
            text = f"{extra.label}: {'; '.join(person.extra[extra.key])}"
            facts.append(Fact(id=f"F-{extra.key}", text=text, source=url))
    if person.bio:
        facts.append(
            Fact(id="F-bio", text=f"{words.org} bio: {person.bio[:BIO_CHARS_IN_PROMPT]}", source=url)
        )
    for i, role in enumerate(person.history, start=1):
        facts.append(
            Fact(
                id=f"F-prev{i}",
                text=f"Previously {role.title or words.person.lower()} at {config.organisation(role.org_id).name}",
                source=role.profile_url,
            )
        )
    for i, event in enumerate(events[:8], start=1):
        facts.append(
            Fact(id=f"E{i}", text=f"{event.detected_at:%d %b %Y}: {event.summary}", source=event.evidence_url)
        )
    facts.extend(Fact(id=s.id, text=s.text) for s in signals)
    # The author's email is left out: the model doesn't need to know who wrote it.
    usable = [note for note in notes or [] if note.use_in_brief][:NOTES_IN_PROMPT]
    for note in usable:
        text = f"Note from a colleague, {note.created_at:%d %b %Y}: {note.text[:NOTE_CHARS_IN_PROMPT]}"
        facts.append(Fact(id=note_fact_id(note), text=text))
    return facts


async def generate_brief(
    gateway: LlmGateway, person: Person, facts: list[Fact], segment_name: str, context: str, user: str
) -> LlmResult[Brief]:
    """`context` is the domain profile's `brief_context`: who the brief is for, and about whom."""
    payload: dict[str, Any] = {"segment": segment_name, "facts": [f.model_dump() for f in facts]}
    # The "last verified" date changes on every routine re-fetch, even when nothing on the page
    # did. Leave it out of the cache key, or every brief would be regenerated (and paid for) weekly.
    stable = {**payload, "facts": [f.model_dump() for f in facts if f.id not in VOLATILE_FACTS]}
    return await gateway.run(
        task="call_prep_brief",
        prompt_version=BRIEF_PROMPT_VERSION,
        system=context.strip() + "\n\n" + (PROMPTS / "brief.md").read_text(),
        payload=payload,
        schema=Brief,
        subject_id=person.id,
        user=user,
        cache_payload=stable,
    )


class Digest(BaseModel):
    headline: str
    bullets: list[str] = Field(default_factory=list)


DIGEST_RULES = (
    "You receive a JSON list of dated events. Using only those events, return a JSON object "
    '{"headline": "one sentence", "bullets": ["3-6 short bullets grouping related moves by organisation '
    'or location"]}. Do not speculate about reasons for moves. British English.'
)


async def generate_digest(
    gateway: LlmGateway, events: list[Event], context: str, user: str
) -> LlmResult[Digest]:
    """`context` is the domain profile's `digest_context`."""
    payload = {
        "events": [
            {"date": f"{e.detected_at:%Y-%m-%d}", "type": e.type, "summary": e.summary} for e in events[:60]
        ]
    }
    return await gateway.run(
        task="market_digest",
        prompt_version=DIGEST_PROMPT_VERSION,
        system=f"{context.strip()} {DIGEST_RULES}",
        payload=payload,
        schema=Digest,
        user=user,
    )

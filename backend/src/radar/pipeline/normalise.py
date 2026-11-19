"""Deterministic clean-up of extracted records: titles, groups, education, segments."""

import re
from datetime import datetime

from radar.config import DomainConfig, GroupTag, Segment, Seniority
from radar.models import Education, Person, ProfileRecord, Provenance

_YEAR = re.compile(r"\b(19[4-9]\d|20[0-4]\d)\b")
_DEGREE = re.compile(
    r"\b(J\.?\s?D\.?|Juris Doctor|LL\.?\s?M\.?|LL\.?\s?B\.?|B\.?\s?C\.?\s?L\.?|M\.?\s?B\.?\s?A\.?|"
    r"Ph\.?\s?D\.?|B\.?\s?A\.?|B\.?\s?S\.?\s?c?\.?|M\.?\s?A\.?|M\.?\s?Sc\.?|"
    r"Bachelor of [A-Za-z ]+?|Master of [A-Za-z ]+?|Ma[iî]trise|D\.?E\.?A\.?|D\.?E\.?S\.?S\.?)(?=[\s,;()]|$)",
    re.IGNORECASE,
)
_HONOURS = re.compile(r"\b(summa|magna)?\s*cum laude\b|\bwith (high )?honou?rs\b|\bOrder of the Coif\b", re.I)
_SCHOOL_HINT = re.compile(r"universit|college|school|institut|academy|faculty|polytechn|sciences po", re.I)
# Kept only while a person is in a target segment (see `minimise`), with the domain's extra fields.
_IN_SCOPE_DETAIL = ("education", "email", "phone", "bio")


def normalise_title(title: str | None, titles: dict[str, list[str]]) -> str | None:
    if not title:
        return None
    text = title.lower()
    for canonical, keywords in titles.items():
        if any(keyword in text for keyword in keywords):
            return canonical
    return "other"


def classify_groups(
    labels: list[str], taxonomy: dict[str, GroupTag], learned: dict[str, list[str]] | None = None
) -> list[str]:
    """Canonical tags for a site's labels. Keyword rules first; `learned` (saved AI answers, keyed by
    lower-cased label, see radar.llm.classify) only fills in labels that no rule matches."""
    tags: list[str] = []
    for label in labels:
        matched = [tag for tag, rule in taxonomy.items() if rule.matches(label)]
        if not matched and learned:
            matched = learned.get(" ".join(label.lower().split()), [])
        tags += [tag for tag in matched if tag not in tags]
    return tags


def unmapped_labels(labels: list[str], taxonomy: dict[str, GroupTag]) -> list[str]:
    return [label for label in labels if not any(rule.matches(label) for rule in taxonomy.values())]


def segments_for(
    org_id: str, title_norm: str | None, groups: list[str], segments: list[Segment]
) -> list[str]:
    return [segment.id for segment in segments if segment.contains(org_id, title_norm, groups)]


def parse_education(lines: list[str]) -> list[Education]:
    parsed: list[Education] = []
    for raw in lines:
        degree_match = _DEGREE.search(raw)
        years = _YEAR.findall(raw)
        degree = re.sub(r"[\s.]", "", degree_match.group(1)) if degree_match else None
        parsed.append(
            Education(
                raw=raw,
                degree=degree.upper() if degree and len(degree) <= 5 else degree,
                school=_school(raw),
                year=int(years[-1]) if years else None,
            )
        )
    return parsed


def _school(raw: str) -> str | None:
    """The school name in an education line: prefer a part that looks like one, else the first leftover."""
    text = _HONOURS.sub("", raw)
    for part in (p.strip(" .;()-") for p in re.split(r"[,;]| - ", text)):
        if not _SCHOOL_HINT.search(part):
            continue
        degree = _DEGREE.search(part)
        if degree and degree.start() > 0:
            part = part[: degree.start()]  # "Harvard Law School J.D. 2005 ..." -> "Harvard Law School"
        cleaned = _without_degree_and_year(part)
        if cleaned:
            return cleaned
    leftovers = [_without_degree_and_year(part) for part in re.split(r"[,;]", text)]
    leftovers = [part for part in leftovers if len(part) > 3]
    return leftovers[0] if leftovers else None


def _without_degree_and_year(text: str) -> str:
    return " ".join(_YEAR.sub("", _DEGREE.sub("", text)).split()).strip(" .;()-")


def seniority_year(education: list[Education], seniority: Seniority | None) -> int | None:
    """Year of the earliest degree of the kinds the domain names (e.g. a first law degree)."""
    if seniority is None:
        return None
    kinds = {degree.lower() for degree in seniority.degrees}
    years = [e.year for e in education if e.year and e.degree and e.degree.lower() in kinds]
    return min(years) if years else None


def clean_offices(offices: list[str]) -> list[str]:
    cleaned = []
    for office in offices:
        name = re.sub(r"\s+office$", "", office.strip(), flags=re.I)
        name = re.sub(r"^\d{4,6}\s+|\s+\d{4,6}$", "", name)  # "60329 Frankfurt" -> "Frankfurt"
        if name and name not in cleaned:
            cleaned.append(name)
    return cleaned


def apply_record(
    person: Person,
    record: ProfileRecord,
    domain: DomainConfig,
    now: datetime,
    learned: dict[str, list[str]] | None = None,
) -> Person:
    """Merge a freshly extracted record into a person.

    Missing values never overwrite known ones: if a page temporarily lacks a field (or our
    parser breaks), we keep the last verified value instead of recording a change.
    """
    updated = person.model_copy(deep=True)
    updated.status = "active"
    updated.last_fetched = now
    updated.missing_runs = 0

    def keep(field: str, value: object) -> bool:
        """True if the page had a value for `field`; also records where that value came from."""
        if not value:
            return False
        updated.provenance[field] = Provenance(
            method=record.methods.get(field, "unknown"), source_url=record.url, observed_at=now
        )
        return True

    if keep("name", record.name):
        updated.name = record.name
    if keep("title", record.title):
        updated.title = record.title
        updated.title_norm = normalise_title(record.title, domain.titles)
    if keep("offices", record.offices):
        updated.offices = clean_offices(record.offices)
    if keep("groups", record.groups):
        updated.groups_raw = record.groups
        updated.groups = classify_groups(record.groups, domain.taxonomy, learned)

    updated.segments = segments_for(updated.org_id, updated.title_norm, updated.groups, domain.segments)

    if updated.in_scope:
        if keep("education", record.education):
            updated.education = parse_education(record.education)
            updated.seniority_year = seniority_year(updated.education, domain.seniority)
        for extra in domain.fields:
            values = record.extra.get(extra.key, [])
            if keep(extra.key, values):
                updated.extra[extra.key] = values
        for field in ("email", "phone", "bio"):
            value = getattr(record, field)
            if keep(field, value):
                setattr(updated, field, value)
    else:
        minimise(updated)
    return updated


def minimise(person: Person) -> None:
    """Drop detail we have no purpose for once someone is outside every target segment."""
    person.education = []
    for key in person.extra:
        person.provenance.pop(key, None)
    person.extra = {}
    person.seniority_year = None
    person.email = None
    person.phone = None
    person.bio = None
    for field in _IN_SCOPE_DETAIL:
        person.provenance.pop(field, None)

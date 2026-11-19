"""Normalisation, segment membership, change detection, health checks and move matching."""

from datetime import UTC, datetime

import pytest
from conftest import make_person

from radar.config import DomainConfig
from radar.models import Education, ProfileRecord
from radar.pipeline.diff import diff_person
from radar.pipeline.health import assess_discovery
from radar.pipeline.matching import MOVE_THRESHOLD, POSSIBLE_THRESHOLD, match_score
from radar.pipeline.normalise import (
    apply_record,
    classify_groups,
    clean_offices,
    normalise_title,
    parse_education,
)

NOW = datetime(2026, 11, 18, tzinfo=UTC)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Partner", "partner"),
        ("Partner, Mergers and Acquisitions", "partner"),
        ("Retired Partner", "retired"),
        ("Of Counsel", "counsel"),
        ("Associate", "associate"),
        ("Chief Knowledge Officer", "other"),
        (None, None),
    ],
)
def test_title_normalisation(domain: DomainConfig, title: str | None, expected: str | None) -> None:
    assert normalise_title(title, domain.titles) == expected


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Mergers & Acquisitions", ["mergers-acquisitions"]),
        ("M&A", ["mergers-acquisitions"]),
        ("Private Equity Infrastructure", ["private-equity"]),
        ("Corporate", ["corporate"]),
        ("Corporate Litigation", ["litigation"]),
        ("Banking & Finance", ["finance"]),
        ("Antitrust and Competition", []),
    ],
)
def test_group_classification(domain: DomainConfig, label: str, expected: list[str]) -> None:
    assert classify_groups([label], domain.taxonomy) == expected


def test_education_parsing() -> None:
    education = parse_education(
        [
            "J.D., UCLA School of Law, 2020",
            "University of Illinois Chicago School of Law J.D. magna cum laude 2023",
        ]
    )
    assert [(e.degree, e.school, e.year) for e in education] == [
        ("JD", "UCLA School of Law", 2020),
        ("JD", "University of Illinois Chicago School of Law", 2023),
    ]


def _record(**fields: object) -> ProfileRecord:
    base: dict[str, object] = {
        "url": "https://www.weil.com/people/jane-example",
        "name": "Jane Example",
        "title": "Partner",
        "offices": ["London"],
        "groups": ["Mergers & Acquisitions"],
        "extra": {"admissions": ["New York"]},
        "education": ["J.D., Yale Law School, 2005"],
        "email": "jane@example.com",
    }
    return ProfileRecord(**(base | fields))  # type: ignore[arg-type]


def test_apply_record_puts_partner_in_segment(domain: DomainConfig) -> None:
    person = apply_record(make_person(status="pending"), _record(), domain, NOW)
    assert person.segments == ["corp-ma-partners"]
    assert person.seniority_year == 2005
    assert person.extra == {"admissions": ["New York"]}
    assert person.provenance["title"].source_url.endswith("jane-example")


def test_apply_record_minimises_out_of_scope_people(domain: DomainConfig) -> None:
    person = apply_record(make_person(status="pending"), _record(title="Associate"), domain, NOW)
    assert person.segments == []
    assert person.email is None and person.education == [] and person.extra == {}


def test_missing_fields_never_overwrite_known_values(domain: DomainConfig) -> None:
    before = apply_record(make_person(status="pending"), _record(), domain, NOW)
    after = apply_record(before, _record(title=None, offices=[], groups=[]), domain, NOW)
    assert (after.title, after.offices, after.segments) == ("Partner", ["London"], ["corp-ma-partners"])
    assert diff_person(before, after, "Weil", domain) == []


def test_first_fetch_during_baseline_emits_nothing(domain: DomainConfig) -> None:
    pending = make_person(status="pending", baseline=True)
    assert diff_person(pending, apply_record(pending, _record(), domain, NOW), "Weil", domain) == []


def test_first_fetch_after_baseline_is_a_join(domain: DomainConfig) -> None:
    pending = make_person(status="pending", baseline=False)
    events = diff_person(pending, apply_record(pending, _record(), domain, NOW), "Weil", domain)
    assert [e.type for e in events] == ["joined"]
    assert "Jane Example appeared at Weil (London)" in events[0].summary


def test_promotion_office_and_group_changes(domain: DomainConfig) -> None:
    pending = make_person(status="pending")
    counsel = apply_record(pending, _record(title="Counsel"), domain, NOW)
    partner = apply_record(counsel, _record(offices=["Paris"], groups=["Private Equity"]), domain, NOW)
    types = [e.type for e in diff_person(counsel, partner, "Weil", domain)]
    assert types == ["promoted", "office_changed", "group_changed"]


def test_changes_outside_every_segment_are_not_news(domain: DomainConfig) -> None:
    pending = make_person(status="pending")
    associate = apply_record(pending, _record(title="Associate"), domain, NOW)
    moved = apply_record(associate, _record(title="Associate", offices=["Paris"]), domain, NOW)
    assert diff_person(associate, moved, "Weil", domain) == []


def test_discovery_health() -> None:
    assert assess_discovery(None, 1200) == "ok"
    assert assess_discovery(1200, 1150) == "ok"
    assert assess_discovery(1200, 900) == "quarantined"
    assert assess_discovery(1200, 0) == "broken"


def test_move_matching_needs_corroboration() -> None:
    old = make_person(
        id="a",
        name="Jane A. Example",
        seniority_year=2005,
        education=[Education(raw="x", school="Yale Law School")],
    )
    same = make_person(id="b", org_id="kirkland", name="Jane Example", seniority_year=2005)
    name_only = make_person(id="c", org_id="kirkland", name="Jane Example")
    different = make_person(id="d", org_id="kirkland", name="Jane Exemplar", seniority_year=1990)

    assert match_score(old, same) >= MOVE_THRESHOLD
    assert POSSIBLE_THRESHOLD <= match_score(old, name_only) < MOVE_THRESHOLD
    assert match_score(old, different) < POSSIBLE_THRESHOLD


def test_office_names_drop_postcodes_and_suffixes() -> None:
    assert clean_offices(["60329 Frankfurt", "London Office", "Paris", "Paris"]) == [
        "Frankfurt",
        "London",
        "Paris",
    ]

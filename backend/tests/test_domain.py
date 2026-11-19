"""The domain profile: the same engine tracks footballers at clubs, and admins can't save a broken one."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import respx
from conftest import REPO_CONFIG, make_person
from pydantic import ValidationError

from radar.config import AppConfig, DomainConfig, ListingDiscovery, OrgConfig, load_domain
from radar.db import Database, ensure_indexes
from radar.domain import load_domain as load_stored_domain
from radar.domain import save_domain
from radar.models import ProfileRecord
from radar.pipeline.diff import diff_person
from radar.pipeline.normalise import apply_record
from radar.scraping.extract import extract_profile
from radar.scraping.fetcher import Fetcher
from radar.scraping.sources import discover, listing_links
from radar.settings import Settings
from radar.worker.crawler import sync_config

NOW = datetime(2026, 11, 19, tzinfo=UTC)
CLUB = "https://club.test"


@pytest.fixture(scope="module")
def football() -> DomainConfig:
    return load_domain(REPO_CONFIG, "football")


def test_every_shipped_profile_loads() -> None:
    for path in (REPO_CONFIG / "domains").glob("*.yaml"):
        assert load_domain(REPO_CONFIG, path.stem).segments


def _player(**fields: object) -> ProfileRecord:
    base: dict[str, object] = {
        "url": f"{CLUB}/squad/sam-example",
        "name": "Sam Example",
        "title": "Midfielder",
        "groups": ["Central midfield"],
        "extra": {"nationality": ["Wales"]},
    }
    return ProfileRecord(**(base | fields))  # type: ignore[arg-type]


def test_footballers_use_the_same_pipeline(football: DomainConfig) -> None:
    pending = make_person(status="pending", org_id="club", profile_url=f"{CLUB}/squad/sam-example")
    player = apply_record(pending, _player(), football, NOW)
    assert (player.title_norm, player.groups, player.segments) == ("player", ["midfielder"], ["first-team"])
    assert player.extra == {"nationality": ["Wales"]}
    assert player.seniority_year is None  # the football profile has no seniority proxy

    captain = apply_record(player, _player(title="Club Captain"), football, NOW)
    assert [e.type for e in diff_person(player, captain, "Example FC", football)] == ["promoted"]


def test_events_use_the_domain_vocabulary(football: DomainConfig) -> None:
    pending = make_person(status="pending", org_id="club", profile_url=f"{CLUB}/squad/sam-example")
    midfielder = apply_record(pending, _player(), football, NOW)
    forward = apply_record(midfielder, _player(groups=["Striker"]), football, NOW)
    events = diff_person(midfielder, forward, "Example FC", football)
    assert [e.summary for e in events] == [
        "Sam Example (Example FC) positions changed: midfielder -> forward"
    ]


def test_extra_fields_come_from_the_profile(football: DomainConfig) -> None:
    html = """<html><body><h1>Sam Example</h1><h2>Position</h2><p>Midfielder</p>
      <h2>Nationality</h2><p>Wales</p><h2>Previous clubs</h2><ul><li>Town FC</li><li>City FC</li></ul></body></html>"""
    record = extract_profile(html, f"{CLUB}/squad/sam-example", None, football.fields)
    assert record.groups == ["Midfielder"]
    assert record.extra == {"nationality": ["Wales"], "previous_clubs": ["Town FC", "City FC"]}


def test_profile_validation_catches_typos(football: DomainConfig) -> None:
    data = football.model_dump()
    data["segments"][0]["groups"] = ["midfeilder"]
    with pytest.raises(ValidationError, match="unknown titles/groups: midfeilder"):
        DomainConfig.model_validate(data)
    data = football.model_dump()
    data["taxonomy"]["forward"]["include"] = ["(unclosed"]
    with pytest.raises(ValidationError, match="not a valid regex"):
        DomainConfig.model_validate(data)


SQUAD = """<html><body>
  <a class="player" href="/squad/sam-example">Sam</a>
  <a class="player" href="https://club.test/squad/alex-example/#stats">Alex</a>
  <a class="player" href="/shop/shirt">Shirt</a>
  <a class="next" href="/squad-2">Next</a>
</body></html>"""


def test_listing_links_follow_selectors_and_filters() -> None:
    source = ListingDiscovery(
        urls=[f"{CLUB}/squad"], link_selector="a.player", next_selector="a.next", include="/squad/"
    )
    profiles, next_pages = listing_links(SQUAD, f"{CLUB}/squad", source)
    assert profiles == [f"{CLUB}/squad/sam-example", f"{CLUB}/squad/alex-example"]
    assert next_pages == [f"{CLUB}/squad-2"]


@respx.mock
async def test_listing_discovery_pages_through(tmp_path: Path) -> None:
    respx.get(f"{CLUB}/robots.txt").respond(404)
    respx.get(f"{CLUB}/squad").respond(200, text=SQUAD)
    respx.get(f"{CLUB}/squad-2").respond(200, text='<a class="player" href="/squad/kim-example">Kim</a>')
    source = ListingDiscovery(
        urls=[f"{CLUB}/squad"], link_selector="a.player", next_selector="a.next", include="/squad/"
    )
    fetcher = Fetcher.from_settings(
        Settings(
            scraper_user_agent="TestBot/1.0 (+https://test.invalid)", scraper_min_delay_s=0, data_dir=tmp_path
        )
    )
    try:
        entries = await discover(fetcher, source)
    finally:
        await fetcher.aclose()
    assert sorted(e.url for e in entries) == [
        f"{CLUB}/squad/alex-example",
        f"{CLUB}/squad/kim-example",
        f"{CLUB}/squad/sam-example",
    ]


def test_old_configs_without_a_type_are_sitemaps() -> None:
    org = OrgConfig(
        id="x", name="X", website="https://x.test", discovery={"urls": ["u"], "include": "/people/"}
    )  # type: ignore[arg-type]
    assert org.discovery is not None and org.discovery.type == "sitemap"


async def test_admin_edits_survive_a_redeploy(db: Database, app_config: AppConfig) -> None:
    config = app_config.model_copy(deep=True)
    config.organisations = []
    await sync_config(db, config)
    edited = config.domain.model_copy(deep=True)
    edited.vocabulary.person = "Lawyer"
    await save_domain(db, config.domain_name, edited, "admin@test")

    fresh = app_config.model_copy(deep=True)  # a redeploy re-reads the YAML
    fresh.organisations = []
    await sync_config(db, fresh)
    assert fresh.domain.vocabulary.person == "Lawyer"
    stored = await load_stored_domain(db, config.domain_name)
    assert stored is not None and stored[1]["managed"] is True


async def test_old_people_documents_are_upgraded(db: Database) -> None:
    await db["people"].insert_one(
        {
            "_id": "old",
            "practices": ["corporate"],
            "practices_raw": ["Corporate"],
            "jd_year": 2001,
            "languages": ["French"],
            "admissions": [{"raw": "New York 2002", "jurisdiction": "New York", "year": 2002}],
        }
    )
    await ensure_indexes(db)
    await ensure_indexes(db)  # idempotent
    doc = await db["people"].find_one({"_id": "old"})
    assert doc is not None
    assert (doc["groups"], doc["groups_raw"], doc["seniority_year"]) == (["corporate"], ["Corporate"], 2001)
    assert doc["extra"] == {"languages": ["French"], "admissions": ["New York 2002"]}
    assert "practices" not in doc and "jd_year" not in doc

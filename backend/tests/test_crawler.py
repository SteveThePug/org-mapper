"""End to end over a mocked organisation website and a real MongoDB: crawl, change the site, crawl again."""

import os
import time
from pathlib import Path

import respx
from conftest import make_person

from radar.config import AppConfig, Discovery, OrgConfig
from radar.db import Database
from radar.models import Person, person_id_for
from radar.scraping.fetcher import Fetcher
from radar.scraping.raw_store import RawStore
from radar.settings import Settings
from radar.worker import jobs
from radar.worker.crawler import Crawler, erase_person, link_move, purge_raw, sync_config

A, B = "https://organisation.test", "https://other.test"


def bio(name: str, title: str, office: str, practice: str = "Mergers & Acquisitions", jd: int = 2005) -> str:
    return f"""<html><head><script type="application/ld+json">
    {{"@context": "https://schema.org", "@type": "Person", "name": "{name}", "jobTitle": "{title}",
      "workLocation": {{"@type": "Place", "name": "{office}"}}}}</script></head>
    <body><h1>{name}</h1>
      <section><h2>Practice Areas</h2><ul><li>{practice}</li></ul></section>
      <section><h2>Education</h2><ul><li>J.D., Yale Law School, {jd}</li></ul></section>
    </body></html>"""


def sitemap(site: str, slugs: list[str]) -> str:
    urls = "".join(f"<url><loc>{site}/people/{s}</loc></url>" for s in slugs)
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'


def config(base: AppConfig) -> AppConfig:
    organisations = [
        OrgConfig(
            id=fid,
            name=name,
            website=site,
            discovery=Discovery(urls=[f"{site}/sitemap.xml"], include=rf"^{site}/people/"),
        )
        for fid, name, site in (
            ("organisation-a", "Organisation A", A),
            ("organisation-b", "Organisation B", B),
        )
    ]
    return AppConfig(organisations=organisations, domain=base.domain)


async def drain(crawler: Crawler, db: Database) -> list[str]:
    notes = []
    while job := await jobs.claim(db, set()):
        notes.append(await crawler.fetch_profile(job["payload"]["person_id"], job["payload"]["reason"]))
        await jobs.complete(db, job)
    return notes


async def event_types(db: Database) -> list[str]:
    return sorted([doc["type"] async for doc in db["events"].find()])


@respx.mock
async def test_crawl_detects_joins_changes_departures_and_moves(
    db: Database, app_config: AppConfig, tmp_path: Path
) -> None:
    cfg = config(app_config)
    await sync_config(db, cfg)
    crawler = Crawler(db, Fetcher("TestBot/1.0", min_delay_s=0), cfg, Settings(), RawStore(tmp_path))
    for site in (A, B):
        respx.get(f"{site}/robots.txt").respond(404)
    site_a = respx.get(f"{A}/sitemap.xml")
    site_b = respx.get(f"{B}/sitemap.xml")
    alice = respx.get(f"{A}/people/alice")
    respx.get(f"{A}/people/bob").respond(200, text=bio("Bob Partner", "Partner", "London"))
    respx.get(f"{A}/people/carol").respond(200, text=bio("Carol Associate", "Associate", "London"))
    respx.get(f"{B}/people/zed").respond(200, text=bio("Zed Existing", "Partner", "New York"))

    # Week 1: baseline crawl of both organisations - everyone is known, nothing is "news".
    site_a.respond(200, text=sitemap(A, ["alice", "bob", "carol"]))
    site_b.respond(200, text=sitemap(B, ["zed"]))
    alice.respond(200, text=bio("Alice Counsel", "Counsel", "London"))
    await crawler.discover_org("organisation-a")
    await crawler.discover_org("organisation-b")
    assert await drain(crawler, db) == ["updated (0 events)"] * 4
    assert await event_types(db) == []
    assert await db["people"].count_documents({"segments": "corp-ma-partners"}) == 2  # bob, zed

    # Week 2: Alice made partner, Bob's profile disappears, Dave joins.
    site_a.respond(200, text=sitemap(A, ["alice", "carol", "dave"]))
    alice.respond(200, text=bio("Alice Counsel", "Partner", "London"))
    respx.get(f"{A}/people/dave").respond(200, text=bio("Dave New", "Partner", "London", "Private Equity"))
    await crawler.discover_org("organisation-a")
    for person_id in [p["_id"] async for p in db["people"].find({"name": "Alice Counsel"})]:
        await jobs.enqueue(
            db,
            "fetch",
            f"fetch:{person_id}",
            "organisation-a",
            {"person_id": person_id, "reason": "user"},
            99,
        )
    await drain(crawler, db)
    assert await event_types(db) == ["joined", "promoted"]  # Bob only missing once: not yet "left"

    # Week 3: Bob still missing -> verify his page -> 404 -> left. He shows up at Organisation B.
    respx.get(f"{A}/people/bob").respond(404)
    await crawler.discover_org("organisation-a")
    await drain(crawler, db)
    assert await event_types(db) == ["joined", "left", "promoted"]

    site_b.respond(200, text=sitemap(B, ["zed", "robert"]))
    respx.get(f"{B}/people/robert").respond(200, text=bio("Bob Partner", "Partner", "New York"))
    await crawler.discover_org("organisation-b")
    await drain(crawler, db)
    assert await event_types(db) == ["joined", "joined", "left", "moved", "promoted"]

    moved = await db["events"].find_one({"type": "moved"})
    assert moved is not None and moved["summary"] == "Bob Partner moved from Organisation A to Organisation B"
    new_bob = Person.model_validate(await db["people"].find_one({"_id": moved["person_id"]}))
    assert [h.org_id for h in new_bob.history] == ["organisation-a"]


@respx.mock
async def test_shrunken_sitemap_is_quarantined_not_an_exodus(
    db: Database, app_config: AppConfig, tmp_path: Path
) -> None:
    cfg = config(app_config)
    await sync_config(db, cfg)
    crawler = Crawler(db, Fetcher("TestBot/1.0", min_delay_s=0), cfg, Settings(), RawStore(tmp_path))
    respx.get(f"{A}/robots.txt").respond(404)
    site = respx.get(f"{A}/sitemap.xml")
    slugs = [f"p{i}" for i in range(10)]
    for slug in slugs:
        respx.get(f"{A}/people/{slug}").respond(200, text=bio(f"Person {slug}", "Partner", "London"))
    site.respond(200, text=sitemap(A, slugs))
    await crawler.discover_org("organisation-a")
    await drain(crawler, db)

    site.respond(200, text=sitemap(A, slugs[:3]))  # a half-broken sitemap
    for _ in range(3):
        run = await crawler.discover_org("organisation-a")
        assert run["health"] == "quarantined"
    await drain(crawler, db)
    assert await db["people"].count_documents({"status": "departed"}) == 0
    assert await event_types(db) == []

    # Still 3 after three runs: that's the site's real size now, so the quarantine ends.
    assert (await crawler.discover_org("organisation-a"))["health"] == "ok"


@respx.mock
async def test_erased_person_is_deleted_and_never_collected_again(
    db: Database, app_config: AppConfig, tmp_path: Path
) -> None:
    cfg = config(app_config)
    await sync_config(db, cfg)
    crawler = Crawler(db, Fetcher("TestBot/1.0", min_delay_s=0), cfg, Settings(), RawStore(tmp_path))
    respx.get(f"{A}/robots.txt").respond(404)
    respx.get(f"{A}/sitemap.xml").respond(200, text=sitemap(A, ["alice", "bob"]))
    respx.get(f"{A}/people/alice").respond(200, text=bio("Alice", "Partner", "London"))
    respx.get(f"{A}/people/bob").respond(200, text=bio("Bob", "Partner", "London"))
    await crawler.discover_org("organisation-a")
    await drain(crawler, db)

    counts = await erase_person(db, f"{A}/people/bob", "test")
    assert counts["people"] == 1 and counts["observations"] == 1
    await crawler.discover_org("organisation-a")
    await drain(crawler, db)
    assert [p["name"] async for p in db["people"].find()] == ["Alice"]


@respx.mock
async def test_raw_pages_are_kept_only_in_scope_and_erased_with_the_person(
    db: Database, app_config: AppConfig, tmp_path: Path
) -> None:
    cfg = config(app_config)
    await sync_config(db, cfg)
    store = RawStore(tmp_path)
    crawler = Crawler(db, Fetcher("TestBot/1.0", min_delay_s=0), cfg, Settings(), store)
    respx.get(f"{A}/robots.txt").respond(404)
    respx.get(f"{A}/sitemap.xml").respond(200, text=sitemap(A, ["bob", "carol"]))
    respx.get(f"{A}/people/bob").respond(200, text=bio("Bob", "Partner", "London"))
    respx.get(f"{A}/people/carol").respond(200, text=bio("Carol", "Associate", "London"))
    await crawler.discover_org("organisation-a")
    await drain(crawler, db)

    bob, carol = person_id_for(f"{A}/people/bob"), person_id_for(f"{A}/people/carol")
    bob_obs = await db["observations"].find_one({"person_id": bob})
    carol_obs = await db["observations"].find_one({"person_id": carol})
    assert bob_obs is not None and store.get(bob_obs["raw_sha256"]) is not None
    assert carol_obs is not None and carol_obs["raw_sha256"] is None  # out of scope: no raw page

    counts = await erase_person(db, f"{A}/people/bob", "test", store)
    assert counts["raw_pages"] == 1
    assert store.get(bob_obs["raw_sha256"]) is None


async def test_purge_deletes_unreferenced_and_out_of_scope_raw_pages(db: Database, tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    kept, legacy, orphan, fresh = (store.put(f"<html>{n}</html>".encode()) for n in "abcd")
    day_ago = time.time() - 2 * 86400
    for sha in (kept, legacy, orphan):
        path = tmp_path / sha[:2] / f"{sha}.html.gz"
        os.utime(path, (day_ago, day_ago))
    inside = make_person(id="in", segments=["corp-ma-partners"])
    outside = make_person(id="out", profile_url="https://www.weil.com/people/other")
    await db["people"].insert_many([inside.to_mongo(), outside.to_mongo()])
    await db["observations"].insert_many(
        [
            {"_id": "o1", "person_id": "in", "raw_sha256": kept},
            {"_id": "o2", "person_id": "out", "raw_sha256": legacy},
        ]
    )

    assert await purge_raw(db, store) == 2  # the orphan, and the out-of-scope person's page
    assert store.get(kept) is not None
    assert store.get(fresh) is not None  # unreferenced but recent: its observation may be on the way
    assert store.get(legacy) is None and store.get(orphan) is None
    assert (await db["observations"].find_one({"_id": "o2"}) or {})["raw_sha256"] is None


@respx.mock
async def test_reprocess_reclassifies_out_of_scope_people_from_their_record(
    db: Database, app_config: AppConfig, tmp_path: Path
) -> None:
    cfg = config(app_config)
    await sync_config(db, cfg)
    crawler = Crawler(db, Fetcher("TestBot/1.0", min_delay_s=0), cfg, Settings(), RawStore(tmp_path))
    respx.get(f"{A}/robots.txt").respond(404)
    respx.get(f"{A}/sitemap.xml").respond(200, text=sitemap(A, ["carol"]))
    respx.get(f"{A}/people/carol").respond(200, text=bio("Carol", "Associate", "London"))
    await crawler.discover_org("organisation-a")
    await drain(crawler, db)
    carol = person_id_for(f"{A}/people/carol")

    # Nothing changed: re-classified from the stored record, still out of scope, no refetch.
    counts = await crawler.reprocess_org("organisation-a")
    assert counts["from_record"] == 1 and counts["missing_raw"] == 0 and counts["now_in_scope"] == 0

    # Stand-in for a taxonomy/segment edit: the stored record now classifies her into a segment.
    await db["observations"].update_one({"person_id": carol}, {"$set": {"record.title": "Partner"}})
    counts = await crawler.reprocess_org("organisation-a")
    assert counts["now_in_scope"] == 1
    person = await db["people"].find_one({"_id": carol})
    assert person is not None and person["segments"] and person["record_hash"] is None
    assert await db["jobs"].count_documents({"key": f"fetch:{carol}", "status": "queued"}) == 1


async def test_erasure_also_removes_linked_moves(db: Database) -> None:
    old_url, new_url = f"{A}/people/bob", f"{B}/people/robert"
    old_id, new_id = person_id_for(old_url), person_id_for(new_url)
    old = make_person(
        id=old_id, org_id="organisation-a", profile_url=old_url, moved_to=new_id, status="departed"
    )
    new = make_person(id=new_id, org_id="organisation-b", profile_url=new_url, moved_from=old_id)
    await db["people"].insert_many([old.to_mongo(), new.to_mongo()])
    await link_move(db, old, new)
    await db["events"].insert_one(
        {"_id": "e1", "type": "moved", "person_id": new_id, "related_person_id": old_id}
    )

    await erase_person(db, old_url, "test")

    assert await db["events"].count_documents({}) == 0
    remaining = await db["people"].find_one({"_id": new_id})
    assert remaining is not None and remaining["history"] == [] and remaining["moved_from"] is None
    assert await db["suppressions"].count_documents({}) == 2  # the https and http forms


@respx.mock
async def test_a_single_404_is_checked_again_before_reporting_a_departure(
    db: Database, app_config: AppConfig, tmp_path: Path
) -> None:
    cfg = config(app_config)
    await sync_config(db, cfg)
    crawler = Crawler(db, Fetcher("TestBot/1.0", min_delay_s=0), cfg, Settings(), RawStore(tmp_path))
    respx.get(f"{A}/robots.txt").respond(404)
    respx.get(f"{A}/sitemap.xml").respond(200, text=sitemap(A, ["bob"]))
    page = respx.get(f"{A}/people/bob")
    page.respond(200, text=bio("Bob Partner", "Partner", "London"))
    await crawler.discover_org("organisation-a")
    await drain(crawler, db)
    pid = person_id_for(f"{A}/people/bob")

    page.respond(404)  # a glitch, or a real departure?
    assert (await crawler.fetch_profile(pid, "stale")).startswith("HTTP 404: verifying")
    assert await event_types(db) == []
    retry = await db["jobs"].find_one({"key": f"fetch:{pid}", "status": "queued"})
    assert retry is not None and retry["payload"]["reason"] == "verify"

    assert await crawler.fetch_profile(pid, "verify") == "departed"
    assert await event_types(db) == ["left"]


@respx.mock
async def test_a_broken_first_discovery_does_not_end_the_baseline(
    db: Database, app_config: AppConfig, tmp_path: Path
) -> None:
    cfg = config(app_config)
    await sync_config(db, cfg)
    crawler = Crawler(db, Fetcher("TestBot/1.0", min_delay_s=0), cfg, Settings(), RawStore(tmp_path))
    respx.get(f"{A}/robots.txt").respond(404)
    site = respx.get(f"{A}/sitemap.xml")
    respx.get(f"{A}/people/alice").respond(200, text=bio("Alice", "Partner", "London"))

    site.respond(200, text=sitemap(A, []))  # misconfigured / broken: nothing found
    assert (await crawler.discover_org("organisation-a"))["health"] == "broken"
    site.respond(200, text=sitemap(A, ["alice"]))
    assert (await crawler.discover_org("organisation-a"))["baseline"] is True
    await drain(crawler, db)
    assert await event_types(db) == []


@respx.mock
async def test_backfill_reconstructs_history_from_the_archive(
    db: Database, app_config: AppConfig, tmp_path: Path
) -> None:
    from radar.backfill import WAYBACK, Backfiller

    cfg = config(app_config)
    await sync_config(db, cfg)
    crawler = Crawler(db, Fetcher("TestBot/1.0", min_delay_s=0), cfg, Settings(), RawStore(tmp_path))
    respx.get(f"{A}/robots.txt").respond(404)
    respx.get(f"{WAYBACK}/robots.txt").respond(404)
    # Today: Alice (partner) and Carol (partner). Six months ago: Alice (counsel) and Bob (partner).
    respx.get(f"{A}/sitemap.xml").respond(200, text=sitemap(A, ["alice", "carol"]))
    respx.get(f"{A}/people/alice").respond(200, text=bio("Alice Smith", "Partner", "London"))
    respx.get(f"{A}/people/carol").respond(200, text=bio("Carol Jones", "Partner", "London"))
    await crawler.discover_org("organisation-a")
    await drain(crawler, db)
    assert await event_types(db) == []  # baseline

    snap = "20260518000000"
    respx.get(url__startswith=f"{WAYBACK}/cdx/").respond(200, json=[["urlkey", "timestamp"], ["x", snap]])
    old = f"{WAYBACK}/web/{snap}id_/{A}"
    respx.get(f"{old}/sitemap.xml").respond(200, text=sitemap(A, ["alice", "bob"]))
    respx.get(f"{old}/people/alice").respond(200, text=bio("Alice Smith", "Counsel", "London"))
    respx.get(f"{old}/people/bob").respond(200, text=bio("Bob Brown", "Partner", "London"))

    backfiller = Backfiller(crawler)
    crawler.fetcher.set_host_delay(WAYBACK, 0)
    result = await backfiller.run("organisation-a", months=6)

    assert "skipped" not in result, result
    assert (result["joined"], result["left"], result["promoted"]) == (1, 1, 1)
    assert await event_types(db) == ["joined", "left", "promoted"]
    assert await db["events"].count_documents({"source": "archive"}) == 3
    left = await db["events"].find_one({"type": "left"})
    assert left is not None and left["evidence_url"].startswith(f"{WAYBACK}/web/{snap}/")
    assert (await backfiller.run("organisation-a"))["skipped"].startswith("already backfilled")
    # A forced re-run (or one resuming after an archive outage) adds no duplicate events.
    await backfiller.run("organisation-a", months=6, force=True)
    assert await event_types(db) == ["joined", "left", "promoted"]


async def _departure_at_a(crawler: Crawler, db: Database, slug: str = "bob") -> None:
    """Organisation A's baseline has Bob; on the next crawls his page is gone (-> "left")."""
    site = respx.get(f"{A}/sitemap.xml")
    # Enough colleagues that one missing profile isn't quarantined as a broken sitemap.
    others = [f"keep{i}" for i in range(9)]
    site.respond(200, text=sitemap(A, [slug, *others]))
    respx.get(f"{A}/people/{slug}").respond(200, text=bio("Bob Partner", "Partner", "London"))
    for other in others:
        respx.get(f"{A}/people/{other}").respond(200, text=bio(f"Kim {other}", "Associate", "London"))
    await crawler.discover_org("organisation-a")
    await drain(crawler, db)
    site.respond(200, text=sitemap(A, others))
    respx.get(f"{A}/people/{slug}").respond(404)
    for _ in range(2):
        await crawler.discover_org("organisation-a")
        await drain(crawler, db)


def _crawler(db: Database, app_config: AppConfig, tmp_path: Path) -> Crawler:
    for site in (A, B):
        respx.get(f"{site}/robots.txt").respond(404)
    return Crawler(
        db, Fetcher("TestBot/1.0", min_delay_s=0), config(app_config), Settings(), RawStore(tmp_path)
    )


@respx.mock
async def test_a_move_is_found_when_the_new_organisation_is_first_crawled_after_the_departure(
    db: Database, app_config: AppConfig, tmp_path: Path
) -> None:
    await sync_config(db, config(app_config))
    crawler = _crawler(db, app_config, tmp_path)
    await _departure_at_a(crawler, db)
    assert await event_types(db) == ["left"]

    # Organisation B is only now tracked: Bob is in its baseline, so we can't prove he arrived recently.
    respx.get(f"{B}/sitemap.xml").respond(200, text=sitemap(B, ["robert"]))
    respx.get(f"{B}/people/robert").respond(200, text=bio("Bob Partner", "Partner", "New York"))
    await crawler.discover_org("organisation-b")
    await drain(crawler, db)
    assert await event_types(db) == ["left", "possible_move"]  # a recruiter confirms it

    # Asking again (a re-match, or another first read) never suggests the same pair twice.
    assert await crawler.rematch_departures() == 0
    assert await event_types(db) == ["left", "possible_move"]

    # Later, evidence that he arrived recently (a backfill of B) upgrades the suggestion in place.
    new_bob = Person.model_validate(await db["people"].find_one({"profile_url": f"{B}/people/robert"}))
    new_bob.baseline = False
    assert await crawler.match_moves(new_bob, joined=True, source="archive")
    assert await event_types(db) == ["left", "moved"]
    moved = await db["events"].find_one({"type": "moved"})
    assert moved is not None and moved["summary"] == "Bob Partner moved from Organisation A to Organisation B"
    linked = await db["people"].find_one({"_id": new_bob.id})
    assert linked is not None and linked["moved_from"] is not None


@respx.mock
async def test_a_move_is_found_when_the_departure_is_seen_after_the_baseline_arrival(
    db: Database, app_config: AppConfig, tmp_path: Path
) -> None:
    await sync_config(db, config(app_config))
    crawler = _crawler(db, app_config, tmp_path)
    respx.get(f"{B}/sitemap.xml").respond(200, text=sitemap(B, ["robert"]))
    respx.get(f"{B}/people/robert").respond(200, text=bio("Bob Partner", "Partner", "New York"))
    await crawler.discover_org("organisation-b")
    await drain(crawler, db)

    await _departure_at_a(crawler, db)
    assert await event_types(db) == ["left", "possible_move"]


@respx.mock
async def test_a_partner_who_moves_to_an_untracked_title_is_still_a_move(
    db: Database, app_config: AppConfig, tmp_path: Path
) -> None:
    await sync_config(db, config(app_config))
    crawler = _crawler(db, app_config, tmp_path)
    await _departure_at_a(crawler, db)
    # B's baseline: Bob is now counsel there - outside every target segment.
    respx.get(f"{B}/sitemap.xml").respond(200, text=sitemap(B, ["robert"]))
    respx.get(f"{B}/people/robert").respond(200, text=bio("Bob Partner", "Counsel", "New York"))
    await crawler.discover_org("organisation-b")
    await drain(crawler, db)

    assert await event_types(db) == ["left", "possible_move"]
    move = await db["events"].find_one({"type": "possible_move"})
    # Filed under the segment he left, so filtering the feed by it still shows the move.
    assert move is not None and move["segments"] == ["corp-ma-partners"]


@respx.mock
async def test_backfill_cut_short_by_the_page_budget_resumes_where_it_stopped(
    db: Database, app_config: AppConfig, tmp_path: Path
) -> None:
    from radar.backfill import WAYBACK, Backfiller

    cfg = config(app_config)
    await sync_config(db, cfg)
    crawler = _crawler(db, app_config, tmp_path)
    respx.get(f"{WAYBACK}/robots.txt").respond(404)
    respx.get(f"{A}/sitemap.xml").respond(200, text=sitemap(A, ["carol"]))
    respx.get(f"{A}/people/carol").respond(200, text=bio("Carol Jones", "Partner", "London"))
    await crawler.discover_org("organisation-a")
    await drain(crawler, db)

    snap = "20260518000000"
    respx.get(url__startswith=f"{WAYBACK}/cdx/").respond(200, json=[["urlkey", "timestamp"], ["x", snap]])
    old = f"{WAYBACK}/web/{snap}id_/{A}"
    respx.get(f"{old}/sitemap.xml").respond(200, text=sitemap(A, ["carol", "bob", "dan"]))
    respx.get(f"{old}/people/bob").respond(200, text=bio("Bob Brown", "Partner", "London"))
    respx.get(f"{old}/people/dan").respond(200, text=bio("Dan Green", "Partner", "London"))
    respx.get(f"{old}/people/carol").respond(200, text=bio("Carol Jones", "Partner", "London"))

    backfiller = Backfiller(crawler)
    crawler.fetcher.set_host_delay(WAYBACK, 0)
    first = await backfiller.run("organisation-a", months=6, max_pages=1)
    assert (first["left"], first["remaining"]) == (1, 2)  # Dan's page, and Carol's promotion check
    org = await db["organisations"].find_one({"_id": "organisation-a"})
    assert org is not None and "backfilled_at" not in org  # unfinished: not marked done

    second = await backfiller.run("organisation-a", months=6, max_pages=2)
    assert (second["left"], second["remaining"]) == (1, 0)
    assert await event_types(db) == ["left", "left"]
    assert (await backfiller.run("organisation-a"))["skipped"].startswith("already backfilled")

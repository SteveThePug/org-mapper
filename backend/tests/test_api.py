"""The HTTP API over a real (throwaway) Mongo: signing in, lockout, and who may call what."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from conftest import make_person
from test_gateway import FakeCompletions, gateway

from radar import auth
from radar import db as db_module
from radar.api import admin
from radar.api.main import app
from radar.auth import create_user
from radar.db import Database
from radar.models import person_id_for
from radar.scraping.fetcher import Fetcher
from radar.settings import get_settings
from radar.worker import heartbeat
from radar.worker.crawler import erase_person

PASSWORD = "correct horse battery staple"


@pytest.fixture
async def client(db: Database, monkeypatch: Any) -> AsyncIterator[httpx.AsyncClient]:
    # Point the app's get_db() at the test database.
    monkeypatch.setattr(db_module, "_client", db.client)
    monkeypatch.setattr(get_settings(), "mongo_db", db.name)
    await create_user(db, "admin@x.test", PASSWORD, "admin")
    await create_user(db, "rec@x.test", PASSWORD, "recruiter")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def login(http: httpx.AsyncClient, email: str, password: str = PASSWORD) -> httpx.Response:
    return await http.post("/api/auth/login", json={"email": email, "password": password})


async def test_sign_in_and_out(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/auth/me")).status_code == 401
    assert (await login(client, "rec@x.test", "wrong")).status_code == 401
    response = await login(client, "rec@x.test")
    assert response.status_code == 200 and response.json()["role"] == "recruiter"
    assert (await client.get("/api/auth/me")).json()["email"] == "rec@x.test"
    await client.post("/api/auth/logout")
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_repeated_failures_lock_the_account(client: httpx.AsyncClient) -> None:
    for _ in range(auth.MAX_FAILED_LOGINS):
        assert (await login(client, "rec@x.test", "wrong")).status_code == 401
    # Locked, even with the right password.
    assert (await login(client, "rec@x.test")).status_code == 429


async def test_overlong_password_is_rejected_before_hashing(client: httpx.AsyncClient) -> None:
    assert (await login(client, "rec@x.test", "x" * 10_000)).status_code == 422


async def test_recruiters_get_403_from_every_admin_route(client: httpx.AsyncClient) -> None:
    await login(client, "rec@x.test")
    admin_routes = [
        (method.upper(), path)
        for path, methods in app.openapi()["paths"].items()
        if path.startswith("/api/admin/")
        for method in methods
    ]
    assert admin_routes
    for method, path in admin_routes:
        url = path.replace("{org_id}", "some-org")
        response = await client.request(method, url, json={})
        assert response.status_code == 403, (method, path, response.status_code)


async def test_a_demoted_admin_loses_access_at_once(client: httpx.AsyncClient, db: Database) -> None:
    await login(client, "admin@x.test")
    assert (await client.get("/api/admin/settings")).status_code == 200
    await db["users"].update_one({"_id": "admin@x.test"}, {"$set": {"role": "recruiter"}})
    assert (await client.get("/api/admin/settings")).status_code == 403
    await db["users"].delete_one({"_id": "admin@x.test"})
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_notes_add_edit_delete_and_who_may_change_them(client: httpx.AsyncClient, db: Database) -> None:
    await db["people"].insert_one(make_person().to_mongo())
    await login(client, "rec@x.test")
    assert (await client.post("/api/people/p1/notes", json={"text": "   "})).status_code == 422
    assert (await client.post("/api/people/nobody/notes", json={"text": "hi"})).status_code == 404

    added = await client.post("/api/people/p1/notes", json={"text": " Open to a move in spring. "})
    assert added.status_code == 201
    note = added.json()
    assert note["text"] == "Open to a move in spring." and note["author"] == "rec@x.test" and note["can_edit"]

    edited = await client.patch(
        f"/api/notes/{note['id']}", json={"text": "Open to a move.", "use_in_brief": False}
    )
    assert edited.json()["text"] == "Open to a move." and edited.json()["use_in_brief"] is False

    profile = (await client.get("/api/people/p1")).json()
    assert [n["text"] for n in profile["notes"]] == ["Open to a move."]

    # Another recruiter can read it but not change it; an admin can.
    await create_user(db, "rec2@x.test", PASSWORD, "recruiter")
    await login(client, "rec2@x.test")
    assert (await client.get("/api/people/p1")).json()["notes"][0]["can_edit"] is False
    assert (await client.delete(f"/api/notes/{note['id']}")).status_code == 403
    await login(client, "admin@x.test")
    assert (await client.delete(f"/api/notes/{note['id']}")).status_code == 204
    assert (await client.get("/api/people/p1")).json()["notes"] == []


async def test_erasure_deletes_notes(db: Database) -> None:
    url = "https://www.weil.com/people/jane-example"
    person = make_person(id=person_id_for(url), profile_url=url)
    await db["people"].insert_one(person.to_mongo())
    await db["notes"].insert_one({"_id": "n1", "person_id": person.id, "text": "x", "author": "a"})
    counts = await erase_person(db, person.profile_url, "test")
    assert counts["notes"] == 1 and await db["notes"].count_documents({}) == 0


async def test_people_list_shows_note_counts_and_notes_load_on_their_own(
    client: httpx.AsyncClient, db: Database
) -> None:
    await db["people"].insert_one(make_person(name="Jane Example", segments=[]).to_mongo())
    await login(client, "rec@x.test")
    for text in ("First call", "Second call"):
        await client.post("/api/people/p1/notes", json={"text": text})
    people = (await client.get("/api/people", params={"segment": "all"})).json()["items"]
    assert [(p["id"], p["note_count"]) for p in people] == [("p1", 2)]
    notes = (await client.get("/api/people/p1/notes")).json()
    assert [n["text"] for n in notes] == ["Second call", "First call"]


async def test_ai_setup_suggests_an_org_without_saving_it(
    client: httpx.AsyncClient, db: Database, monkeypatch: Any
) -> None:
    site = "https://club.test"
    answer = {
        "name": "Club FC",
        "kind": "club",
        "discovery": {
            "type": "sitemap",
            "urls": [f"{site}/sitemap.xml"],
            "include": r"^https://club\.test/players/[^/]+$",
        },
        "selectors": {},
        "notes": "Players are listed in the sitemap.",
    }
    monkeypatch.setattr(admin, "gateway", lambda: gateway(db, FakeCompletions(content=json.dumps(answer))))
    monkeypatch.setattr(admin, "_fetcher", lambda: Fetcher("TestBot/1.0", min_delay_s=0))
    await login(client, "admin@x.test")
    locs = "".join(f"<url><loc>{site}/players/p{i}</loc></url>" for i in range(3))
    with respx.mock(base_url=site, assert_all_called=False) as mock:
        mock.get("/robots.txt").respond(404)
        mock.get("/").respond(200, text='<a href="/players">Players</a>')
        mock.get("/sitemap.xml").respond(
            200, text=f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>'
        )
        mock.get(url__regex=r"/players/p\d").respond(
            200, text="<html><body><h1>Pat Player</h1></body></html>"
        )
        response = await client.post("/api/admin/orgs/ai-setup", json={"query": "club.test"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["suggestion"]["name"] == "Club FC"
    assert body["suggestion"]["discovery"]["include"].startswith("^https://club")
    assert body["survey"]["configured_count"] == 3
    assert await db["organisations"].find_one({"_id": "club-fc"}) is None  # nothing saved


async def test_ai_setup_rejects_a_made_up_website(
    client: httpx.AsyncClient, db: Database, monkeypatch: Any
) -> None:
    answer = {"name": "Someone", "website": "not sure"}
    monkeypatch.setattr(admin, "gateway", lambda: gateway(db, FakeCompletions(content=json.dumps(answer))))
    monkeypatch.setattr(admin, "_fetcher", lambda: Fetcher("TestBot/1.0", min_delay_s=0))
    await login(client, "admin@x.test")
    response = await client.post("/api/admin/orgs/ai-setup", json={"query": "Some Club Name"})
    assert response.status_code == 422
    assert "type its address" in response.json()["detail"]


async def test_worker_page_shows_queue_running_jobs_and_stale_workers(
    client: httpx.AsyncClient, db: Database
) -> None:
    now = datetime.now(UTC)
    await db["people"].insert_one({"_id": "p1", "name": "Jane Example", "org_id": "weil"})
    base = {"org_id": "weil", "priority": 50, "attempts": 1, "created_at": now, "payload": {}}
    await db["jobs"].insert_many(
        [
            base | {"_id": "q1", "key": "a", "kind": "fetch", "status": "queued", "run_after": now},
            base
            | {
                "_id": "q2",
                "key": "b",
                "kind": "fetch",
                "status": "queued",
                "run_after": now + timedelta(hours=1),
                "error": "HTTP 500",
            },
            base
            | {
                "_id": "r1",
                "key": "c",
                "kind": "fetch",
                "status": "running",
                "started_at": now,
                "payload": {"person_id": "p1", "reason": "new"},
            },
            base
            | {
                "_id": "d1",
                "key": "d",
                "kind": "discover",
                "status": "done",
                "finished_at": now,
                "note": "health=ok",
            },
            base
            | {
                "_id": "f1",
                "key": "e",
                "kind": "fetch",
                "status": "failed",
                "finished_at": now - timedelta(minutes=1),
                "error": "HTTP 404",
            },
        ]
    )
    await heartbeat.beat(
        db,
        "alive:1",
        now,
        "running",
        lanes=[{"lane": 0, "job_id": "r1", "org_id": "weil", "person_id": "p1"}, {"lane": 1}],
    )
    await db["workers"].insert_one(
        {
            "_id": "gone:2",
            "started_at": now - timedelta(hours=1),
            "last_seen": now - timedelta(minutes=5),
            "state": "running",
            "lanes": [],
            "paused_orgs": [],
        }
    )
    await login(client, "admin@x.test")

    status = (await client.get("/api/admin/worker")).json()
    assert {w["id"]: w["status"] for w in status["workers"]} == {"alive:1": "running", "gone:2": "stale"}
    queue = {row["kind"]: row for row in status["queue"]}
    assert queue["fetch"] | {"kind": "fetch"} == {
        "kind": "fetch",
        "due": 1,
        "waiting": 1,
        "running": 1,
        "done_24h": 0,
        "failed_24h": 1,
    }
    assert queue["discover"]["done_24h"] == 1
    alive = next(w for w in status["workers"] if w["id"] == "alive:1")
    assert (alive["lanes"][0]["person_name"], alive["lanes"][0]["org_name"]) == (
        "Jane Example",
        "Weil, Gotshal & Manges",
    )
    assert [(r["org_id"], r["due"], r["waiting"], r["running"]) for r in status["by_org"]] == [
        ("weil", 1, 1, 1)
    ]
    assert [(j["id"], j["person_name"], j["reason"]) for j in status["running"]] == [
        ("r1", "Jane Example", "new")
    ]

    log = (await client.get("/api/admin/jobs")).json()
    assert [j["id"] for j in log] == ["d1", "f1"]  # finished jobs, newest first
    failed = (await client.get("/api/admin/jobs", params={"status": "failed", "kind": "fetch"})).json()
    assert [j["error"] for j in failed] == ["HTTP 404"]
    assert (await client.get("/api/admin/jobs", params={"org": "nobody"})).json() == []


async def test_job_actions_and_their_refusals(client: httpx.AsyncClient, db: Database) -> None:
    now = datetime.now(UTC)
    base = {
        "org_id": "weil",
        "kind": "fetch",
        "priority": 20,
        "attempts": 3,
        "created_at": now,
        "payload": {},
    }
    await db["jobs"].insert_many(
        [
            base | {"_id": "q1", "key": "a", "status": "queued", "run_after": now + timedelta(hours=1)},
            base | {"_id": "f1", "key": "b", "status": "failed", "finished_at": now, "error": "HTTP 500"},
        ]
    )
    await login(client, "admin@x.test")

    ran = (await client.post("/api/admin/jobs/q1/run-now")).json()
    assert ran["priority"] == 100
    assert (await client.post("/api/admin/jobs/q1/cancel")).json()["status"] == "cancelled"
    assert (await client.post("/api/admin/jobs/q1/cancel")).status_code == 409  # already cancelled
    retried = (await client.post("/api/admin/jobs/f1/retry")).json()
    assert (retried["status"], retried["attempts"], retried["error"]) == ("queued", 0, None)
    assert (await client.post("/api/admin/jobs/nope/retry")).status_code == 404

    kinds = [e["kind"] async for e in db["usage_events"].find({"user": "admin@x.test"}, {"kind": 1})]
    assert {"job_run_now", "job_cancel", "job_retry"} <= set(kinds)


async def test_worker_events_and_throughput(client: httpx.AsyncClient, db: Database) -> None:
    now = datetime.now(UTC)
    await db["worker_events"].insert_many(
        [
            {
                "_id": "e1",
                "ts": now - timedelta(seconds=2),
                "level": "INFO",
                "levelno": 20,
                "logger": "radar.worker",
                "message": "worker started",
                "worker_id": "h:1",
            },
            {
                "_id": "e2",
                "ts": now,
                "level": "ERROR",
                "levelno": 40,
                "logger": "radar.worker",
                "message": "lane 0: queue error",
                "traceback": "Traceback ...",
                "worker_id": "h:1",
            },
        ]
    )
    base = {
        "org_id": "weil",
        "kind": "fetch",
        "priority": 20,
        "attempts": 1,
        "created_at": now,
        "payload": {},
    }
    await db["jobs"].insert_many(
        [
            base | {"_id": "d1", "key": "a", "status": "done", "finished_at": now},
            base | {"_id": "d2", "key": "b", "status": "done", "finished_at": now - timedelta(hours=3)},
            base | {"_id": "f1", "key": "c", "status": "failed", "finished_at": now},
            base | {"_id": "old", "key": "d", "status": "done", "finished_at": now - timedelta(days=2)},
        ]
    )
    await login(client, "admin@x.test")

    events = (await client.get("/api/admin/worker/events")).json()
    assert [e["id"] for e in events] == ["e2", "e1"]  # newest first
    errors = (await client.get("/api/admin/worker/events", params={"level": "error"})).json()
    assert [e["message"] for e in errors] == ["lane 0: queue error"]

    hours = (await client.get("/api/admin/worker")).json()["throughput"]
    assert len(hours) == 24
    assert (hours[-1]["done"], hours[-1]["failed"]) == (1, 1)
    assert sum(h["done"] for h in hours) == 2  # the two-day-old job is outside the window

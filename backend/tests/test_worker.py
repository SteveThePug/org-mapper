"""The worker keeps running when one scheduler tick fails."""

import asyncio
import logging
from typing import Any

from radar.db import Database
from radar.worker import runner as worker_module
from radar.worker.events import EventBuffer
from radar.worker.runner import Worker


async def test_scheduler_survives_a_failing_tick(db: Database, monkeypatch: Any) -> None:
    w = Worker(db, crawler=None, lanes=0)  # type: ignore[arg-type]
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            raise RuntimeError("mongo blip")
        w.stop.set()

    monkeypatch.setattr(w, "_tick", tick)
    monkeypatch.setattr(worker_module, "SCHEDULER_TICK_S", 0)
    await asyncio.wait_for(w._scheduler(), timeout=5)
    assert ticks == 2


async def test_heartbeat_reports_what_each_lane_is_doing(db: Database) -> None:
    w = Worker(db, crawler=None, lanes=2, user_agent="TestBot/1.0")  # type: ignore[arg-type]
    w.lane_jobs[1] = {"job_id": "j1", "kind": "fetch", "org_id": "weil", "person_id": "p1"}
    w.paused_orgs = {"wachtell"}
    await w.beat("running")
    doc = await db["workers"].find_one({"_id": w.id})
    assert doc is not None
    assert doc["state"] == "running"
    assert doc["lanes"] == [
        {"lane": 0},
        {"lane": 1, "job_id": "j1", "kind": "fetch", "org_id": "weil", "person_id": "p1"},
    ]
    assert doc["paused_orgs"] == ["wachtell"]
    assert doc["user_agent"] == "TestBot/1.0"


def _crash() -> None:
    raise RuntimeError("boom")


async def test_worker_log_lines_are_saved_for_the_worker_page(db: Database) -> None:
    events = EventBuffer("host:1")
    logger = logging.getLogger("radar.worker.test")
    logger.addHandler(events)
    logger.setLevel(logging.INFO)
    try:
        logger.info("discovery weil: ok")
        try:
            _crash()
        except RuntimeError:
            logger.exception("lane %d: job %s crashed", 2, "j1")
        events.emit(logging.LogRecord("httpx", logging.WARNING, "", 0, "retrying", None, None))
    finally:
        logger.removeHandler(events)
    events.emit(
        logging.LogRecord("httpx", logging.INFO, "", 0, "GET /", None, None)
    )  # library chatter: dropped
    await events.save(db)
    assert not events.pending
    saved = await db["worker_events"].find().sort("ts", 1).to_list()
    assert [(e["level"], e["message"]) for e in saved] == [
        ("INFO", "discovery weil: ok"),
        ("ERROR", "lane 2: job j1 crashed"),
        ("WARNING", "retrying"),
    ]
    assert "RuntimeError: boom" in saved[1]["traceback"]
    assert all(e["worker_id"] == "host:1" for e in saved)

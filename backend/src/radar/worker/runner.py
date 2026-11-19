"""The background worker: one process, a few concurrent "lanes", a Mongo-backed queue.

- The scheduler loop queues a sitemap discovery per organisation once a day (cheap: a few requests).
- Lanes claim jobs; a lane never takes a job for an organisation another lane is busy with, so each
  organisation is crawled by at most one lane at a time and per-host throttling stays simple.
- Blocked organisations (403/429) are paused for 24 hours rather than retried aggressively.

Scaling out = more worker containers. Throttling is per process, so with several workers you
would shard organisations between them (or move the per-host clock into Mongo).
"""

import asyncio
import contextlib
import logging
import signal
from datetime import UTC, datetime, timedelta
from typing import Any

from radar.config import get_config
from radar.db import Database, ensure_indexes, get_db
from radar.domain import refresh_domain
from radar.llm.classify import LabelClassifier
from radar.llm.gateway import LlmGateway
from radar.orgs import refresh_orgs
from radar.scraping.fetcher import Blocked, CrawlerNotIdentified, Fetcher
from radar.scraping.raw_store import RawStore
from radar.settings import get_settings
from radar.worker import heartbeat, jobs
from radar.worker.crawler import Crawler, FetchError, purge_raw, sync_config
from radar.worker.events import EventBuffer

log = logging.getLogger("radar.worker")

SCHEDULER_TICK_S = 60
IDLE_SLEEP_S = 3
RAW_PURGE_EVERY = timedelta(days=1)


class Worker:
    def __init__(
        self,
        db: Database,
        crawler: Crawler,
        lanes: int,
        user_agent: str | None = None,
        events: EventBuffer | None = None,
    ) -> None:
        self.db = db
        self.events = events
        self.crawler = crawler
        self.lanes = lanes
        self.user_agent = user_agent
        self.id = heartbeat.worker_id()
        self.started_at = datetime.now(UTC)
        # What each lane is doing right now (None = idle), copied into every heartbeat.
        self.lane_jobs: dict[int, dict[str, Any] | None] = dict.fromkeys(range(lanes))
        self.busy_orgs: set[str] = set()
        self.paused_orgs: set[str] = set()
        self.stop = asyncio.Event()
        # Claim + mark-busy must be atomic across lanes, or several lanes grab the same organisation at once.
        self._claim_lock = asyncio.Lock()
        self._last_raw_purge: datetime | None = None  # None = purge on the first tick

    async def run(self) -> None:
        await asyncio.gather(
            self._scheduler(), self._heartbeat(), *(self._lane(i) for i in range(self.lanes))
        )

    async def _heartbeat(self) -> None:
        while not self.stop.is_set():
            try:
                await self.beat("running")
                if self.events:
                    await self.events.save(self.db)
            except Exception:
                log.exception("heartbeat failed")
            await _wait(self.stop, heartbeat.HEARTBEAT_S)

    async def beat(self, state: heartbeat.WorkerState) -> None:
        lanes = [{"lane": lane, **(current or {})} for lane, current in sorted(self.lane_jobs.items())]
        await heartbeat.beat(
            self.db,
            self.id,
            self.started_at,
            state,
            lanes=lanes,
            paused_orgs=sorted(self.paused_orgs),
            user_agent=self.user_agent,
        )

    async def _scheduler(self) -> None:
        while not self.stop.is_set():
            # One bad tick (a Mongo blip, an invalid admin edit) must not stop the worker: log it and
            # try again next tick.
            try:
                await self._tick()
            except Exception:
                log.exception("scheduler tick failed")
            await _wait(self.stop, SCHEDULER_TICK_S)

    async def _tick(self) -> None:
        now = datetime.now(UTC)
        # Pick up organisations and domain-profile edits made in the admin panel since the last tick.
        await refresh_orgs(self.db, self.crawler.config)
        await refresh_domain(self.db, self.crawler.config)
        self.paused_orgs = {
            doc["_id"]
            async for doc in self.db["organisations"].find(
                {"status": "blocked", "blocked_until": {"$gt": now}}, {"_id": 1}
            )
        }
        due = self.db["organisations"].find(
            {
                "configured": True,
                "enabled": True,
                "_id": {"$nin": sorted(self.paused_orgs)},
                "$or": [{"next_discovery_at": None}, {"next_discovery_at": {"$lte": now}}],
            }
        )
        async for org in due:
            await jobs.enqueue(
                self.db, "discover", f"discover:{org['_id']}", org["_id"], {}, jobs.PRIORITY_DISCOVER
            )
        requeued = await jobs.requeue_expired(self.db)
        if requeued:
            log.warning("requeued %d jobs with expired leases", requeued)
        if self._last_raw_purge is None or now - self._last_raw_purge >= RAW_PURGE_EVERY:
            self._last_raw_purge = now
            await purge_raw(self.db, self.crawler.raw_store)

    async def _lane(self, lane: int) -> None:
        while not self.stop.is_set():
            try:
                await self._run_one(lane)
            except Exception:
                # Claiming or recording a result failed (e.g. Mongo unreachable). Keep the lane
                # alive; an unfinished job's lease expires and it is retried.
                log.exception("lane %d: queue error", lane)
                await _wait(self.stop, IDLE_SLEEP_S)

    async def _run_one(self, lane: int) -> None:
        async with self._claim_lock:
            job = await jobs.claim(self.db, self.busy_orgs | self.paused_orgs)
            if job is not None:
                self.busy_orgs.add(job["org_id"])
        if job is None:
            await _wait(self.stop, IDLE_SLEEP_S)
            return
        org_id = job["org_id"]
        self.lane_jobs[lane] = {
            "job_id": job["_id"],
            "kind": job["kind"],
            "org_id": org_id,
            "person_id": job["payload"].get("person_id"),
            "started_at": job["started_at"],
        }
        try:
            note = await self._handle(job)
            await jobs.complete(self.db, job, note)
        except Blocked as exc:
            await self.crawler.block_org(org_id, str(exc))
            self.paused_orgs.add(org_id)
            await jobs.fail(self.db, job, f"blocked: {exc}")
        except FetchError as exc:
            await jobs.fail(self.db, job, str(exc))
        except Exception as exc:
            log.exception("lane %d: job %s crashed", lane, job["_id"])
            await jobs.fail(self.db, job, repr(exc))
        finally:
            self.busy_orgs.discard(org_id)
            self.lane_jobs[lane] = None

    async def _handle(self, job: dict[str, Any]) -> str:
        if job["kind"] == "discover":
            run = await self.crawler.discover_org(job["org_id"])
            return f"health={run.get('health')} discovered={run.get('discovered')} queued={run.get('queued')}"
        if job["kind"] == "reprocess":  # queued by an admin after editing the domain profile
            counts = await self.crawler.reprocess_org(job["org_id"])
            return " ".join(f"{k}={v}" for k, v in counts.items())
        payload = job["payload"]
        return await self.crawler.fetch_profile(payload["person_id"], payload.get("reason", "stale"))


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def run_worker() -> None:
    settings, config, db = get_settings(), get_config(), get_db()
    # From here on, the worker's log lines are also saved for the Worker page (see events.py).
    events = EventBuffer(heartbeat.worker_id())
    logging.getLogger().addHandler(events)
    await ensure_indexes(db)
    await sync_config(db, config)
    try:
        fetcher = Fetcher.from_settings(settings)
    except CrawlerNotIdentified as exc:
        # Idle rather than exit: Docker would restart an exited worker over and over. Keep sending
        # heartbeats so the Worker page can say why nothing is happening.
        log.error("%s", exc)  # noqa: TRY400 - a config problem; a traceback would only add noise
        wid, started = heartbeat.worker_id(), datetime.now(UTC)
        while True:
            with contextlib.suppress(Exception):
                await heartbeat.beat(db, wid, started, "idle", reason=str(exc))
                await events.save(db)
            await asyncio.sleep(heartbeat.HEARTBEAT_S)
    labels = LabelClassifier(db, LlmGateway(db, settings))
    crawler = Crawler(db, fetcher, config, settings, RawStore(settings.raw_dir), labels)
    worker = Worker(db, crawler, settings.worker_lanes, settings.scraper_user_agent, events)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop.set)
    log.info(
        "worker started with %d lanes, user agent %r", settings.worker_lanes, settings.scraper_user_agent
    )
    try:
        await worker.run()
    finally:
        log.info("worker stopped")
        with contextlib.suppress(Exception):
            await worker.beat("stopped")
            await events.save(db)
        await fetcher.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run_worker())

"""The admin "Worker" page: what the job queue holds and what each worker process is doing.

Jobs come straight from the `jobs` collection (finished ones are kept for a week); live worker
state comes from the heartbeats in `workers` (see radar/worker/heartbeat.py), and the worker's
own log lines from `worker_events` (radar/worker/events.py). The only writes are the three job
actions at the bottom: run now, cancel, retry.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query

from radar.api.deps import track
from radar.api.schemas import (
    BlockedOrg,
    HourRow,
    JobOut,
    LaneOut,
    OrgQueueRow,
    QueueRow,
    WorkerEventOut,
    WorkerOut,
    WorkerStatusOut,
)
from radar.auth import AdminUser
from radar.config import get_config
from radar.db import Database, get_db
from radar.worker import jobs
from radar.worker.heartbeat import STALE_AFTER

router = APIRouter(prefix="/api/admin", tags=["worker"])


def _count(*conditions: Any) -> dict[str, Any]:
    return {"$sum": {"$cond": [{"$and": list(conditions)}, 1, 0]}}


async def _jobs_out(db: Database, docs: list[dict[str, Any]]) -> list[JobOut]:
    """Turn job documents into rows, with organisation and person names looked up in one go."""
    org_names = {org.id: org.name for org in get_config().organisations}
    person_ids = {doc["payload"]["person_id"] for doc in docs if doc.get("payload", {}).get("person_id")}
    people = {
        p["_id"]: p.get("name")
        async for p in db["people"].find({"_id": {"$in": sorted(person_ids)}}, {"name": 1})
    }
    rows = []
    for doc in docs:
        payload = doc.get("payload") or {}
        person_id = payload.get("person_id")
        rows.append(
            JobOut(
                id=doc["_id"],
                kind=doc["kind"],
                status=doc["status"],
                org_id=doc["org_id"],
                org_name=org_names.get(doc["org_id"]),
                person_id=person_id,
                person_name=people.get(person_id),
                reason=payload.get("reason"),
                priority=doc.get("priority", 0),
                attempts=doc.get("attempts", 0),
                created_at=doc["created_at"],
                run_after=doc.get("run_after"),
                started_at=doc.get("started_at"),
                finished_at=doc.get("finished_at"),
                lease_until=doc.get("lease_until"),
                note=doc.get("note"),
                error=doc.get("error"),
            )
        )
    return rows


def _worker_out(
    doc: dict[str, Any], now: datetime, org_names: dict[str, str], people: dict[str, str | None]
) -> WorkerOut:
    status = doc.get("state", "running")
    if status != "stopped" and now - doc["last_seen"] > STALE_AFTER:
        status = "stale"
    return WorkerOut(
        id=doc["_id"],
        status=status,
        reason=doc.get("reason"),
        started_at=doc["started_at"],
        last_seen=doc["last_seen"],
        lanes=[
            LaneOut(
                **lane,
                org_name=org_names.get(lane.get("org_id") or ""),
                person_name=people.get(lane.get("person_id") or ""),
            )
            for lane in doc.get("lanes", [])
        ],
        paused_orgs=doc.get("paused_orgs", []),
        user_agent=doc.get("user_agent"),
    )


@router.get("/worker", response_model=WorkerStatusOut)
async def worker_status(user: AdminUser) -> WorkerStatusOut:
    db, now = get_db(), datetime.now(UTC)
    day_ago = now - timedelta(days=1)
    org_names = {org.id: org.name for org in get_config().organisations}
    worker_docs = await db["workers"].find().sort("started_at", -1).to_list()
    lane_people = {
        lane["person_id"] for w in worker_docs for lane in w.get("lanes", []) if lane.get("person_id")
    }
    people = {
        p["_id"]: p.get("name")
        async for p in db["people"].find({"_id": {"$in": sorted(lane_people)}}, {"name": 1})
    }
    workers = [_worker_out(doc, now, org_names, people) for doc in worker_docs]

    is_queued = {"$eq": ["$status", "queued"]}
    grouped = await (
        await db["jobs"].aggregate(
            [
                {
                    "$group": {
                        "_id": "$kind",
                        "due": _count(is_queued, {"$lte": ["$run_after", now]}),
                        "waiting": _count(is_queued, {"$gt": ["$run_after", now]}),
                        "running": _count({"$eq": ["$status", "running"]}),
                        "done_24h": _count({"$eq": ["$status", "done"]}, {"$gte": ["$finished_at", day_ago]}),
                        "failed_24h": _count(
                            {"$eq": ["$status", "failed"]}, {"$gte": ["$finished_at", day_ago]}
                        ),
                    }
                },
                {"$match": {"_id": {"$ne": None}}},
                {"$sort": {"_id": 1}},
            ]
        )
    ).to_list()
    queue = [QueueRow(kind=row.pop("_id"), **row) for row in grouped]

    per_org = await (
        await db["jobs"].aggregate(
            [
                {"$match": {"status": {"$in": ["queued", "running"]}}},
                {
                    "$group": {
                        "_id": "$org_id",
                        "due": _count(is_queued, {"$lte": ["$run_after", now]}),
                        "waiting": _count(is_queued, {"$gt": ["$run_after", now]}),
                        "running": _count({"$eq": ["$status", "running"]}),
                    }
                },
                {"$sort": {"due": -1, "_id": 1}},
            ]
        )
    ).to_list()
    by_org = [OrgQueueRow(org_id=row["_id"], name=org_names.get(row["_id"]), **row) for row in per_org]

    oldest = await db["jobs"].find_one(
        {"status": "queued", "run_after": {"$lte": now}}, {"run_after": 1}, sort=[("run_after", 1)]
    )
    running = await db["jobs"].find({"status": "running"}).sort("started_at", 1).to_list()

    blocked = [
        BlockedOrg(
            org_id=doc["_id"],
            name=org_names.get(doc["_id"]),
            reason=doc.get("status_reason"),
            blocked_until=doc.get("blocked_until"),
        )
        async for doc in db["organisations"].find({"status": "blocked", "blocked_until": {"$gt": now}})
    ]
    return WorkerStatusOut(
        now=now,
        workers=workers,
        queue=queue,
        by_org=by_org,
        oldest_due_at=oldest["run_after"] if oldest else None,
        running=await _jobs_out(db, running),
        blocked=blocked,
        throughput=await _throughput(db, now),
    )


async def _throughput(db: Database, now: datetime) -> list[HourRow]:
    """Jobs finished per hour over the last 24 hours, with empty hours filled in as zeros."""
    this_hour = now.replace(minute=0, second=0, microsecond=0)
    hours = [this_hour - timedelta(hours=n) for n in range(23, -1, -1)]
    rows = await (
        await db["jobs"].aggregate(
            [
                {"$match": {"status": {"$in": ["done", "failed"]}, "finished_at": {"$gte": hours[0]}}},
                {
                    "$group": {
                        "_id": {"$dateTrunc": {"date": "$finished_at", "unit": "hour"}},
                        "done": _count({"$eq": ["$status", "done"]}),
                        "failed": _count({"$eq": ["$status", "failed"]}),
                    }
                },
            ]
        )
    ).to_list()
    by_hour = {row["_id"]: row for row in rows}
    return [
        HourRow(hour=h, done=by_hour.get(h, {}).get("done", 0), failed=by_hour.get(h, {}).get("failed", 0))
        for h in hours
    ]


@router.get("/jobs", response_model=list[JobOut])
async def list_jobs(
    user: AdminUser,
    status: Literal["finished", "queued", "running", "done", "failed", "cancelled"] = "finished",
    kind: str | None = None,
    org: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[JobOut]:
    """The worker log. "finished" (the default) = done, failed and cancelled jobs, newest first."""
    finished = ["done", "failed", "cancelled"]
    query: dict[str, Any] = {"status": {"$in": finished} if status == "finished" else status}
    if kind:
        query["kind"] = kind
    if org:
        query["org_id"] = org
    # Each status has its natural order: the log newest first, the queue in the order it will run.
    sort = {
        "queued": [("priority", -1), ("run_after", 1)],
        "running": [("started_at", 1)],
    }.get(status, [("finished_at", -1)])
    db = get_db()
    docs = await db["jobs"].find(query).sort(sort).limit(limit).to_list()
    return await _jobs_out(db, docs)


_LEVELS = {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


@router.get("/worker/events", response_model=list[WorkerEventOut])
async def worker_events(
    user: AdminUser,
    level: Literal["info", "warning", "error"] = "info",
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[WorkerEventOut]:
    """The worker's saved log lines at `level` and above, newest first."""
    cursor = get_db()["worker_events"].find({"levelno": {"$gte": _LEVELS[level]}}).sort("ts", -1).limit(limit)
    return [WorkerEventOut(id=doc.pop("_id"), **doc) async for doc in cursor]


# --- Job actions ---------------------------------------------------------------------------------


async def _job_out(job_id: str) -> JobOut:
    db = get_db()
    doc = await db["jobs"].find_one({"_id": job_id})
    if doc is None:
        raise HTTPException(404, "No such job (finished jobs are deleted after a week)")
    return (await _jobs_out(db, [doc]))[0]


async def _act(job_id: str, action: Callable[[], Awaitable[Any]]) -> Any:
    """Run a job action: 404 if the job is gone, 409 if it's in the wrong state for it."""
    await _job_out(job_id)
    try:
        return await action()
    except jobs.JobStateError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/jobs/{job_id}/run-now", response_model=JobOut)
async def run_job_now(job_id: str, user: AdminUser) -> JobOut:
    await _act(job_id, lambda: jobs.run_now(get_db(), job_id))
    await track(get_db(), user.email, "job_run_now", job_id)
    return await _job_out(job_id)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
async def cancel_job(job_id: str, user: AdminUser) -> JobOut:
    await _act(job_id, lambda: jobs.cancel(get_db(), job_id, user.email))
    await track(get_db(), user.email, "job_cancel", job_id)
    return await _job_out(job_id)


@router.post("/jobs/{job_id}/retry", response_model=JobOut)
async def retry_job(job_id: str, user: AdminUser) -> JobOut:
    """Returns the job that will do the work: this one, or an identical job that was already queued."""
    queued_id = await _act(job_id, lambda: jobs.retry(get_db(), job_id))
    await track(get_db(), user.email, "job_retry", job_id)
    return await _job_out(queued_id)

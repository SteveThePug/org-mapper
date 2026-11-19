"""A small job queue on top of MongoDB.

At this scale (thousands of jobs a day) a separate broker (Redis, RabbitMQ, SQS) would be one
more thing to run for no benefit. Jobs are claimed with an atomic `find_one_and_update` and a
lease, so several worker processes can share the queue safely; an expired lease means the
worker died and the job is retried.
"""

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from radar.db import Database

JobKind = Literal["discover", "fetch", "reprocess"]

# Higher runs first.
PRIORITY_USER = 100
PRIORITY_VERIFY = 80
PRIORITY_NEW = 60
PRIORITY_CHANGED = 50
PRIORITY_STALE_IN_SCOPE = 40
PRIORITY_DISCOVER = 30
PRIORITY_REPROCESS = 25
PRIORITY_STALE_OTHER = 20

LEASE = timedelta(minutes=10)
# Kinds that can legitimately run longer than LEASE (a reprocess walks a whole organisation).
LONG_LEASES: dict[str, timedelta] = {"reprocess": timedelta(hours=2)}
MAX_ATTEMPTS = 3


async def enqueue(
    db: Database,
    kind: JobKind,
    key: str,
    org_id: str,
    payload: dict[str, Any],
    priority: int,
    run_after: datetime | None = None,
) -> None:
    """Queue a job unless an identical one is already waiting.

    If one is waiting:
    - a higher priority wins, and brings its payload (a user's "refresh now" replaces a queued
      routine "stale" fetch, so the page is re-read in full rather than conditionally);
    - a job waiting out a retry backoff (it has an `error`) keeps its delay, unless the new
      request has a higher priority. Otherwise the scheduler, which re-queues every discovery
      once a minute, would pull a failing job forward and hit the site every minute.
    """
    now = datetime.now(UTC)
    when = run_after or now
    existing = await db["jobs"].find_one({"key": key, "status": "queued"}, {"priority": 1, "error": 1})
    if existing is None:
        # DuplicateKeyError = a concurrent enqueue of the same key won the race; nothing to do.
        with contextlib.suppress(DuplicateKeyError):
            await db["jobs"].insert_one(
                {
                    "_id": uuid.uuid4().hex,
                    "key": key,
                    "status": "queued",
                    "kind": kind,
                    "org_id": org_id,
                    "payload": payload,
                    "priority": priority,
                    "run_after": when,
                    "created_at": now,
                    "attempts": 0,
                }
            )
        return

    higher = priority > existing.get("priority", 0)
    update: dict[str, Any] = {"$max": {"priority": priority}}
    if higher:
        update["$set"] = {"payload": payload}
    if higher or not existing.get("error"):
        update["$min"] = {"run_after": when}
    await db["jobs"].update_one({"_id": existing["_id"], "status": "queued"}, update)


async def claim(db: Database, exclude_orgs: set[str], only_org: str | None = None) -> dict[str, Any] | None:
    now = datetime.now(UTC)
    query: dict[str, Any] = {"status": "queued", "run_after": {"$lte": now}}
    if only_org:
        query["org_id"] = only_org
    elif exclude_orgs:
        query["org_id"] = {"$nin": sorted(exclude_orgs)}
    # The token proves ownership: if this lease expires and another worker re-claims the job,
    # our late complete()/fail() no longer matches and can't overwrite the newer run's state.
    token = uuid.uuid4().hex
    job = await db["jobs"].find_one_and_update(
        query,
        {
            "$set": {
                "status": "running",
                "lease_until": now + LEASE,
                "lease_token": token,
                "started_at": now,
            },
            "$inc": {"attempts": 1},
        },
        sort=[("priority", -1), ("run_after", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if job is not None and job["kind"] in LONG_LEASES:
        job["lease_until"] = now + LONG_LEASES[job["kind"]]
        await db["jobs"].update_one({"_id": job["_id"]}, {"$set": {"lease_until": job["lease_until"]}})
    return job


def _owned(job: dict[str, Any]) -> dict[str, Any]:
    """Filter matching this job only while we still hold its lease."""
    return {"_id": job["_id"], "status": "running", "lease_token": job.get("lease_token")}


async def complete(db: Database, job: dict[str, Any], note: str | None = None) -> None:
    await db["jobs"].update_one(
        _owned(job), {"$set": {"status": "done", "finished_at": datetime.now(UTC), "note": note}}
    )


async def fail(db: Database, job: dict[str, Any], error: str, retry_in: timedelta | None = None) -> None:
    now = datetime.now(UTC)
    if job.get("attempts", 1) >= MAX_ATTEMPTS:
        update = {"status": "failed", "finished_at": now, "error": error}
    else:
        backoff = retry_in if retry_in is not None else timedelta(minutes=5 * 2 ** job.get("attempts", 1))
        update = {"status": "queued", "run_after": now + backoff, "error": error, "lease_token": None}
    try:
        await db["jobs"].update_one(_owned(job), {"$set": update})
    except DuplicateKeyError:
        # The same work was re-queued meanwhile; that newer job will do it.
        await db["jobs"].update_one(
            _owned(job), {"$set": {"status": "failed", "finished_at": now, "error": error}}
        )


async def requeue_expired(db: Database) -> int:
    """Jobs whose worker died mid-way go back to the queue."""
    requeued = 0
    async for job in db["jobs"].find({"status": "running", "lease_until": {"$lt": datetime.now(UTC)}}):
        await fail(db, job, "lease expired (worker stopped?)", retry_in=timedelta(0))
        requeued += 1
    return requeued


# --- Admin actions from the Worker page ------------------------------------------------------


class JobStateError(Exception):
    """The job isn't in a state that allows this action (e.g. cancelling a job that already ran)."""


async def run_now(db: Database, job_id: str) -> None:
    """Skip a queued job's wait (a retry backoff or a schedule) and put it at the front."""
    result = await db["jobs"].update_one(
        {"_id": job_id, "status": "queued"},
        {"$set": {"run_after": datetime.now(UTC)}, "$max": {"priority": PRIORITY_USER}},
    )
    if result.matched_count == 0:
        raise JobStateError("only a queued job can be run now")


async def cancel(db: Database, job_id: str, by: str) -> None:
    """Take a queued job off the queue. A running job can't be cancelled: the worker would carry on."""
    result = await db["jobs"].update_one(
        {"_id": job_id, "status": "queued"},
        {"$set": {"status": "cancelled", "finished_at": datetime.now(UTC), "note": f"cancelled by {by}"}},
    )
    if result.matched_count == 0:
        raise JobStateError("only a queued job can be cancelled")


async def retry(db: Database, job_id: str) -> str:
    """Put a failed or cancelled job back on the queue as a fresh job, to run next.

    Returns the id of the job that will do the work: usually this one, but if the same work has
    been queued again meanwhile (the unique index allows one queued job per key), that one is
    run now instead.
    """
    job = await db["jobs"].find_one({"_id": job_id}, {"key": 1, "status": 1})
    if job is None or job["status"] not in ("failed", "cancelled"):
        raise JobStateError("only a failed or cancelled job can be retried")
    try:
        result = await db["jobs"].update_one(
            {"_id": job_id, "status": job["status"]},
            {
                "$set": {
                    "status": "queued",
                    "run_after": datetime.now(UTC),
                    "attempts": 0,
                    "lease_token": None,
                },
                "$max": {"priority": PRIORITY_USER},
                # finished_at must go, or the week-long expiry index would delete the queued job.
                "$unset": {"finished_at": "", "error": "", "note": ""},
            },
        )
    except DuplicateKeyError:
        existing = await db["jobs"].find_one({"key": job["key"], "status": "queued"}, {"_id": 1})
        if existing is None:  # it was claimed in the meantime
            raise JobStateError("the same job is already running") from None
        await run_now(db, existing["_id"])
        return str(existing["_id"])
    if result.matched_count == 0:
        raise JobStateError("the job changed meanwhile; refresh and try again")
    return job_id

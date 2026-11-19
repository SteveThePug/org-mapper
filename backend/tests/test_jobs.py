"""The Mongo job queue: backoff that sticks, priority upgrades, and lease ownership."""

from datetime import UTC, datetime, timedelta

import pytest

from radar.db import Database
from radar.worker import jobs


async def queued(db: Database, key: str) -> dict:
    doc = await db["jobs"].find_one({"key": key, "status": "queued"})
    assert doc is not None
    return doc


async def test_reenqueue_does_not_cut_a_retry_backoff_short(db: Database) -> None:
    await jobs.enqueue(db, "discover", "discover:a", "a", {}, jobs.PRIORITY_DISCOVER)
    job = await jobs.claim(db, set())
    assert job is not None
    await jobs.fail(db, job, "HTTP 500")
    retry_at = (await queued(db, "discover:a"))["run_after"]
    assert retry_at > datetime.now(UTC) + timedelta(minutes=5)

    # The scheduler queues the same discovery again on its next tick: the backoff must hold.
    await jobs.enqueue(db, "discover", "discover:a", "a", {}, jobs.PRIORITY_DISCOVER)
    assert (await queued(db, "discover:a"))["run_after"] == retry_at


async def test_higher_priority_request_replaces_the_payload(db: Database) -> None:
    payload = {"person_id": "p1", "reason": "stale"}
    await jobs.enqueue(db, "fetch", "fetch:p1", "a", payload, jobs.PRIORITY_STALE_OTHER)
    user = {"person_id": "p1", "reason": "user"}
    await jobs.enqueue(db, "fetch", "fetch:p1", "a", user, jobs.PRIORITY_USER)
    doc = await queued(db, "fetch:p1")
    assert (doc["priority"], doc["payload"]["reason"]) == (jobs.PRIORITY_USER, "user")

    # A later low-priority request changes neither.
    await jobs.enqueue(db, "fetch", "fetch:p1", "a", payload, jobs.PRIORITY_STALE_OTHER)
    doc = await queued(db, "fetch:p1")
    assert (doc["priority"], doc["payload"]["reason"]) == (jobs.PRIORITY_USER, "user")
    assert await db["jobs"].count_documents({}) == 1


async def test_a_worker_whose_lease_expired_cannot_overwrite_the_retry(db: Database) -> None:
    await jobs.enqueue(db, "fetch", "fetch:p1", "a", {"person_id": "p1"}, jobs.PRIORITY_NEW)
    slow = await jobs.claim(db, set())
    assert slow is not None
    # The lease runs out and the job is handed to another worker.
    await db["jobs"].update_one(
        {"_id": slow["_id"]}, {"$set": {"lease_until": datetime.now(UTC) - timedelta(seconds=1)}}
    )
    assert await jobs.requeue_expired(db) == 1
    fast = await jobs.claim(db, set())
    assert fast is not None and fast["_id"] == slow["_id"]

    await jobs.complete(db, slow, "late")  # no longer ours: ignored
    doc = await db["jobs"].find_one({"_id": fast["_id"]})
    assert doc is not None and doc["status"] == "running"
    await jobs.complete(db, fast, "done")
    doc = await db["jobs"].find_one({"_id": fast["_id"]})
    assert doc is not None and (doc["status"], doc["note"]) == ("done", "done")


async def failed_job(db: Database, key: str = "fetch:p1") -> dict:
    """A job that has used up its attempts."""
    await jobs.enqueue(db, "fetch", key, "a", {"person_id": "p1"}, jobs.PRIORITY_STALE_OTHER)
    for _ in range(jobs.MAX_ATTEMPTS):
        await db["jobs"].update_one(
            {"key": key, "status": "queued"}, {"$set": {"run_after": datetime.now(UTC)}}
        )
        job = await jobs.claim(db, set())
        assert job is not None
        await jobs.fail(db, job, "HTTP 500")
    doc = await db["jobs"].find_one({"key": key})
    assert doc is not None and doc["status"] == "failed"
    return doc


async def test_run_now_skips_the_wait_and_jumps_the_queue(db: Database) -> None:
    later = datetime.now(UTC) + timedelta(hours=1)
    await jobs.enqueue(db, "fetch", "fetch:p1", "a", {}, jobs.PRIORITY_STALE_OTHER, run_after=later)
    job = await queued(db, "fetch:p1")
    await jobs.run_now(db, job["_id"])
    job = await queued(db, "fetch:p1")
    assert job["run_after"] <= datetime.now(UTC)
    assert job["priority"] == jobs.PRIORITY_USER


async def test_cancel_takes_a_queued_job_off_the_queue_but_not_a_running_one(db: Database) -> None:
    await jobs.enqueue(db, "fetch", "fetch:p1", "a", {}, jobs.PRIORITY_STALE_OTHER)
    job_id = (await queued(db, "fetch:p1"))["_id"]
    await jobs.cancel(db, job_id, "admin@x.test")
    assert await jobs.claim(db, set()) is None
    doc = await db["jobs"].find_one({"_id": job_id})
    assert doc is not None and doc["status"] == "cancelled" and doc["note"] == "cancelled by admin@x.test"

    await jobs.enqueue(db, "fetch", "fetch:p2", "a", {}, jobs.PRIORITY_STALE_OTHER)
    running = await jobs.claim(db, set())
    assert running is not None
    with pytest.raises(jobs.JobStateError):
        await jobs.cancel(db, running["_id"], "admin@x.test")


async def test_retry_requeues_a_failed_job_as_new(db: Database) -> None:
    doc = await failed_job(db)
    assert await jobs.retry(db, doc["_id"]) == doc["_id"]
    job = await queued(db, "fetch:p1")
    assert job["attempts"] == 0
    assert "finished_at" not in job  # or the expiry index would delete it
    assert "error" not in job
    assert (await jobs.claim(db, set()) or {}).get("_id") == doc["_id"]


async def test_retry_runs_the_already_queued_twin_instead(db: Database) -> None:
    doc = await failed_job(db)
    later = datetime.now(UTC) + timedelta(hours=1)
    await jobs.enqueue(
        db, "fetch", "fetch:p1", "a", {"person_id": "p1"}, jobs.PRIORITY_STALE_OTHER, run_after=later
    )
    twin = await queued(db, "fetch:p1")
    assert await jobs.retry(db, doc["_id"]) == twin["_id"]
    assert (await queued(db, "fetch:p1"))["run_after"] <= datetime.now(UTC)
    still = await db["jobs"].find_one({"_id": doc["_id"]})
    assert still is not None and still["status"] == "failed"


async def test_retry_refuses_a_job_that_did_not_fail(db: Database) -> None:
    await jobs.enqueue(db, "fetch", "fetch:p1", "a", {}, jobs.PRIORITY_STALE_OTHER)
    with pytest.raises(jobs.JobStateError):
        await jobs.retry(db, (await queued(db, "fetch:p1"))["_id"])

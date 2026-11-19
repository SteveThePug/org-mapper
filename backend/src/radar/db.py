"""MongoDB access. One client per process; collections are reached via `db["name"]`."""

from typing import Any

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, IndexModel
from pymongo.asynchronous.database import AsyncDatabase

from radar.settings import get_settings

Database = AsyncDatabase[dict[str, Any]]

_client: AsyncMongoClient[dict[str, Any]] | None = None


def get_db() -> Database:
    global _client
    settings = get_settings()
    if _client is None:
        _client = AsyncMongoClient(settings.mongo_uri, tz_aware=True)
    return _client[settings.mongo_db]


async def close_db() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


INDEXES: dict[str, list[IndexModel]] = {
    "people": [
        IndexModel([("org_id", ASCENDING), ("status", ASCENDING)]),
        IndexModel([("segments", ASCENDING), ("status", ASCENDING), ("name", ASCENDING)]),
        IndexModel([("status", ASCENDING), ("departed_at", DESCENDING)]),
    ],
    "events": [
        IndexModel([("detected_at", DESCENDING)]),
        IndexModel([("person_id", ASCENDING), ("detected_at", DESCENDING)]),
        IndexModel([("org_id", ASCENDING), ("type", ASCENDING), ("detected_at", DESCENDING)]),
        # Move events seen from the old profile's side (see Crawler._earlier_move_events).
        IndexModel([("related_person_id", ASCENDING)], sparse=True),
    ],
    # Archived pages a backfill has already read, so an interrupted backfill can resume.
    "archive_checked": [
        IndexModel([("org_id", ASCENDING), ("snapshot", ASCENDING), ("kind", ASCENDING)]),
        IndexModel([("person_id", ASCENDING)]),
    ],
    "observations": [IndexModel([("person_id", ASCENDING), ("fetched_at", DESCENDING)])],
    "notes": [IndexModel([("person_id", ASCENDING), ("created_at", DESCENDING)])],
    "jobs": [
        IndexModel([("status", ASCENDING), ("priority", DESCENDING), ("run_after", ASCENDING)]),
        # At most one *queued* job per key (e.g. "fetch:<person>"), so re-enqueueing is a no-op.
        IndexModel([("key", ASCENDING)], unique=True, partialFilterExpression={"status": "queued"}),
        # Finished jobs are kept a week for debugging, then expire.
        IndexModel([("finished_at", ASCENDING)], expireAfterSeconds=7 * 24 * 3600),
        # The worker log filtered by organisation.
        IndexModel([("org_id", ASCENDING), ("finished_at", DESCENDING)]),
    ],
    # One heartbeat per worker process; a worker silent for a day is forgotten.
    "workers": [IndexModel([("last_seen", ASCENDING)], expireAfterSeconds=24 * 3600)],
    # The worker's saved log lines (see worker/events.py), kept a week.
    "worker_events": [
        IndexModel([("ts", ASCENDING)], expireAfterSeconds=7 * 24 * 3600),
        IndexModel([("levelno", ASCENDING), ("ts", DESCENDING)]),
    ],
    "scrape_runs": [IndexModel([("org_id", ASCENDING), ("started_at", DESCENDING)])],
    "llm_calls": [IndexModel([("ts", DESCENDING)]), IndexModel([("task", ASCENDING), ("ts", DESCENDING)])],
    # Failed-login counters; Mongo deletes each one when its lockout window ends.
    "login_failures": [IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0)],
    "usage_events": [IndexModel([("ts", DESCENDING)]), IndexModel([("kind", ASCENDING), ("ts", DESCENDING)])],
}


async def ensure_indexes(db: Database) -> None:
    """Prepare the database: indexes, plus any one-off upgrades of older documents."""
    for collection, indexes in INDEXES.items():
        await db[collection].create_indexes(indexes)
    await _upgrade_documents(db)


async def _upgrade_documents(db: Database) -> None:
    """Rename the law-specific fields of documents written before the domain profile existed.

    Idempotent: it only matches documents that still have the old fields. `make reprocess`
    afterwards re-derives everything from the stored pages.
    """
    await db["people"].update_many(
        {"practices": {"$exists": True}},
        [
            {
                "$set": {
                    "groups": "$practices",
                    "groups_raw": "$practices_raw",
                    "seniority_year": "$jd_year",
                    "extra": {
                        "languages": {"$ifNull": ["$languages", []]},
                        "admissions": {
                            "$map": {"input": {"$ifNull": ["$admissions", []]}, "in": "$$this.raw"}
                        },
                    },
                }
            },
            {"$unset": ["practices", "practices_raw", "jd_year", "languages", "admissions"]},
        ],
    )
    await db["events"].update_many({"type": "practice_changed"}, {"$set": {"type": "group_changed"}})

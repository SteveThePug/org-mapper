"""Worker heartbeats: how the API (a different process) knows what the worker is doing.

The worker keeps its live state (which lane is on which job, which organisations are paused)
in memory. Every few seconds it copies that state into one document in the `workers`
collection; the admin "Worker" page reads it from there. A worker that stops writing
(crashed, container stopped) is shown as "stale" once its last heartbeat is older than
STALE_AFTER. Old heartbeats are deleted by a TTL index (see db.py).
"""

import os
import socket
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from radar.db import Database

HEARTBEAT_S = 10
# Three missed heartbeats in a row = the worker is probably gone.
STALE_AFTER = timedelta(seconds=3 * HEARTBEAT_S)

WorkerState = Literal["running", "idle", "stopped"]


def worker_id() -> str:
    """One id per worker process: the container's hostname plus the process id."""
    return f"{socket.gethostname()}:{os.getpid()}"


async def beat(
    db: Database,
    wid: str,
    started_at: datetime,
    state: WorkerState,
    *,
    reason: str | None = None,
    lanes: list[dict[str, Any]] | None = None,
    paused_orgs: list[str] | None = None,
    user_agent: str | None = None,
) -> None:
    await db["workers"].update_one(
        {"_id": wid},
        {
            "$set": {
                "started_at": started_at,
                "last_seen": datetime.now(UTC),
                "state": state,
                "reason": reason,
                "lanes": lanes or [],
                "paused_orgs": paused_orgs or [],
                "user_agent": user_agent,
            }
        },
        upsert=True,
    )

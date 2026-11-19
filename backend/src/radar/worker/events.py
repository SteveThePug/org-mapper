"""Worker events: the worker's own log lines, saved to Mongo for the admin Worker page.

Python's logging is synchronous and our Mongo driver is async, so the handler doesn't write to
Mongo itself. It keeps records in memory, and the worker saves them with each heartbeat (every
few seconds). If Mongo is down at that moment, that batch is lost, but every line still goes to
the container log as before (`docker compose logs worker`). Events expire after a week.

Kept: everything from our own code ("radar.*") at INFO and above, and warnings and errors from
libraries. Library INFO lines (every HTTP request) would drown the useful ones.
"""

import logging
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import Any

from radar.db import Database

MAX_PENDING = 1000  # if Mongo is unreachable for a while, keep the newest lines only
MAX_MESSAGE = 2000


class EventBuffer(logging.Handler):
    def __init__(self, worker_id: str) -> None:
        super().__init__(logging.INFO)
        self.worker_id = worker_id
        self.pending: deque[dict[str, Any]] = deque(maxlen=MAX_PENDING)

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING and not record.name.startswith("radar"):
            return
        try:
            message = record.getMessage()
        except Exception:  # a bad format string in someone's log call mustn't crash the worker
            message = str(record.msg)
        traceback = logging.Formatter().formatException(record.exc_info) if record.exc_info else None
        self.pending.append(
            {
                "_id": uuid.uuid4().hex,
                "ts": datetime.fromtimestamp(record.created, UTC),
                "level": record.levelname,
                "levelno": record.levelno,
                "logger": record.name,
                "message": message[:MAX_MESSAGE],
                "traceback": traceback,
                "worker_id": self.worker_id,
            }
        )

    async def save(self, db: Database) -> None:
        batch = list(self.pending)
        if not batch:
            return
        self.pending.clear()  # taken first: lines logged while we write wait for the next save
        await db["worker_events"].insert_many(batch)

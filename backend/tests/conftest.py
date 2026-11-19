import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pymongo import AsyncMongoClient

from radar.config import AppConfig, DomainConfig, load_config
from radar.db import Database, ensure_indexes
from radar.models import Person

FIXTURES = Path(__file__).parent / "fixtures"
REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture(scope="session")
def app_config() -> AppConfig:
    return load_config(REPO_CONFIG)


@pytest.fixture(scope="session")
def domain(app_config: AppConfig) -> DomainConfig:
    return app_config.domain


def fixture_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_person(**fields: Any) -> Person:
    defaults: dict[str, Any] = {
        "id": "p1",
        "org_id": "weil",
        "profile_url": "https://www.weil.com/people/jane-example",
        "first_seen": datetime(2026, 1, 1, tzinfo=UTC),
        "status": "active",
        "baseline": False,
    }
    return Person(**(defaults | fields))


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    """A throwaway database on the Mongo from `docker compose` (skipped if Mongo isn't running)."""
    uri = os.environ.get("TEST_MONGO_URI", "mongodb://localhost:27017")
    client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
        uri, serverSelectionTimeoutMS=1500, tz_aware=True
    )
    try:
        await client.admin.command("ping")
    except Exception:
        await client.close()
        pytest.skip(f"MongoDB not reachable at {uri} (run `make test`, which starts it)")
    name = f"radar_test_{uuid.uuid4().hex[:8]}"
    database = client[name]
    await ensure_indexes(database)
    yield database
    await client.drop_database(name)
    await client.close()

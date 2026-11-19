"""The domain profile lives in MongoDB too, so admins can edit it in the UI (Admin -> Tracking).

The same pattern as organisations (radar/orgs.py): `config/domains/<RADAR_DOMAIN>.yaml` is only the
seed. On startup it is copied into the `domain_profiles` collection *unless an admin has edited
it* (`managed: true`), so a UI edit is never overwritten by a redeploy.

Each profile is its own document (`_id` = the profile name), so switching RADAR_DOMAIN never mixes
up two profiles' edits. People from different domains would still share the `people` collection,
so run one domain per deployment (its own database) rather than switching back and forth.

Changing titles, the taxonomy or segments only affects pages fetched from then on. To re-classify
everyone already stored, reprocess (Admin -> Tracking -> Reprocess, or `make reprocess`).
"""

from datetime import UTC, datetime
from typing import Any

from radar.config import AppConfig, DomainConfig
from radar.db import Database

COLLECTION = "domain_profiles"


async def seed_domain(db: Database, name: str, seed: DomainConfig, force: bool = False) -> None:
    """Copy the YAML profile into Mongo, unless an admin has edited it."""
    existing = await db[COLLECTION].find_one({"_id": name}, {"managed": 1})
    if existing and existing.get("managed") and not force:
        return
    await db[COLLECTION].replace_one(
        {"_id": name}, {"_id": name, "profile": seed.model_dump(), "managed": False}, upsert=True
    )


async def load_domain(db: Database, name: str) -> tuple[DomainConfig, dict[str, Any]] | None:
    """The stored profile and its document (for `managed`, `updated_at`...), or None if not seeded."""
    doc = await db[COLLECTION].find_one({"_id": name})
    if doc is None:
        return None
    return DomainConfig.model_validate(doc["profile"]), doc


async def refresh_domain(db: Database, config: AppConfig) -> None:
    """Swap in the stored profile, so every holder of the shared config sees an admin's edit."""
    loaded = await load_domain(db, config.domain_name)
    if loaded is not None:
        config.domain = loaded[0]


async def save_domain(db: Database, name: str, domain: DomainConfig, user: str) -> None:
    """An admin edit. Marks the profile as managed so the YAML seed won't overwrite it."""
    await db[COLLECTION].replace_one(
        {"_id": name},
        {
            "_id": name,
            "profile": domain.model_dump(),
            "managed": True,
            "updated_at": datetime.now(UTC),
            "updated_by": user,
        },
        upsert=True,
    )

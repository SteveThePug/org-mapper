"""Which organisations we track lives in MongoDB, so admins can add and edit them in the UI.

`config/organisations.yaml` is only the seed: on startup each YAML entry is copied into the
`organisations` collection *unless an admin has edited that organisation* (`managed: true`),
so a UI edit is never overwritten by a redeploy.

Every process keeps one shared `AppConfig`. `refresh_orgs` swaps its `organisations` list in place
with what is in Mongo, so code holding the config (crawler, API) sees changes without a restart.
The worker calls it every scheduler tick; the API calls it after each admin edit.
"""

import re
from typing import Any

from radar.config import AppConfig, OrgConfig
from radar.db import Database

# The fields of OrgConfig that are stored on the organisation document (the id is `_id`).
CONFIG_FIELDS = ("name", "website", "kind", "discovery", "selectors", "min_delay_s", "enabled", "notes")


def to_doc(org: OrgConfig) -> dict[str, Any]:
    """The `$set` for an organisation document. `configured` is what the worker's scheduler reads."""
    doc = org.model_dump(include=set(CONFIG_FIELDS))
    doc["configured"] = org.discovery is not None
    return doc


def from_doc(doc: dict[str, Any]) -> OrgConfig:
    return OrgConfig(id=doc["_id"], **{k: doc[k] for k in CONFIG_FIELDS if doc.get(k) is not None})


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


async def seed_orgs(db: Database, seed: list[OrgConfig], force: bool = False) -> None:
    """Copy YAML organisations into Mongo, leaving alone any that an admin has edited."""
    for org in seed:
        existing = await db["organisations"].find_one({"_id": org.id}, {"managed": 1})
        if existing and existing.get("managed") and not force:
            continue
        await db["organisations"].update_one(
            {"_id": org.id},
            {
                "$set": to_doc(org) | {"managed": False},
                "$setOnInsert": {"status": "new", "discoveries": 0, "next_discovery_at": None},
            },
            upsert=True,
        )


async def load_orgs(db: Database) -> list[OrgConfig]:
    return [from_doc(doc) async for doc in db["organisations"].find({"name": {"$exists": True}}).sort("name")]


async def refresh_orgs(db: Database, config: AppConfig) -> None:
    config.organisations[:] = await load_orgs(db)


async def save_org(db: Database, org: OrgConfig, create: bool) -> None:
    """An admin edit. Marks the organisation as managed so the YAML seed won't overwrite it."""
    update: dict[str, Any] = {"$set": to_doc(org) | {"managed": True}}
    if create:
        update["$setOnInsert"] = {"status": "new", "discoveries": 0, "next_discovery_at": None}
    await db["organisations"].update_one({"_id": org.id}, update, upsert=create)

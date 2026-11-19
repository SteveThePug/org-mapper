"""Organisations are seeded from YAML but owned by the admin panel once edited."""

import pytest
from pydantic import ValidationError

from radar.api.schemas import OrgIn
from radar.config import AppConfig, Discovery, OrgConfig
from radar.db import Database
from radar.orgs import save_org, slugify
from radar.worker.crawler import sync_config


def _org(name: str = "Seed Name") -> OrgConfig:
    return OrgConfig(
        id="acme",
        name=name,
        website="https://acme.test",
        discovery=Discovery(urls=["https://acme.test/sitemap.xml"], include="/people/"),
    )


async def test_admin_edit_survives_reseed_and_new_orgs_load(db: Database, app_config: AppConfig) -> None:
    config = AppConfig(organisations=[_org()], domain=app_config.domain)
    await sync_config(db, config)
    assert [o.name for o in config.organisations] == ["Seed Name"]

    await save_org(db, _org("Edited in UI").model_copy(update={"kind": "bank"}), create=False)
    await save_org(db, OrgConfig(id="newco", name="NewCo", website="https://newco.test"), create=True)

    fresh = AppConfig(organisations=[_org()], domain=app_config.domain)  # a redeploy re-reads YAML
    await sync_config(db, fresh)
    by_id = {o.id: o for o in fresh.organisations}
    assert by_id["acme"].name == "Edited in UI" and by_id["acme"].kind == "bank"
    assert by_id["newco"].discovery is None
    doc = await db["organisations"].find_one({"_id": "acme"})
    assert doc is not None and doc["configured"] is True and doc["status"] == "new"


def test_org_in_rejects_bad_regex() -> None:
    with pytest.raises(ValidationError, match="not a valid regex"):
        OrgIn(
            name="X",
            website="https://x.test",
            discovery=Discovery(urls=["https://x.test/s.xml"], include="(["),
        )


def test_slugify() -> None:
    assert slugify("Goldman Sachs & Co.") == "goldman-sachs-co"

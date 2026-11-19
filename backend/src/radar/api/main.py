"""FastAPI application: `uvicorn radar.api.main:app`."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import get_args

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from radar.api import admin, events, notes, people, worker
from radar.api.schemas import LoginIn, Me, Meta, Option
from radar.auth import CurrentUser, authenticate, create_user, is_locked_out, record_login
from radar.config import Vocabulary, get_config
from radar.db import close_db, ensure_indexes, get_db
from radar.domain import refresh_domain
from radar.models import EventType
from radar.orgs import refresh_orgs
from radar.settings import get_settings
from radar.worker.crawler import sync_config

log = logging.getLogger("radar.api")

SESSION_MAX_AGE_S = 8 * 3600
INSECURE_SECRETS = {"dev-insecure-change-me", "change-me"}  # the code and .env.example defaults


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings, db = get_settings(), get_db()
    await ensure_indexes(db)
    await sync_config(db, get_config())
    # Anyone who knows the session secret can forge an admin cookie, so a default or short one is
    # a hard error rather than a warning.
    if settings.session_secret in INSECURE_SECRETS or len(settings.session_secret) < 32:
        raise RuntimeError(
            "SESSION_SECRET is missing or too weak: run `make env` (or set 32+ random characters)"
        )
    admin_email, admin_password = settings.bootstrap_admin_email, settings.bootstrap_admin_password
    if admin_email and admin_password and not await db["users"].find_one({"_id": admin_email.lower()}):
        await create_user(db, admin_email, admin_password, "admin")
        log.info("created bootstrap admin %s", admin_email)
    yield
    await close_db()


settings = get_settings()
app = FastAPI(title="Radar API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    https_only=settings.session_https_only,
    same_site="lax",
    max_age=SESSION_MAX_AGE_S,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)
app.include_router(people.router)
app.include_router(notes.router)
app.include_router(events.router)
app.include_router(admin.router)
app.include_router(worker.router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    await get_db().command("ping")
    return {"status": "ok"}


@app.post("/api/auth/login", response_model=Me)
async def login(body: LoginIn, request: Request) -> Me:
    db = get_db()
    ip = request.client.host if request.client else None
    if await is_locked_out(db, body.email, ip):
        raise HTTPException(429, "Too many failed sign-in attempts. Try again in 15 minutes.")
    user = await authenticate(db, body.email, body.password)
    await record_login(db, body.email, ip, success=user is not None)
    if user is None:
        raise HTTPException(401, "Wrong email or password")
    request.session["user"] = {"email": user.email}  # the role is looked up on each request
    return Me(**user.model_dump())


@app.post("/api/auth/logout", status_code=204)
async def logout(request: Request) -> None:
    request.session.clear()


@app.get("/api/auth/me", response_model=Me)
async def me(user: CurrentUser) -> Me:
    return Me(**user.model_dump())


@app.get("/api/vocabulary", response_model=Vocabulary)
async def vocabulary() -> Vocabulary:
    """The product's words, without signing in (the login page shows the app's name)."""
    config = get_config()
    await refresh_domain(get_db(), config)
    return config.domain.vocabulary


@app.get("/api/meta", response_model=Meta)
async def meta(user: CurrentUser) -> Meta:
    config = get_config()
    await refresh_orgs(get_db(), config)  # organisations and the domain profile are editable at runtime
    await refresh_domain(get_db(), config)
    offices = await get_db()["people"].distinct("offices", {"status": "active", "segments": {"$ne": []}})
    domain = config.domain
    return Meta(
        vocabulary=domain.vocabulary,
        segments=[Option(id=s.id, name=s.name) for s in domain.segments],
        default_segment=domain.default_segment_id,
        organisations=[Option(id=f.id, name=f.name) for f in config.organisations],
        groups=[Option(id=k, name=v.label) for k, v in domain.taxonomy.items()],
        fields=domain.fields,
        seniority=domain.seniority,
        offices=sorted(o for o in offices if isinstance(o, str)),
        event_types=list(get_args(EventType)),
    )

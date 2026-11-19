"""Minimal but real authentication: Argon2 password hashes + a signed, HTTP-only session cookie.

Two roles: `recruiter` (everything except admin) and `admin` (costs, toggles, scraper health).
Production next step: SSO with the company's identity provider (e.g. Microsoft Entra ID).
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel

from radar.db import Database, get_db

Role = Literal["recruiter", "admin"]

_hasher = PasswordHasher()

# Brute-force protection: this many failed logins for one email (or from one IP address) locks
# it out for LOCKOUT, counted from the first failure.
MAX_FAILED_LOGINS = 10
LOCKOUT = timedelta(minutes=15)


class User(BaseModel):
    email: str
    role: Role


async def create_user(db: Database, email: str, password: str, role: Role) -> None:
    await db["users"].update_one(
        {"_id": email.lower()},
        {
            "$set": {"password_hash": _hasher.hash(password), "role": role},
            "$setOnInsert": {"created_at": datetime.now(UTC)},
        },
        upsert=True,
    )


async def authenticate(db: Database, email: str, password: str) -> User | None:
    doc = await db["users"].find_one({"_id": email.lower()})
    if doc is None:
        _hasher.hash(password)  # spend the same time as a real check (no user enumeration)
        return None
    try:
        _hasher.verify(doc["password_hash"], password)
    except (VerificationError, InvalidHashError):  # wrong password, or a corrupt stored hash
        return None
    if _hasher.check_needs_rehash(doc["password_hash"]):  # hashing parameters were strengthened
        await db["users"].update_one({"_id": doc["_id"]}, {"$set": {"password_hash": _hasher.hash(password)}})
    return User(email=doc["_id"], role=doc["role"])


def _failure_keys(email: str, ip: str | None) -> list[str]:
    return [f"email:{email.lower()}"] + ([f"ip:{ip}"] if ip else [])


async def is_locked_out(db: Database, email: str, ip: str | None) -> bool:
    locked = await db["login_failures"].find_one(
        {
            "_id": {"$in": _failure_keys(email, ip)},
            "count": {"$gte": MAX_FAILED_LOGINS},
            "expires_at": {"$gt": datetime.now(UTC)},
        }
    )
    return locked is not None


async def record_login(db: Database, email: str, ip: str | None, success: bool) -> None:
    """Count failures per email and per IP (a TTL index deletes them after LOCKOUT); a successful
    login clears the email's count."""
    if success:
        await db["login_failures"].delete_one({"_id": f"email:{email.lower()}"})
        return
    for key in _failure_keys(email, ip):
        await db["login_failures"].update_one(
            {"_id": key},
            {"$inc": {"count": 1}, "$setOnInsert": {"expires_at": datetime.now(UTC) + LOCKOUT}},
            upsert=True,
        )


async def current_user(request: Request) -> User:
    """The signed-in user, read fresh from the database on every request.

    The cookie only says *who* signed in. Reading the role here (not from the cookie) means a
    demoted or deleted user loses access at once, not when their cookie expires.
    """
    data = request.session.get("user")
    email = data.get("email") if isinstance(data, dict) else None
    doc = await get_db()["users"].find_one({"_id": email}, {"role": 1}) if email else None
    if doc is None:
        request.session.clear()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    return User(email=doc["_id"], role=doc["role"])


def require_admin(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admins only")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
AdminUser = Annotated[User, Depends(require_admin)]

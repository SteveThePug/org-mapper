"""Notes: what users know about a person that the person's public page doesn't say.

A recruiter adds a note after a call ("open to a move in spring, wants a bigger London team"),
or when they hear something. Colleagues see it on the profile, and unless the author opts out,
the AI brief may use it as a fact (cited like any other).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException

from radar.api.deps import track
from radar.api.schemas import NoteIn, NoteOut
from radar.auth import CurrentUser, User
from radar.db import get_db
from radar.models import Note

router = APIRouter(prefix="/api", tags=["notes"])


async def person_notes(person_id: str) -> list[Note]:
    """All notes on a person, newest first."""
    cursor = get_db()["notes"].find({"person_id": person_id}).sort([("created_at", -1)])
    return [Note.model_validate(doc) async for doc in cursor]


async def note_counts(person_ids: list[str]) -> dict[str, int]:
    """How many notes each of these people has (people without notes are left out)."""
    pipeline: list[dict[str, Any]] = [
        {"$match": {"person_id": {"$in": person_ids}}},
        {"$group": {"_id": "$person_id", "n": {"$sum": 1}}},
    ]
    cursor = await get_db()["notes"].aggregate(pipeline)
    return {row["_id"]: row["n"] async for row in cursor}


def note_out(note: Note, user: User) -> NoteOut:
    return NoteOut(
        **note.model_dump(include=set(NoteOut.model_fields) - {"can_edit"}),
        can_edit=user.role == "admin" or note.author == user.email,
    )


async def _editable(note_id: str, user: User) -> Note:
    doc = await get_db()["notes"].find_one({"_id": note_id})
    if doc is None:
        raise HTTPException(404, "Note not found")
    note = Note.model_validate(doc)
    if not note_out(note, user).can_edit:
        raise HTTPException(403, "Only the note's author or an admin can change it")
    return note


@router.get("/people/{person_id}/notes", response_model=list[NoteOut])
async def list_notes(person_id: str, user: CurrentUser) -> list[NoteOut]:
    """For the People list, which shows a person's notes without loading their whole profile."""
    return [note_out(note, user) for note in await person_notes(person_id)]


@router.post("/people/{person_id}/notes", response_model=NoteOut, status_code=201)
async def add_note(person_id: str, body: NoteIn, user: CurrentUser) -> NoteOut:
    db = get_db()
    if await db["people"].count_documents({"_id": person_id}, limit=1) == 0:
        raise HTTPException(404, "Person not found")
    note = Note(
        id=uuid.uuid4().hex,
        person_id=person_id,
        text=body.text,
        author=user.email,
        created_at=datetime.now(UTC),
        use_in_brief=body.use_in_brief,
    )
    await db["notes"].insert_one(note.to_mongo())
    await track(db, user.email, "note_added", person_id)
    return note_out(note, user)


@router.patch("/notes/{note_id}", response_model=NoteOut)
async def edit_note(note_id: str, body: NoteIn, user: CurrentUser) -> NoteOut:
    note = await _editable(note_id, user)
    changes = {"text": body.text, "use_in_brief": body.use_in_brief, "updated_at": datetime.now(UTC)}
    await get_db()["notes"].update_one({"_id": note.id}, {"$set": changes})
    return note_out(note.model_copy(update=changes), user)


@router.delete("/notes/{note_id}", status_code=204)
async def delete_note(note_id: str, user: CurrentUser) -> None:
    note = await _editable(note_id, user)
    await get_db()["notes"].delete_one({"_id": note.id})
    await track(get_db(), user.email, "note_deleted", note.person_id)

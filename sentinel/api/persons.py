"""
sentinel/api/persons.py — Named-person management endpoints.

Endpoints:
  GET    /api/persons                       — list all persons with event counts
  POST   /api/persons                       — create a new person (name only, enroll later)
  PATCH  /api/persons/{id}                  — rename a person
  DELETE /api/persons/{id}                  — delete person (encodings cascade; events unlinked)
  POST   /api/persons/{id}/enroll           — enroll a face event into this person
  DELETE /api/persons/{id}/encodings/{enc_id} — remove one specific encoding
  POST   /api/events/{event_id}/assign-person — create/assign person from a face event
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sentinel.db import get_db
from sentinel.util.auth import get_current_user
from sentinel.util.logging import get_logger
from sentinel.util.roles import require_operator_or_admin

log = get_logger("sentinel.api.persons")
router = APIRouter(dependencies=[Depends(get_current_user)])

# Writes (create/rename/delete/enroll) require admin or operator, matching the
# RBAC tier used for camera writes. Reads stay open to any authenticated user,
# including viewer, same as events/recordings.
_write = Depends(require_operator_or_admin())


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class PersonCreate(BaseModel):
    name: str

class PersonRename(BaseModel):
    name: str

class EnrollRequest(BaseModel):
    event_id: int          # face event whose face_embedding to enroll

class AssignPersonRequest(BaseModel):
    name: str              # create person if not exists, then enroll + assign event
    event_id: Optional[int] = None  # if None, uses the URL event_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _person_row_to_dict(row, db) -> dict:
    count = db.execute(
        "SELECT COUNT(*) FROM face_encodings WHERE person_id=?", (row["id"],)
    ).fetchone()[0]
    event_count = db.execute(
        "SELECT COUNT(*) FROM events WHERE person_id=?", (row["id"],)
    ).fetchone()[0]
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "encoding_count": count,
        "event_count": event_count,
    }


def _get_embedding_for_event(db, event_id: int):
    """Return the face_embedding blob for an event, or raise 422 if missing."""
    import numpy as np
    row = db.execute(
        "SELECT face_embedding FROM events WHERE id=?", (event_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, f"Event {event_id} not found")
    if not row["face_embedding"]:
        raise HTTPException(422, f"Event {event_id} has no stored face embedding — was it a face event?")
    raw = row["face_embedding"]
    # Stored as raw float32 bytes (512-d = 2048 bytes). Never unpickle DB blobs.
    if len(raw) != 512 * 4:
        raise HTTPException(422, f"Event {event_id} has an unrecognised face embedding format")
    return np.frombuffer(raw, dtype=np.float32).copy()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/persons")
def list_persons():
    db = get_db()
    rows = db.execute("SELECT * FROM persons ORDER BY name").fetchall()
    return [_person_row_to_dict(r, db) for r in rows]


@router.post("/persons", status_code=201, dependencies=[_write])
def create_person(body: PersonCreate):
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "name is required")
    db = get_db()
    existing = db.execute("SELECT id FROM persons WHERE name=?", (name,)).fetchone()
    if existing:
        raise HTTPException(409, f"Person '{name}' already exists")
    cur = db.execute(
        "INSERT INTO persons (name, created_at) VALUES (?, ?)",
        (name, _now()),
    )
    db.commit()
    row = db.execute("SELECT * FROM persons WHERE id=?", (cur.lastrowid,)).fetchone()
    log.info("[persons] created person id=%d name=%r", cur.lastrowid, name)
    return _person_row_to_dict(row, db)


@router.patch("/persons/{person_id}", dependencies=[_write])
def rename_person(person_id: int, body: PersonRename):
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "name is required")
    db = get_db()
    row = db.execute("SELECT * FROM persons WHERE id=?", (person_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"Person {person_id} not found")
    conflict = db.execute(
        "SELECT id FROM persons WHERE name=? AND id!=?", (name, person_id)
    ).fetchone()
    if conflict:
        raise HTTPException(409, f"Person '{name}' already exists")
    db.execute("UPDATE persons SET name=? WHERE id=?", (name, person_id))
    db.commit()
    row = db.execute("SELECT * FROM persons WHERE id=?", (person_id,)).fetchone()
    return _person_row_to_dict(row, db)


@router.delete("/persons/{person_id}", status_code=204, dependencies=[_write])
def delete_person(person_id: int):
    db = get_db()
    if not db.execute("SELECT id FROM persons WHERE id=?", (person_id,)).fetchone():
        raise HTTPException(404, f"Person {person_id} not found")
    # Delete from DB first; update the in-memory cache only after the commit
    # succeeds.  If the SQL DELETE fails the cache is never touched, keeping
    # it in sync with the persisted data.
    # ON DELETE CASCADE on face_encodings handles the DB rows in one atomic commit.
    db.execute("DELETE FROM persons WHERE id=?", (person_id,))
    db.commit()
    from sentinel.services.recognition import _remove_person_from_cache
    _remove_person_from_cache(person_id)
    log.info("[persons] deleted person id=%d", person_id)


@router.post("/persons/{person_id}/enroll", dependencies=[_write])
def enroll_face(person_id: int, body: EnrollRequest):
    """Enroll a face event's embedding into this person."""
    db = get_db()
    if not db.execute("SELECT id FROM persons WHERE id=?", (person_id,)).fetchone():
        raise HTTPException(404, f"Person {person_id} not found")
    embedding = _get_embedding_for_event(db, body.event_id)
    from sentinel.services.recognition import enroll
    enc_id = enroll(db, person_id, embedding, event_id=body.event_id)
    # Tag the event
    db.execute(
        "UPDATE events SET person_id=? WHERE id=?", (person_id, body.event_id)
    )
    db.commit()
    log.info("[persons] enrolled event %d into person %d (enc %d)", body.event_id, person_id, enc_id)
    return {"encoding_id": enc_id, "person_id": person_id, "event_id": body.event_id}


@router.delete("/persons/{person_id}/encodings/{enc_id}", status_code=204, dependencies=[_write])
def remove_encoding(person_id: int, enc_id: int):
    db = get_db()
    row = db.execute(
        "SELECT id FROM face_encodings WHERE id=? AND person_id=?", (enc_id, person_id)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Encoding not found")
    from sentinel.services.recognition import remove_encoding as _remove
    _remove(db, enc_id)
    log.info("[persons] removed encoding %d from person %d", enc_id, person_id)


@router.post("/events/{event_id}/assign-person", dependencies=[_write])
def assign_person_to_event(event_id: int, body: AssignPersonRequest):
    """
    Create a person by name (if not existing), enroll this event's face,
    and tag the event — all in one call.
    """
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "name is required")
    db = get_db()

    # Verify event is a face event with an embedding
    embedding = _get_embedding_for_event(db, event_id)

    # Create person if not exists
    existing = db.execute("SELECT id FROM persons WHERE name=?", (name,)).fetchone()
    if existing:
        person_id = existing["id"]
    else:
        cur = db.execute(
            "INSERT INTO persons (name, created_at) VALUES (?, ?)", (name, _now())
        )
        db.commit()
        person_id = cur.lastrowid
        log.info("[persons] auto-created person id=%d name=%r", person_id, name)

    # Enroll this event's embedding
    from sentinel.services.recognition import enroll
    enc_id = enroll(db, person_id, embedding, event_id=event_id)

    # Tag the event with person_id
    db.execute("UPDATE events SET person_id=? WHERE id=?", (person_id, event_id))
    db.commit()

    person_row = db.execute("SELECT * FROM persons WHERE id=?", (person_id,)).fetchone()
    log.info("[persons] assigned event %d → person %d %r", event_id, person_id, name)
    return {
        "person": _person_row_to_dict(person_row, db),
        "encoding_id": enc_id,
        "event_id": event_id,
    }

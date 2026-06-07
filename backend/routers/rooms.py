from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config import settings
from db import crud
from db.session import get_db
import vault
from agent.mcp_client import call_swiggy_tool

router = APIRouter(prefix="/rooms", tags=["rooms"])
_signer = URLSafeTimedSerializer(settings.SECRET_KEY)


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _require_host(circle_session: str | None = Cookie(default=None)) -> str:
    if not circle_session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        return _signer.loads(circle_session, max_age=86400 * 30)["uid"]
    except BadSignature:
        raise HTTPException(status_code=401, detail="Invalid session")


def _get_valid_token(db: Session, host_uid: str) -> str:
    row = crud.get_host_token(db, host_uid)
    if not row:
        raise HTTPException(status_code=401, detail="re_auth_required")
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="re_auth_required")
    return vault.decrypt(bytes(row.encrypted_token))["access_token"]


# ── Request/response models ───────────────────────────────────────────────────

class CreateRoomBody(BaseModel):
    display_name: str = "Host"


class SetAddressBody(BaseModel):
    address_id: str


class JoinBody(BaseModel):
    display_name: str


class CravingCardBody(BaseModel):
    participant_id: str
    veg: str = "either"
    budget_max: int | None = None
    cuisine_vibe: str | None = None
    must_have: str | None = None
    allergies: list[str] = []
    deal_breakers: list[str] = []


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("")
def create_room(
    body: CreateRoomBody,
    host_uid: str = Depends(_require_host),
    db: Session = Depends(get_db),
):
    room, host = crud.create_room(db, host_uid, body.display_name)
    join_url = f"{settings.FRONTEND_URL}/join/{room.id}"
    return {
        "room_id": room.id,
        "participant_id": host.id,
        "join_url": join_url,
        "room_code": room.id[:8].upper(),
    }


@router.get("/{room_id}")
def get_room(room_id: str, db: Session = Depends(get_db)):
    room = crud.get_room(db, room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    participants = crud.get_participants(db, room_id)
    cards = crud.get_craving_cards(db, room_id)
    card_pids = {c.participant_id for c in cards}
    return {
        "id": room.id,
        "status": room.status,
        "address_id": room.address_id,
        "host_user_id": room.host_user_id,
        "room_code": room.id[:8].upper(),
        "participants": [
            {
                "id": p.id,
                "display_name": p.display_name,
                "is_host": p.is_host,
                "has_card": p.id in card_pids,
            }
            for p in participants
        ],
        "cards_submitted": len(cards),
        "total_participants": len(participants),
    }


@router.post("/{room_id}/address")
def set_address(
    room_id: str,
    body: SetAddressBody,
    host_uid: str = Depends(_require_host),
    db: Session = Depends(get_db),
):
    room = crud.get_room(db, room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if room.host_user_id != host_uid:
        raise HTTPException(status_code=403, detail="Only the host can set the address")
    crud.set_room_address(db, room_id, body.address_id)
    return {"ok": True}


@router.post("/{room_id}/join")
def join_room(room_id: str, body: JoinBody, db: Session = Depends(get_db)):
    room = crud.get_room(db, room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if room.status != "collecting":
        raise HTTPException(status_code=409, detail="Room is no longer accepting guests")
    if not body.display_name.strip():
        raise HTTPException(status_code=422, detail="Display name required")
    p = crud.create_participant(db, room_id, body.display_name.strip())
    return {"participant_id": p.id, "display_name": p.display_name}


@router.delete("/{room_id}/participants/{participant_id}")
def kick_participant(
    room_id: str,
    participant_id: str,
    host_uid: str = Depends(_require_host),
    db: Session = Depends(get_db),
):
    room = crud.get_room(db, room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if room.host_user_id != host_uid:
        raise HTTPException(status_code=403, detail="Only the host can kick participants")
    p = crud.get_participant(db, participant_id)
    if p and p.is_host:
        raise HTTPException(status_code=400, detail="Cannot kick the host")
    crud.delete_participant(db, participant_id)
    return {"ok": True}


@router.post("/{room_id}/cards")
def submit_card(
    room_id: str,
    body: CravingCardBody,
    db: Session = Depends(get_db),
):
    room = crud.get_room(db, room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    p = crud.get_participant(db, body.participant_id)
    if not p or p.room_id != room_id:
        raise HTTPException(status_code=403, detail="Participant not in this room")
    crud.upsert_craving_card(
        db, body.participant_id, room_id,
        veg=body.veg,
        budget_max=body.budget_max,
        cuisine_vibe=body.cuisine_vibe,
        must_have=body.must_have,
        allergies=body.allergies,
        deal_breakers=body.deal_breakers,
    )
    return {"ok": True}


@router.get("/{room_id}/addresses")
async def get_addresses(
    room_id: str,
    host_uid: str = Depends(_require_host),
    db: Session = Depends(get_db),
):
    access_token = _get_valid_token(db, host_uid)
    addresses = await call_swiggy_tool("get_addresses", {}, access_token)
    return {"addresses": addresses}

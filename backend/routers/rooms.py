from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, HTTPException
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


class ApprovePrefsBody(BaseModel):
    edits: list[dict] = []   # optional host edits — reserved for Phase 4


class VoteBody(BaseModel):
    participant_id: str
    plan_id: str


class ChoosePlanBody(BaseModel):
    plan_id: str


def _plan_to_dict(p) -> dict:
    return {
        "id": p.id,
        "kind": p.kind,
        "sub_orders": p.sub_orders,
        "total": p.total,
        "satisfaction": float(p.satisfaction) if p.satisfaction is not None else None,
        "n_deliveries": p.n_deliveries,
        "notes": p.notes,
        "rationale": p.rationale,
        "why_not_runner_up": p.why_not_runner_up,
        "per_person_fit": p.per_person_fit or {},
        "rank": p.rank,
        "chosen": p.chosen,
    }


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


# ── Phase 3: Agent activation & pref approval ─────────────────────────────────

@router.post("/{room_id}/activate")
async def activate_room(
    room_id: str,
    background_tasks: BackgroundTasks,
    host_uid: str = Depends(_require_host),
    db: Session = Depends(get_db),
):
    room = crud.get_room(db, room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if room.host_user_id != host_uid:
        raise HTTPException(status_code=403, detail="Only the host can activate")
    if room.status not in ("collecting",):
        raise HTTPException(status_code=409, detail=f"Room already activated (status={room.status})")
    if not room.address_id:
        raise HTTPException(status_code=422, detail="Set a delivery address before activating")

    # Ensure all participants have submitted cards
    participants = crud.get_participants(db, room_id)
    cards = crud.get_craving_cards(db, room_id)
    card_pids = {c.participant_id for c in cards}
    missing = [p for p in participants if p.id not in card_pids]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"{len(missing)} participant(s) haven't submitted cards yet",
        )

    # Validate the host token is present/unexpired before kicking off the agent
    _get_valid_token(db, host_uid)
    crud.set_room_status(db, room_id, "activated")

    # Import here to avoid importing the agent stack at module load
    from agent.runner import run_parse

    background_tasks.add_task(run_parse, room_id, host_uid, room.address_id)

    return {"ok": True, "status": "activated"}


@router.get("/{room_id}/pref-specs")
def get_pref_specs(
    room_id: str,
    db: Session = Depends(get_db),
):
    specs = crud.get_pref_specs(db, room_id)
    return {
        "pref_specs": [
            {
                "id": s.id,
                "participant_id": s.participant_id,
                "room_id": s.room_id,
                "veg": s.veg,
                "budget_max": s.budget_max,
                "allergies": list(s.allergies) if s.allergies else [],
                "excludes": list(s.excludes) if s.excludes else [],
                "soft": list(s.soft) if s.soft else [],
                "approved": s.approved,
            }
            for s in specs
        ]
    }


@router.post("/{room_id}/approve-prefs")
async def approve_prefs(
    room_id: str,
    body: ApprovePrefsBody,
    background_tasks: BackgroundTasks,
    host_uid: str = Depends(_require_host),
    db: Session = Depends(get_db),
):
    room = crud.get_room(db, room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if room.host_user_id != host_uid:
        raise HTTPException(status_code=403, detail="Only the host can approve prefs")
    if room.status != "planning":
        raise HTTPException(
            status_code=409,
            detail=f"Room is not waiting for pref approval (status={room.status})",
        )

    crud.approve_pref_specs(db, room_id)
    crud.set_room_status(db, room_id, "discovering")

    access_token = _get_valid_token(db, host_uid)

    from agent.runner import run_discover
    background_tasks.add_task(run_discover, room_id, access_token)

    return {"ok": True, "status": "discovering"}


# ── Phase 4: Plans, voting & selection ────────────────────────────────────────

@router.get("/{room_id}/plans")
def get_plans(room_id: str, db: Session = Depends(get_db)):
    plans = crud.get_plans(db, room_id)
    counts = crud.get_vote_counts(db, room_id)
    votes = crud.get_votes(db, room_id)
    return {
        "plans": [_plan_to_dict(p) for p in plans],
        "vote_counts": counts,
        "votes": votes,
    }


@router.post("/{room_id}/vote")
def vote(room_id: str, body: VoteBody, db: Session = Depends(get_db)):
    room = crud.get_room(db, room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    p = crud.get_participant(db, body.participant_id)
    if not p or p.room_id != room_id:
        raise HTTPException(status_code=403, detail="Participant not in this room")
    plan = crud.get_plan(db, body.plan_id)
    if not plan or plan.room_id != room_id:
        raise HTTPException(status_code=404, detail="Plan not found in this room")
    crud.add_plan_vote(db, body.participant_id, body.plan_id)
    return {"ok": True, "vote_counts": crud.get_vote_counts(db, room_id)}


@router.post("/{room_id}/choose-plan")
def choose_plan(
    room_id: str,
    body: ChoosePlanBody,
    host_uid: str = Depends(_require_host),
    db: Session = Depends(get_db),
):
    room = crud.get_room(db, room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if room.host_user_id != host_uid:
        raise HTTPException(status_code=403, detail="Only the host can choose the plan")
    plan = crud.get_plan(db, body.plan_id)
    if not plan or plan.room_id != room_id:
        raise HTTPException(status_code=404, detail="Plan not found in this room")

    crud.set_plan_chosen(db, room_id, body.plan_id)
    crud.set_room_status(db, room_id, "ordering")
    # The order segment (build_cart → split → confirm) is Phase 5.
    return {"ok": True, "status": "ordering", "chosen_plan_id": body.plan_id}

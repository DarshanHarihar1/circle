import uuid
from datetime import datetime
from sqlalchemy.orm import Session
from .models import Candidate, CravingCard, HostToken, Participant, PrefSpec, Room


# ── Host tokens ───────────────────────────────────────────────────────────────

def upsert_host_token(db: Session, host_user_id: str, encrypted_token: bytes,
                      expires_at: datetime) -> None:
    row = db.get(HostToken, host_user_id)
    if row:
        row.encrypted_token = encrypted_token
        row.expires_at = expires_at
        row.updated_at = datetime.utcnow()
    else:
        db.add(HostToken(host_user_id=host_user_id,
                         encrypted_token=encrypted_token,
                         expires_at=expires_at))
    db.commit()


def get_host_token(db: Session, host_user_id: str) -> HostToken | None:
    return db.get(HostToken, host_user_id)


# ── Rooms ─────────────────────────────────────────────────────────────────────

def create_room(db: Session, host_user_id: str,
                display_name: str = "Host") -> tuple[Room, Participant]:
    room = Room(id=str(uuid.uuid4()), host_user_id=host_user_id, status="collecting")
    db.add(room)
    db.flush()  # write room row before participant FK reference
    host = Participant(id=str(uuid.uuid4()), room_id=room.id,
                       display_name=display_name, is_host=True)
    db.add(host)
    db.commit()
    db.refresh(room)
    db.refresh(host)
    return room, host


def get_room(db: Session, room_id: str) -> Room | None:
    return db.get(Room, room_id)


def set_room_address(db: Session, room_id: str, address_id: str) -> None:
    room = db.get(Room, room_id)
    if room:
        room.address_id = address_id
        db.commit()


def set_room_status(db: Session, room_id: str, status: str) -> None:
    room = db.get(Room, room_id)
    if room:
        room.status = status
        db.commit()


# ── Participants ──────────────────────────────────────────────────────────────

def create_participant(db: Session, room_id: str,
                       display_name: str) -> Participant:
    p = Participant(id=str(uuid.uuid4()), room_id=room_id,
                    display_name=display_name, is_host=False)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def get_participant(db: Session, participant_id: str) -> Participant | None:
    return db.get(Participant, participant_id)


def get_participants(db: Session, room_id: str) -> list[Participant]:
    return db.query(Participant).filter(Participant.room_id == room_id).all()


def delete_participant(db: Session, participant_id: str) -> None:
    p = db.get(Participant, participant_id)
    if p:
        db.delete(p)
        db.commit()


# ── Craving cards ─────────────────────────────────────────────────────────────

def upsert_craving_card(db: Session, participant_id: str, room_id: str,
                        veg: str, budget_max: int | None, cuisine_vibe: str | None,
                        must_have: str | None, allergies: list,
                        deal_breakers: list) -> CravingCard:
    existing = (db.query(CravingCard)
                .filter(CravingCard.participant_id == participant_id)
                .first())
    if existing:
        existing.veg = veg
        existing.budget_max = budget_max
        existing.cuisine_vibe = cuisine_vibe
        existing.must_have = must_have
        existing.allergies = allergies
        existing.deal_breakers = deal_breakers
        db.commit()
        return existing
    card = CravingCard(id=str(uuid.uuid4()), participant_id=participant_id,
                       room_id=room_id, veg=veg, budget_max=budget_max,
                       cuisine_vibe=cuisine_vibe, must_have=must_have,
                       allergies=allergies, deal_breakers=deal_breakers)
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def get_craving_cards(db: Session, room_id: str) -> list[CravingCard]:
    return db.query(CravingCard).filter(CravingCard.room_id == room_id).all()


# ── Pref specs ────────────────────────────────────────────────────────────────

def upsert_pref_spec(db: Session, spec) -> PrefSpec:
    """spec is an agent.state.PrefSpec pydantic model."""
    existing = (
        db.query(PrefSpec)
        .filter(PrefSpec.participant_id == spec.participant_id)
        .first()
    )
    if existing:
        existing.veg = spec.veg
        existing.budget_max = spec.budget_max
        existing.allergies = spec.allergies
        existing.excludes = spec.excludes
        existing.soft = spec.soft
        existing.updated_at = datetime.utcnow()
        db.commit()
        return existing

    row = PrefSpec(
        id=str(uuid.uuid4()),
        participant_id=spec.participant_id,
        room_id=spec.participant_id,  # overwritten below
        veg=spec.veg,
        budget_max=spec.budget_max,
        allergies=spec.allergies,
        excludes=spec.excludes,
        soft=spec.soft,
        raw_chat="",
        approved=False,
    )
    # resolve room_id from participant
    p = db.get(Participant, spec.participant_id)
    if p:
        row.room_id = p.room_id
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_pref_specs(db: Session, room_id: str) -> list[PrefSpec]:
    return db.query(PrefSpec).filter(PrefSpec.room_id == room_id).all()


def approve_pref_specs(db: Session, room_id: str) -> None:
    db.query(PrefSpec).filter(PrefSpec.room_id == room_id).update({"approved": True})
    db.commit()


# ── Candidates ────────────────────────────────────────────────────────────────

def create_candidate(db: Session, room_id: str, c: dict) -> Candidate:
    row = Candidate(
        id=str(uuid.uuid4()),
        room_id=room_id,
        restaurant_id=c["id"],
        restaurant_name=c.get("name", "Unknown"),
        cuisines=c.get("cuisines") or [],
        rating=c.get("rating"),
        cost_for_two=c.get("cost_for_two"),
        distance_km=c.get("distance_km"),
        availability=c.get("availability", "OPEN"),
        raw_metadata=c.get("metadata") or c,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def delete_candidates(db: Session, room_id: str) -> None:
    db.query(Candidate).filter(Candidate.room_id == room_id).delete()
    db.commit()


def get_candidates(db: Session, room_id: str) -> list[Candidate]:
    return db.query(Candidate).filter(Candidate.room_id == room_id).all()


def candidates_as_dicts(db: Session, room_id: str) -> list[dict]:
    """Rehydrate persisted candidates into the dict shape discover/feasibility use."""
    out = []
    for c in get_candidates(db, room_id):
        out.append({
            "id": c.restaurant_id,
            "name": c.restaurant_name,
            "cuisines": c.cuisines or [],
            "rating": float(c.rating) if c.rating is not None else None,
            "cost_for_two": c.cost_for_two,
            "distance_km": float(c.distance_km) if c.distance_km is not None else None,
            "availability": c.availability,
            "metadata": c.raw_metadata or {},
        })
    return out


def pref_specs_as_dicts(db: Session, room_id: str) -> list[dict]:
    """Rehydrate persisted pref_specs into PrefSpec.model_dump() shape."""
    out = []
    for s in get_pref_specs(db, room_id):
        p = get_participant(db, s.participant_id)
        out.append({
            "participant_id": s.participant_id,
            "display_name": p.display_name if p else s.participant_id[:8],
            "veg": s.veg,
            "budget_max": s.budget_max,
            "allergies": list(s.allergies) if s.allergies else [],
            "excludes": list(s.excludes) if s.excludes else [],
            "soft": list(s.soft) if s.soft else [],
        })
    return out

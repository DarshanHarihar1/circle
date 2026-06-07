import uuid
from datetime import datetime
from sqlalchemy.orm import Session
from .models import CravingCard, HostToken, Participant, Room


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

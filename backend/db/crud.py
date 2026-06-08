import uuid
from datetime import datetime
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .models import (
    Candidate, CravingCard, HostToken, Participant, Plan, PlanVote, PlacedOrder,
    PrefSpec, Room, Split,
)


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


# ── Plans ─────────────────────────────────────────────────────────────────────

def create_plan(db: Session, room_id: str, plan: dict) -> Plan:
    row = Plan(
        id=plan["plan_id"],
        room_id=room_id,
        kind=plan["kind"],
        sub_orders=plan["sub_orders"],
        total=int(plan["total"]),
        satisfaction=plan.get("satisfaction"),
        n_deliveries=int(plan["n_deliveries"]),
        notes=("; ".join(plan["notes_uncovered"]) if plan.get("notes_uncovered") else None),
        rationale=plan.get("rationale"),
        why_not_runner_up=plan.get("why_not_runner_up"),
        per_person_fit=plan.get("per_person_fit", {}),
        rank=plan.get("rank"),
        chosen=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_plan_pricing(db: Session, plan_id: str, total: int, sub_orders: list[dict]) -> None:
    row = db.get(Plan, plan_id)
    if row:
        row.total = int(total)
        row.sub_orders = sub_orders
        db.commit()


def delete_plans(db: Session, room_id: str) -> None:
    plan_ids = [p.id for p in db.query(Plan).filter(Plan.room_id == room_id).all()]
    if plan_ids:
        db.query(PlanVote).filter(PlanVote.plan_id.in_(plan_ids)).delete(synchronize_session=False)
    db.query(Plan).filter(Plan.room_id == room_id).delete()
    db.commit()


def get_plans(db: Session, room_id: str) -> list[Plan]:
    return (db.query(Plan)
            .filter(Plan.room_id == room_id)
            .order_by(Plan.rank.asc().nullslast())
            .all())


def get_plan(db: Session, plan_id: str) -> Plan | None:
    return db.get(Plan, plan_id)


def set_plan_chosen(db: Session, room_id: str, plan_id: str) -> None:
    db.query(Plan).filter(Plan.room_id == room_id).update({"chosen": False})
    row = db.get(Plan, plan_id)
    if row:
        row.chosen = True
    db.commit()


# ── Plan votes ────────────────────────────────────────────────────────────────

def add_plan_vote(db: Session, participant_id: str, plan_id: str) -> None:
    # one vote per participant per room — clear prior votes for this participant
    row = db.get(Plan, plan_id)
    if not row:
        return
    sibling_ids = [p.id for p in db.query(Plan).filter(Plan.room_id == row.room_id).all()]
    if sibling_ids:
        # synchronize_session="fetch" keeps the identity map consistent so the
        # re-check below doesn't see a stale (just-deleted) PlanVote instance.
        (db.query(PlanVote)
         .filter(PlanVote.participant_id == participant_id,
                 PlanVote.plan_id.in_(sibling_ids))
         .delete(synchronize_session="fetch"))
        db.flush()
    if not db.get(PlanVote, (participant_id, plan_id)):
        db.add(PlanVote(participant_id=participant_id, plan_id=plan_id))
    try:
        db.commit()
    except IntegrityError:
        # Concurrent vote from the same participant raced us to the same PK.
        # The row exists, which is exactly the desired end state — treat as success.
        db.rollback()


def get_votes(db: Session, room_id: str) -> list[dict]:
    plan_ids = [p.id for p in db.query(Plan).filter(Plan.room_id == room_id).all()]
    if not plan_ids:
        return []
    votes = db.query(PlanVote).filter(PlanVote.plan_id.in_(plan_ids)).all()
    return [{"participant_id": v.participant_id, "plan_id": v.plan_id} for v in votes]


def get_vote_counts(db: Session, room_id: str) -> dict[str, int]:
    plan_ids = [p.id for p in db.query(Plan).filter(Plan.room_id == room_id).all()]
    counts = {pid: 0 for pid in plan_ids}
    if plan_ids:
        votes = db.query(PlanVote).filter(PlanVote.plan_id.in_(plan_ids)).all()
        for v in votes:
            counts[v.plan_id] = counts.get(v.plan_id, 0) + 1
    return counts


# ── Chosen plan helpers ───────────────────────────────────────────────────────

def get_chosen_plan(db: Session, room_id: str) -> Plan | None:
    return db.query(Plan).filter(Plan.room_id == room_id, Plan.chosen == True).first()


def plans_as_dicts(db: Session, room_id: str) -> list[dict]:
    return [
        {
            "plan_id": p.id,
            "kind": p.kind,
            "sub_orders": p.sub_orders,
            "total": p.total,
            "satisfaction": float(p.satisfaction) if p.satisfaction is not None else 0.0,
            "n_deliveries": p.n_deliveries,
            "rationale": p.rationale or "",
            "why_not_runner_up": p.why_not_runner_up or "",
            "per_person_fit": p.per_person_fit or {},
        }
        for p in get_plans(db, room_id)
    ]


# ── Placed orders ─────────────────────────────────────────────────────────────

def create_placed_order(
    db: Session,
    room_id: str,
    plan_id: str,
    swiggy_order_id: str | None,
    restaurant_id: str,
    restaurant_name: str,
    sub_order_data: dict,
) -> PlacedOrder:
    row = PlacedOrder(
        id=str(uuid.uuid4()),
        room_id=room_id,
        plan_id=plan_id,
        swiggy_order_id=swiggy_order_id,
        restaurant_id=restaurant_id,
        restaurant_name=restaurant_name,
        status="placed" if swiggy_order_id else "pending",
        sub_order_data=sub_order_data,
        placed_at=datetime.utcnow() if swiggy_order_id else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_placed_orders(db: Session, room_id: str) -> list[PlacedOrder]:
    return db.query(PlacedOrder).filter(PlacedOrder.room_id == room_id).all()


# ── Splits ────────────────────────────────────────────────────────────────────

def upsert_split(
    db: Session,
    room_id: str,
    participant_id: str,
    amount: int,
    upi_link: str | None = None,
) -> Split:
    existing = (
        db.query(Split)
        .filter(Split.room_id == room_id, Split.participant_id == participant_id)
        .first()
    )
    if existing:
        existing.amount = amount
        if upi_link is not None:
            existing.upi_link = upi_link
        db.commit()
        return existing
    row = Split(
        id=str(uuid.uuid4()),
        room_id=room_id,
        participant_id=participant_id,
        amount=amount,
        upi_link=upi_link,
        paid=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_splits(db: Session, room_id: str) -> list[Split]:
    return db.query(Split).filter(Split.room_id == room_id).all()


def get_split(db: Session, split_id: str) -> Split | None:
    return db.get(Split, split_id)


def mark_split_paid(db: Session, split_id: str, paid: bool | None = None) -> bool | None:
    """Set a split's paid flag. When `paid` is None, toggle (legacy behaviour).

    Passing an explicit bool makes the operation idempotent — safe under retries
    and double-taps, which a blind toggle is not. Returns the resulting state.
    """
    row = db.get(Split, split_id)
    if not row:
        return None
    row.paid = (not row.paid) if paid is None else paid
    db.commit()
    return row.paid


# ── Placed-order tracking ─────────────────────────────────────────────────────

def get_placed_order(db: Session, order_id: str) -> PlacedOrder | None:
    return db.get(PlacedOrder, order_id)


def update_placed_order_status(
    db: Session,
    order_id: str,
    status: str,
    eta_mins: int | None = None,
) -> None:
    row = db.get(PlacedOrder, order_id)
    if not row:
        return
    row.status = status
    if eta_mins is not None:
        data = dict(row.sub_order_data) if row.sub_order_data else {}
        data["eta_mins"] = eta_mins
        row.sub_order_data = data
    db.commit()

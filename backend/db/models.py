from sqlalchemy import (
    Boolean, Column, ForeignKey, Integer, Numeric, Text, TIMESTAMP
)
from sqlalchemy.dialects.postgresql import BYTEA, JSONB
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class Room(Base):
    __tablename__ = "rooms"

    id           = Column(Text, primary_key=True)
    host_user_id = Column(Text, nullable=False)
    address_id   = Column(Text)
    status       = Column(Text, nullable=False, default="collecting")
    created_at   = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at   = Column(TIMESTAMP(timezone=True), server_default=func.now())


class Participant(Base):
    __tablename__ = "participants"

    id           = Column(Text, primary_key=True)
    room_id      = Column(Text, ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False)
    display_name = Column(Text, nullable=False)
    is_host      = Column(Boolean, nullable=False, default=False)
    joined_at    = Column(TIMESTAMP(timezone=True), server_default=func.now())


class CravingCard(Base):
    __tablename__ = "craving_cards"

    id             = Column(Text, primary_key=True)
    participant_id = Column(Text, ForeignKey("participants.id", ondelete="CASCADE"), nullable=False)
    room_id        = Column(Text, nullable=False)
    veg            = Column(Text, nullable=False, default="either")
    budget_max     = Column(Integer)
    cuisine_vibe   = Column(Text)
    must_have      = Column(Text)
    allergies      = Column(JSONB, nullable=False, default=list)
    deal_breakers  = Column(JSONB, nullable=False, default=list)
    submitted_at   = Column(TIMESTAMP(timezone=True), server_default=func.now())


class PrefSpec(Base):
    __tablename__ = "pref_specs"

    id             = Column(Text, primary_key=True)
    participant_id = Column(Text, ForeignKey("participants.id"), nullable=False)
    room_id        = Column(Text, nullable=False)
    veg            = Column(Text, nullable=False)
    budget_max     = Column(Integer)
    allergies      = Column(JSONB, nullable=False, default=list)
    excludes       = Column(JSONB, nullable=False, default=list)
    soft           = Column(JSONB, nullable=False, default=list)
    raw_chat       = Column(Text, nullable=False, default="")
    approved       = Column(Boolean, nullable=False, default=False)
    updated_at     = Column(TIMESTAMP(timezone=True), server_default=func.now())


class Candidate(Base):
    __tablename__ = "candidates"

    id              = Column(Text, primary_key=True)
    room_id         = Column(Text, ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False)
    restaurant_id   = Column(Text, nullable=False)
    restaurant_name = Column(Text, nullable=False)
    cuisines        = Column(JSONB)
    rating          = Column(Numeric(3, 1))
    cost_for_two    = Column(Integer)
    distance_km     = Column(Numeric(4, 2))
    availability    = Column(Text)
    raw_metadata    = Column("metadata", JSONB)


class Plan(Base):
    __tablename__ = "plans"

    id                = Column(Text, primary_key=True)
    room_id           = Column(Text, ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False)
    kind              = Column(Text, nullable=False)
    sub_orders        = Column(JSONB, nullable=False)
    total             = Column(Integer, nullable=False)
    satisfaction      = Column(Numeric(4, 3))
    n_deliveries      = Column(Integer, nullable=False)
    notes             = Column(Text)
    rationale         = Column(Text)
    why_not_runner_up = Column(Text)
    per_person_fit    = Column(JSONB)
    rank              = Column(Integer)
    chosen            = Column(Boolean, nullable=False, default=False)
    created_at        = Column(TIMESTAMP(timezone=True), server_default=func.now())


class PlanVote(Base):
    __tablename__ = "plan_votes"

    participant_id = Column(Text, ForeignKey("participants.id"), primary_key=True)
    plan_id        = Column(Text, ForeignKey("plans.id"), primary_key=True)
    voted_at       = Column(TIMESTAMP(timezone=True), server_default=func.now())


class PlacedOrder(Base):
    __tablename__ = "placed_orders"

    id              = Column(Text, primary_key=True)
    room_id         = Column(Text, ForeignKey("rooms.id"), nullable=False)
    plan_id         = Column(Text, ForeignKey("plans.id"))
    swiggy_order_id = Column(Text)
    restaurant_id   = Column(Text, nullable=False)
    restaurant_name = Column(Text, nullable=False)
    status          = Column(Text, nullable=False, default="pending")
    sub_order_data  = Column(JSONB)
    placed_at       = Column(TIMESTAMP(timezone=True))


class Split(Base):
    __tablename__ = "splits"

    id             = Column(Text, primary_key=True)
    room_id        = Column(Text, ForeignKey("rooms.id"), nullable=False)
    participant_id = Column(Text, ForeignKey("participants.id"), nullable=False)
    amount         = Column(Integer, nullable=False)
    upi_link       = Column(Text)
    paid           = Column(Boolean, nullable=False, default=False)


class HostToken(Base):
    __tablename__ = "host_tokens"

    host_user_id    = Column(Text, primary_key=True)
    encrypted_token = Column(BYTEA, nullable=False)
    expires_at      = Column(TIMESTAMP(timezone=True), nullable=False)
    updated_at      = Column(TIMESTAMP(timezone=True), server_default=func.now())

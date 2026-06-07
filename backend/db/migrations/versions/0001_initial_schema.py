"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-06-07
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import BYTEA, JSONB

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    op.create_table(
        "rooms",
        sa.Column("id", sa.Text, primary_key=True,
                  server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("host_user_id", sa.Text, nullable=False),
        sa.Column("address_id", sa.Text),
        sa.Column("status", sa.Text, nullable=False, server_default="collecting"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "participants",
        sa.Column("id", sa.Text, primary_key=True,
                  server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("room_id", sa.Text,
                  sa.ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("is_host", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("joined_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "craving_cards",
        sa.Column("id", sa.Text, primary_key=True,
                  server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("participant_id", sa.Text,
                  sa.ForeignKey("participants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("room_id", sa.Text, nullable=False),
        sa.Column("veg", sa.Text, nullable=False, server_default="either"),
        sa.Column("budget_max", sa.Integer),
        sa.Column("cuisine_vibe", sa.Text),
        sa.Column("must_have", sa.Text),
        sa.Column("allergies", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("deal_breakers", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("submitted_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "pref_specs",
        sa.Column("id", sa.Text, primary_key=True,
                  server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("participant_id", sa.Text,
                  sa.ForeignKey("participants.id"), nullable=False),
        sa.Column("room_id", sa.Text, nullable=False),
        sa.Column("veg", sa.Text, nullable=False),
        sa.Column("budget_max", sa.Integer),
        sa.Column("allergies", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("excludes", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("soft", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("raw_chat", sa.Text, nullable=False, server_default="''"),
        sa.Column("approved", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "candidates",
        sa.Column("id", sa.Text, primary_key=True,
                  server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("room_id", sa.Text,
                  sa.ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("restaurant_id", sa.Text, nullable=False),
        sa.Column("restaurant_name", sa.Text, nullable=False),
        sa.Column("cuisines", JSONB),
        sa.Column("rating", sa.Numeric(3, 1)),
        sa.Column("cost_for_two", sa.Integer),
        sa.Column("distance_km", sa.Numeric(4, 2)),
        sa.Column("availability", sa.Text),
        sa.Column("metadata", JSONB),
    )

    op.create_table(
        "plans",
        sa.Column("id", sa.Text, primary_key=True,
                  server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("room_id", sa.Text,
                  sa.ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("sub_orders", JSONB, nullable=False),
        sa.Column("total", sa.Integer, nullable=False),
        sa.Column("satisfaction", sa.Numeric(4, 3)),
        sa.Column("n_deliveries", sa.Integer, nullable=False),
        sa.Column("notes", sa.Text),
        sa.Column("rationale", sa.Text),
        sa.Column("why_not_runner_up", sa.Text),
        sa.Column("per_person_fit", JSONB),
        sa.Column("rank", sa.Integer),
        sa.Column("chosen", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "plan_votes",
        sa.Column("participant_id", sa.Text,
                  sa.ForeignKey("participants.id"), primary_key=True),
        sa.Column("plan_id", sa.Text,
                  sa.ForeignKey("plans.id"), primary_key=True),
        sa.Column("voted_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "placed_orders",
        sa.Column("id", sa.Text, primary_key=True,
                  server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("room_id", sa.Text,
                  sa.ForeignKey("rooms.id"), nullable=False),
        sa.Column("plan_id", sa.Text, sa.ForeignKey("plans.id")),
        sa.Column("swiggy_order_id", sa.Text),
        sa.Column("restaurant_id", sa.Text, nullable=False),
        sa.Column("restaurant_name", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("sub_order_data", JSONB),
        sa.Column("placed_at", sa.TIMESTAMP(timezone=True)),
    )

    op.create_table(
        "splits",
        sa.Column("id", sa.Text, primary_key=True,
                  server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("room_id", sa.Text,
                  sa.ForeignKey("rooms.id"), nullable=False),
        sa.Column("participant_id", sa.Text,
                  sa.ForeignKey("participants.id"), nullable=False),
        sa.Column("amount", sa.Integer, nullable=False),
        sa.Column("upi_link", sa.Text),
        sa.Column("paid", sa.Boolean, nullable=False, server_default="false"),
    )

    op.create_table(
        "host_tokens",
        sa.Column("host_user_id", sa.Text, primary_key=True),
        sa.Column("encrypted_token", BYTEA, nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )

    # Enable RLS
    for table in ["rooms", "participants", "craving_cards", "pref_specs", "candidates",
                  "plans", "plan_votes", "placed_orders", "splits", "host_tokens"]:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    for table in ["host_tokens", "splits", "placed_orders", "plan_votes", "plans",
                  "candidates", "pref_specs", "craving_cards", "participants", "rooms"]:
        op.drop_table(table)

"""backfill realtime publication + RLS policies for phase 2 tables

The Phase 2 tables (rooms, participants, craving_cards) have RLS enabled in
0001 but were never added to the supabase_realtime publication nor given SELECT
policies — those were applied ad-hoc against the live project during Phase 2.
RLS-enabled tables with no policy deny all reads to the anon role, and Realtime
only delivers rows the subscribing role can SELECT, so a fresh deploy from
migrations would have silently broken every participant/card/status live update
the frontend subscribes to. This migration makes that history reproducible.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-08
"""
from typing import Sequence, Union
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("rooms", "participants", "craving_cards")

# Frontend writes participants (join) and craving_cards (submit/update) directly,
# so those need INSERT/UPDATE in addition to SELECT.
_POLICIES = {
    "rooms": [("rooms_select_all", "SELECT")],
    "participants": [
        ("participants_select_all", "SELECT"),
        ("participants_insert_all", "INSERT"),
    ],
    "craving_cards": [
        ("craving_cards_select_all", "SELECT"),
        ("craving_cards_insert_all", "INSERT"),
        ("craving_cards_update_all", "UPDATE"),
    ],
}


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"""
            DO $$
            BEGIN
                ALTER PUBLICATION supabase_realtime ADD TABLE {table};
            EXCEPTION
                WHEN others THEN NULL;
            END $$;
        """)

    for table, policies in _POLICIES.items():
        for name, action in policies:
            clause = "WITH CHECK (true)" if action in ("INSERT",) else "USING (true)"
            if action == "UPDATE":
                clause = "USING (true) WITH CHECK (true)"
            op.execute(f"""
                DO $$
                BEGIN
                    CREATE POLICY {name} ON {table}
                        FOR {action} TO public {clause};
                EXCEPTION
                    WHEN duplicate_object THEN NULL;
                END $$;
            """)


def downgrade() -> None:
    for table, policies in _POLICIES.items():
        for name, _action in policies:
            op.execute(f"DROP POLICY IF EXISTS {name} ON {table}")
    for table in _TABLES:
        op.execute(f"""
            DO $$
            BEGIN
                ALTER PUBLICATION supabase_realtime DROP TABLE {table};
            EXCEPTION
                WHEN others THEN NULL;
            END $$;
        """)

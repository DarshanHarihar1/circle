"""select policies for plans + plan_votes (phase 4 realtime)

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-07
"""
from typing import Sequence, Union
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # plans and plan_votes are already in the realtime publication; realtime
    # only delivers rows the subscribing (anon) role can SELECT, so add
    # permissive SELECT policies mirroring the other shared tables.
    for table in ("plans", "plan_votes"):
        op.execute(f"""
            DO $$
            BEGIN
                CREATE POLICY {table}_select_all ON {table}
                    FOR SELECT TO public USING (true);
            EXCEPTION
                WHEN duplicate_object THEN NULL;
            END $$;
        """)


def downgrade() -> None:
    for table in ("plans", "plan_votes"):
        op.execute(f"DROP POLICY IF EXISTS {table}_select_all ON {table}")

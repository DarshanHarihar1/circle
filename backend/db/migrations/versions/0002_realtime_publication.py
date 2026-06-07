"""enable realtime + select policies for phase 3 tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-07
"""
from typing import Sequence, Union
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add tables to Supabase realtime publication so the frontend can subscribe
    # to INSERT/UPDATE events on parsed preferences and discovered candidates.
    for table in ("pref_specs", "candidates"):
        op.execute(f"""
            DO $$
            BEGIN
                ALTER PUBLICATION supabase_realtime ADD TABLE {table};
            EXCEPTION
                WHEN others THEN NULL;
            END $$;
        """)

    # Permissive SELECT policies (RLS is enabled on every table). Realtime only
    # delivers rows the subscribing role can SELECT — mirror the Phase 2 tables.
    for table in ("pref_specs", "candidates"):
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
    for table in ("pref_specs", "candidates"):
        op.execute(f"DROP POLICY IF EXISTS {table}_select_all ON {table}")
        op.execute(f"""
            DO $$
            BEGIN
                ALTER PUBLICATION supabase_realtime DROP TABLE {table};
            EXCEPTION
                WHEN others THEN NULL;
            END $$;
        """)

"""Add SELECT RLS policies for placed_orders and splits tables.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-08
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            ALTER TABLE placed_orders ENABLE ROW LEVEL SECURITY;
        EXCEPTION WHEN others THEN NULL;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            ALTER TABLE splits ENABLE ROW LEVEL SECURITY;
        EXCEPTION WHEN others THEN NULL;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE POLICY placed_orders_select_all ON placed_orders
                FOR SELECT USING (true);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE POLICY splits_select_all ON splits
                FOR SELECT USING (true);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """)
    # Add placed_orders and splits to supabase_realtime publication
    op.execute("""
        DO $$ BEGIN
            ALTER PUBLICATION supabase_realtime ADD TABLE placed_orders;
        EXCEPTION WHEN others THEN NULL;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            ALTER PUBLICATION supabase_realtime ADD TABLE splits;
        EXCEPTION WHEN others THEN NULL;
        END $$;
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS placed_orders_select_all ON placed_orders;")
    op.execute("DROP POLICY IF EXISTS splits_select_all ON splits;")

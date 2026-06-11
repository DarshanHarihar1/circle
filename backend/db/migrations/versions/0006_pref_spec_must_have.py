"""add must_have column to pref_specs

parse_preferences now carries the craving card's "must have" dish verbatim into
the PrefSpec so the planner can prioritise that exact item. craving_cards already
had must_have (0001); pref_specs did not.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-09
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("pref_specs", sa.Column("must_have", sa.Text))


def downgrade() -> None:
    op.drop_column("pref_specs", "must_have")

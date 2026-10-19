"""insert order for approvals, plans and runs

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # created_at can tie (one clock tick, or a frozen test clock), "latest" has to be well defined
    for table in ("approvals", "plans", "runs"):
        op.add_column(table, sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False))
    op.drop_index("ix_approvals_plan", table_name="approvals")
    op.create_index("ix_approvals_plan", "approvals", ["tenant_id", "plan_id", "seq"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_approvals_plan", table_name="approvals")
    op.create_index("ix_approvals_plan", "approvals", ["tenant_id", "plan_id", "created_at"], unique=False)
    for table in ("approvals", "plans", "runs"):
        op.drop_column(table, "seq")

"""api request idempotency

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_requests",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("route", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_api_requests_tenant_id_tenants")),
        sa.PrimaryKeyConstraint("tenant_id", "key", name=op.f("pk_api_requests")),
    )


def downgrade() -> None:
    op.drop_table("api_requests")

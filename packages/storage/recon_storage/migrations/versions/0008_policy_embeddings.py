"""policy chunk embeddings

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # no ann index yet, per-tenant corpora are small and exact scans can't under-return
    op.add_column("policy_chunks", sa.Column("embedding", Vector(768), nullable=True))
    op.add_column("policy_chunks", sa.Column("embedding_model", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("policy_chunks", "embedding_model")
    op.drop_column("policy_chunks", "embedding")

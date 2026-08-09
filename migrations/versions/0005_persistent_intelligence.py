"""persistent intelligence and resumable missions

Revision ID: 0005_persistent_intelligence
Revises: 0004_application_catalog_constraints
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_persistent_intelligence"
down_revision = "0004_application_catalog_constraints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("namespace", sa.String(128), nullable=False, index=True),
        sa.Column("kind", sa.String(32), nullable=False, index=True),
        sa.Column("subject", sa.String(512), nullable=False, index=True),
        sa.Column("content", sa.JSON, nullable=False),
        sa.Column("importance", sa.Float, nullable=False, server_default="0.5"),
        sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("superseded_by", sa.String(36)),
        sa.UniqueConstraint("namespace", "kind", "subject", name="uq_memory_subject"),
    )
    op.create_table(
        "world_facts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("entity", sa.String(512), nullable=False, index=True),
        sa.Column("attribute", sa.String(256), nullable=False, index=True),
        sa.Column("value", sa.JSON, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True)),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("entity", "attribute", name="uq_world_fact"),
    )
    with op.batch_alter_table("missions") as batch:
        batch.add_column(sa.Column("goal", sa.Text, nullable=True))
        batch.add_column(sa.Column("priority", sa.Integer, nullable=False, server_default="50"))
        batch.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("resumable", sa.Boolean, nullable=False, server_default=sa.true()))
    with op.batch_alter_table("mission_tasks") as batch:
        batch.add_column(sa.Column("title", sa.String(512), nullable=True))
        batch.add_column(sa.Column("operation", sa.String(256), nullable=True))
        batch.add_column(sa.Column("arguments", sa.JSON, nullable=True))
        batch.add_column(sa.Column("dependencies", sa.JSON, nullable=True))
        batch.add_column(sa.Column("result", sa.JSON, nullable=True))
        batch.add_column(sa.Column("error", sa.Text, nullable=True))
        batch.add_column(sa.Column("attempts", sa.Integer, nullable=False, server_default="0"))
        batch.add_column(sa.Column("ordinal", sa.Integer, nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("mission_tasks") as batch:
        for name in ("ordinal", "attempts", "error", "result", "dependencies", "arguments", "operation", "title"):
            batch.drop_column(name)
    with op.batch_alter_table("missions") as batch:
        for name in ("resumable", "updated_at", "priority", "goal"):
            batch.drop_column(name)
    op.drop_table("world_facts")
    op.drop_table("memory_records")

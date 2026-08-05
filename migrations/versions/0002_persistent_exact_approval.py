"""expand persistent exact-approval binding

Revision ID: 0002_persistent_exact_approval
Revises: 0001_initial_runtime_schema
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_persistent_exact_approval"
down_revision = "0001_initial_runtime_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("approvals") as batch:
        batch.add_column(sa.Column("tool_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("tool_version", sa.String(32), nullable=True))
        batch.add_column(sa.Column("operation", sa.String(128), nullable=True))
        batch.add_column(sa.Column("arguments_json", sa.JSON, nullable=True))
        batch.add_column(sa.Column("target", sa.String(1024), nullable=True))
        batch.add_column(sa.Column("risk_level", sa.String(32), nullable=True))
        batch.add_column(sa.Column("permission_scopes", sa.JSON, nullable=True))
        batch.add_column(sa.Column("mission_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("session_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("approval_mode", sa.String(32), nullable=True))
        batch.add_column(sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("exact_operation_hash", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column("reusable", sa.Boolean, nullable=False, server_default=sa.false())
        )
    op.create_index("ix_approvals_active_actor", "approvals", ["actor", "expires_at"])


def downgrade() -> None:
    op.drop_index("ix_approvals_active_actor", table_name="approvals")
    with op.batch_alter_table("approvals") as batch:
        for name in (
            "reusable",
            "exact_operation_hash",
            "created_at",
            "approval_mode",
            "session_id",
            "mission_id",
            "permission_scopes",
            "risk_level",
            "target",
            "arguments_json",
            "operation",
            "tool_version",
            "tool_id",
        ):
            batch.drop_column(name)

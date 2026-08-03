"""initial runtime schema

Revision ID: 0001_initial_runtime_schema
Revises: None
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial_runtime_schema"
down_revision = None
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table("commands", sa.Column("id", sa.String(36), primary_key=True), sa.Column("utterance", sa.Text, nullable=False), sa.Column("trace_id", sa.String(36), nullable=False, index=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("events", sa.Column("id", sa.String(36), primary_key=True), sa.Column("event_type", sa.String(128), nullable=False), sa.Column("payload", sa.JSON, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("tool_specifications", sa.Column("tool_id", sa.String(128), primary_key=True), sa.Column("version", sa.String(32), primary_key=True), sa.Column("body", sa.JSON, nullable=False))
    op.create_table("tool_executions", sa.Column("id", sa.String(36), primary_key=True), sa.Column("command_id", sa.String(36), sa.ForeignKey("commands.id")), sa.Column("status", sa.String(32), nullable=False), sa.Column("evidence", sa.JSON, nullable=False))
    op.create_table("permission_grants", sa.Column("id", sa.Integer, primary_key=True), sa.Column("actor", sa.String(128), nullable=False), sa.Column("scope", sa.String(512), nullable=False), sa.UniqueConstraint("actor", "scope"))
    op.create_table("approvals", sa.Column("approval_id", sa.String(64), primary_key=True), sa.Column("binding_hash", sa.String(64), unique=True, nullable=False), sa.Column("actor", sa.String(128), nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("consumed_at", sa.DateTime(timezone=True)), sa.Column("revoked_at", sa.DateTime(timezone=True)))
    op.create_table("approval_consumptions", sa.Column("id", sa.Integer, primary_key=True), sa.Column("approval_id", sa.String(64), sa.ForeignKey("approvals.approval_id"), unique=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("approval_revocations", sa.Column("id", sa.Integer, primary_key=True), sa.Column("approval_id", sa.String(64), sa.ForeignKey("approvals.approval_id")), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("audit_records", sa.Column("id", sa.Integer, primary_key=True), sa.Column("command_id", sa.String(36), sa.ForeignKey("commands.id"), index=True), sa.Column("status", sa.String(32), nullable=False), sa.Column("message", sa.Text, nullable=False), sa.Column("evidence", sa.JSON, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("runtime_health", sa.Column("id", sa.Integer, primary_key=True), sa.Column("component", sa.String(128), unique=True), sa.Column("status", sa.String(32), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("missions", sa.Column("id", sa.String(36), primary_key=True), sa.Column("state", sa.String(32), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("mission_tasks", sa.Column("id", sa.String(36), primary_key=True), sa.Column("mission_id", sa.String(36), sa.ForeignKey("missions.id"), nullable=False), sa.Column("state", sa.String(32), nullable=False))
    op.create_table("mission_checkpoints", sa.Column("id", sa.String(36), primary_key=True), sa.Column("mission_id", sa.String(36), sa.ForeignKey("missions.id"), nullable=False), sa.Column("payload", sa.JSON, nullable=False))

def downgrade() -> None:
    for table in ("mission_checkpoints", "mission_tasks", "missions", "runtime_health", "audit_records", "approval_revocations", "approval_consumptions", "approvals", "permission_grants", "tool_executions", "tool_specifications", "events", "commands"): op.drop_table(table)

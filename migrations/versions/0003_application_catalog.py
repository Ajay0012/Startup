"""application catalog persistence

Revision ID: 0003_application_catalog
Revises: 0002_persistent_exact_approval
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_application_catalog"
down_revision = "0002_persistent_exact_approval"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table("applications", sa.Column("application_id", sa.String(64), primary_key=True), sa.Column("display_name", sa.String(512), nullable=False), sa.Column("normalized_name", sa.String(512), nullable=False), sa.Column("body", sa.JSON, nullable=False), sa.Column("stale", sa.Boolean, nullable=False), sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False), sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_applications_normalized_name", "applications", ["normalized_name"])
    op.create_table("application_aliases", sa.Column("id", sa.Integer, primary_key=True), sa.Column("application_id", sa.String(64), sa.ForeignKey("applications.application_id")), sa.Column("alias", sa.String(512), nullable=False))
    op.create_index("ix_application_aliases_application_id", "application_aliases", ["application_id"]); op.create_index("ix_application_aliases_alias", "application_aliases", ["alias"])
    op.create_table("application_evidence", sa.Column("id", sa.Integer, primary_key=True), sa.Column("application_id", sa.String(64), sa.ForeignKey("applications.application_id")), sa.Column("source", sa.String(128), nullable=False), sa.Column("evidence", sa.JSON, nullable=False))
    op.create_index("ix_application_evidence_application_id", "application_evidence", ["application_id"])
    op.create_table("application_discovery_runs", sa.Column("id", sa.Integer, primary_key=True), sa.Column("status", sa.String(32), nullable=False), sa.Column("started_at", sa.DateTime(timezone=True), nullable=False), sa.Column("completed_at", sa.DateTime(timezone=True)))

def downgrade() -> None:
    op.drop_table("application_discovery_runs"); op.drop_table("application_evidence"); op.drop_table("application_aliases"); op.drop_table("applications")

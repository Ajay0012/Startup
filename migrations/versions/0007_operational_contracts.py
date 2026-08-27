"""restore operational-contract migration lineage

Revision ID: 0007_operational_contracts
Revises: 0005_persistent_intelligence

This revision intentionally performs no schema changes. The current runtime schema
represented by migrations 0001 through 0005 already contains the fields required by
the production code. Some existing local PANGU databases were stamped with the
historical revision id ``0007_operational_contracts`` before that migration file was
lost from the branch. Restoring the revision id allows Alembic to recognize those
databases without deleting, restamping, or mutating user data.
"""

revision = "0007_operational_contracts"
down_revision = "0005_persistent_intelligence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Compatibility lineage marker; no schema change is required."""


def downgrade() -> None:
    """Compatibility lineage marker; no schema change is required."""

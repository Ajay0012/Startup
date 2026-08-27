"""reconcile persistent-intelligence schema for legacy stamped databases

Revision ID: 0008_reconcile_persistent_intelligence
Revises: 0007_operational_contracts

Some local PANGU databases were historically stamped at 0007 without having run the
0005 persistent-intelligence DDL. This migration is deliberately idempotent: it
inspects the live schema and creates only tables/columns that are missing. Fresh
installs that already received 0005 pass through unchanged.
"""

from alembic import op
import sqlalchemy as sa

revision = "0008_reconcile_persistent_intelligence"
down_revision = "0007_operational_contracts"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    tables = _table_names()

    if "memory_records" not in tables:
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

    if "world_facts" not in tables:
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

    if "missions" in tables:
        mission_columns = _column_names("missions")
        missing_mission_columns: list[sa.Column[object]] = []
        if "goal" not in mission_columns:
            missing_mission_columns.append(sa.Column("goal", sa.Text, nullable=True))
        if "priority" not in mission_columns:
            missing_mission_columns.append(
                sa.Column("priority", sa.Integer, nullable=False, server_default="50")
            )
        if "updated_at" not in mission_columns:
            missing_mission_columns.append(
                sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)
            )
        if "resumable" not in mission_columns:
            missing_mission_columns.append(
                sa.Column("resumable", sa.Boolean, nullable=False, server_default=sa.true())
            )
        if missing_mission_columns:
            with op.batch_alter_table("missions") as batch:
                for column in missing_mission_columns:
                    batch.add_column(column)

    if "mission_tasks" in tables:
        task_columns = _column_names("mission_tasks")
        missing_task_columns: list[sa.Column[object]] = []
        specs = (
            ("title", sa.Column("title", sa.String(512), nullable=True)),
            ("operation", sa.Column("operation", sa.String(256), nullable=True)),
            ("arguments", sa.Column("arguments", sa.JSON, nullable=True)),
            ("dependencies", sa.Column("dependencies", sa.JSON, nullable=True)),
            ("result", sa.Column("result", sa.JSON, nullable=True)),
            ("error", sa.Column("error", sa.Text, nullable=True)),
            (
                "attempts",
                sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
            ),
            (
                "ordinal",
                sa.Column("ordinal", sa.Integer, nullable=False, server_default="0"),
            ),
        )
        for name, column in specs:
            if name not in task_columns:
                missing_task_columns.append(column)
        if missing_task_columns:
            with op.batch_alter_table("mission_tasks") as batch:
                for column in missing_task_columns:
                    batch.add_column(column)


def downgrade() -> None:
    """No-op: this migration only repairs schema that logically belongs to revision 0005."""

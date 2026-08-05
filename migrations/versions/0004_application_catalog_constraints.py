"""application catalog constraints

Revision ID: 0004_application_catalog_constraints
Revises: 0003_application_catalog
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_application_catalog_constraints"
down_revision = "0003_application_catalog"
branch_labels = None
depends_on = None


def _reject_invalid_rows(table: str, value_column: str) -> None:
    connection = op.get_bind()
    invalid = connection.execute(
        sa.text(
            f"SELECT COUNT(*) FROM {table} AS child LEFT JOIN applications AS parent "
            "ON child.application_id = parent.application_id "
            "WHERE child.application_id IS NULL OR parent.application_id IS NULL"
        )
    ).scalar_one()
    duplicate = connection.execute(
        sa.text(
            f"SELECT COUNT(*) FROM (SELECT application_id, {value_column} FROM {table} "
            f"GROUP BY application_id, {value_column} HAVING COUNT(*) > 1)"
        )
    ).scalar_one()
    if invalid or duplicate:
        raise RuntimeError(f"application catalog migration refused invalid {table} rows")


def _rebuild_aliases() -> None:
    _reject_invalid_rows("application_aliases", "alias")
    op.create_table(
        "_application_aliases_0004",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("application_id", sa.String(64), nullable=False),
        sa.Column("alias", sa.String(512), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.application_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("application_id", "alias", name="uq_application_alias"),
    )
    op.execute("INSERT INTO _application_aliases_0004 (id, application_id, alias) SELECT id, application_id, alias FROM application_aliases")
    op.drop_table("application_aliases")
    op.rename_table("_application_aliases_0004", "application_aliases")
    op.create_index("ix_application_aliases_application_id", "application_aliases", ["application_id"])
    op.create_index("ix_application_aliases_alias", "application_aliases", ["alias"])


def _rebuild_evidence() -> None:
    _reject_invalid_rows("application_evidence", "source")
    op.create_table(
        "_application_evidence_0004",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("application_id", sa.String(64), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("evidence", sa.JSON, nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.application_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("application_id", "source", name="uq_application_evidence_source"),
    )
    op.execute("INSERT INTO _application_evidence_0004 (id, application_id, source, evidence) SELECT id, application_id, source, evidence FROM application_evidence")
    op.drop_table("application_evidence")
    op.rename_table("_application_evidence_0004", "application_evidence")
    op.create_index("ix_application_evidence_application_id", "application_evidence", ["application_id"])


def upgrade() -> None:
    _rebuild_aliases()
    _rebuild_evidence()


def _downgrade_table(name: str, value_column: str, value_type: object) -> None:
    temporary = f"_{name}_0003"
    columns = [sa.Column("id", sa.Integer, primary_key=True), sa.Column("application_id", sa.String(64)), sa.Column(value_column, value_type, nullable=False)]
    if name == "application_evidence": columns.append(sa.Column("evidence", sa.JSON, nullable=False))
    op.create_table(temporary, *columns, sa.ForeignKeyConstraint(["application_id"], ["applications.application_id"]))
    selected = "id, application_id, source, evidence" if name == "application_evidence" else "id, application_id, alias"
    op.execute(f"INSERT INTO {temporary} ({selected}) SELECT {selected} FROM {name}")
    op.drop_table(name)
    op.rename_table(temporary, name)
    op.create_index(f"ix_{name}_application_id", name, ["application_id"])
    if name == "application_aliases": op.create_index("ix_application_aliases_alias", name, ["alias"])


def downgrade() -> None:
    _downgrade_table("application_evidence", "source", sa.String(128))
    _downgrade_table("application_aliases", "alias", sa.String(512))

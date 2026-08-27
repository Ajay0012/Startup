from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from .database import DatabaseService, WorldFactRow


@dataclass(frozen=True)
class WorldFact:
    fact_id: str
    entity: str
    attribute: str
    value: object
    confidence: float
    source: str
    observed_at: datetime
    valid_until: datetime | None = None


@dataclass(frozen=True)
class WorldDelta:
    entity: str
    attribute: str
    previous: object | None
    current: object
    changed: bool
    confidence: float
    source: str
    observed_at: datetime


class PersonalWorldModel:
    """Persistent, source-aware state model for the owner's current environment."""

    def __init__(self, database: DatabaseService) -> None:
        self.database = database

    @staticmethod
    def _utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @staticmethod
    def _fact(row: WorldFactRow) -> WorldFact:
        return WorldFact(
            row.id,
            row.entity,
            row.attribute,
            row.value,
            float(row.confidence),
            row.source,
            PersonalWorldModel._utc(row.observed_at) or datetime.now(UTC),
            PersonalWorldModel._utc(row.valid_until),
        )

    def observe(
        self,
        entity: str,
        attribute: str,
        value: object,
        *,
        confidence: float = 1.0,
        source: str = "runtime",
        valid_until: datetime | None = None,
    ) -> WorldDelta:
        if not entity.strip() or not attribute.strip():
            raise ValueError("entity and attribute are required")
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        now = datetime.now(UTC)
        with self.database.transaction() as session:
            row = session.scalar(
                select(WorldFactRow).where(
                    WorldFactRow.entity == entity.strip(),
                    WorldFactRow.attribute == attribute.strip(),
                )
            )
            previous = None if row is None else row.value
            changed = row is None or previous != value
            if row is None:
                row = WorldFactRow(
                    id=str(uuid4()),
                    entity=entity.strip(),
                    attribute=attribute.strip(),
                    value=value,
                    confidence=confidence,
                    source=source,
                    observed_at=now,
                    valid_until=valid_until,
                    superseded_at=None,
                )
                session.add(row)
            else:
                row.value = value
                row.confidence = confidence
                row.source = source
                row.observed_at = now
                row.valid_until = valid_until
                row.superseded_at = None
            return WorldDelta(
                entity.strip(),
                attribute.strip(),
                previous,
                value,
                changed,
                confidence,
                source,
                now,
            )

    def get(self, entity: str, attribute: str) -> WorldFact | None:
        now = datetime.now(UTC)
        with self.database.transaction() as session:
            row = session.scalar(
                select(WorldFactRow).where(
                    WorldFactRow.entity == entity,
                    WorldFactRow.attribute == attribute,
                )
            )
            if row is None or row.superseded_at is not None:
                return None
            valid_until = self._utc(row.valid_until)
            if valid_until is not None and valid_until <= now:
                return None
            return self._fact(row)

    def snapshot(self, prefix: str | None = None, limit: int = 100) -> tuple[WorldFact, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        now = datetime.now(UTC)
        with self.database.transaction() as session:
            statement = select(WorldFactRow).where(WorldFactRow.superseded_at.is_(None))
            if prefix:
                statement = statement.where(WorldFactRow.entity.like(f"{prefix}%"))
            rows = list(session.scalars(statement.limit(limit)).all())
        return tuple(
            self._fact(row)
            for row in rows
            if self._utc(row.valid_until) is None or (self._utc(row.valid_until) or now) > now
        )

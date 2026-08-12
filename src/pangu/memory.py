from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import select

from .database import DatabaseService, MemoryRow


class MemoryKind(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    namespace: str
    kind: MemoryKind
    subject: str
    content: dict[str, object]
    importance: float
    confidence: float
    source: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None


class PersistentMemoryRuntime:
    """Layered local memory using the single PANGU database owner.

    Memory writes are explicit and typed. Recall is deterministic and bounded; an LLM
    may use recalled records as context but does not own persistence or retention.
    """

    def __init__(self, database: DatabaseService, default_namespace: str = "owner") -> None:
        self.database = database
        self.default_namespace = default_namespace

    @staticmethod
    def _utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @staticmethod
    def _record(row: MemoryRow) -> MemoryRecord:
        return MemoryRecord(
            row.id,
            row.namespace,
            MemoryKind(row.kind),
            row.subject,
            dict(row.content),
            float(row.importance),
            float(row.confidence),
            row.source,
            PersistentMemoryRuntime._utc(row.created_at) or datetime.now(UTC),
            PersistentMemoryRuntime._utc(row.updated_at) or datetime.now(UTC),
            PersistentMemoryRuntime._utc(row.expires_at),
        )

    def remember(
        self,
        kind: MemoryKind,
        subject: str,
        content: dict[str, object],
        *,
        namespace: str | None = None,
        importance: float = 0.5,
        confidence: float = 1.0,
        source: str = "user",
        ttl_seconds: int | None = None,
    ) -> MemoryRecord:
        if not subject.strip():
            raise ValueError("memory subject is required")
        if not 0 <= importance <= 1 or not 0 <= confidence <= 1:
            raise ValueError("importance and confidence must be between 0 and 1")
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        ns = (namespace or self.default_namespace).strip() or self.default_namespace
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=ttl_seconds) if ttl_seconds else None
        with self.database.transaction() as session:
            row = session.scalar(
                select(MemoryRow).where(
                    MemoryRow.namespace == ns,
                    MemoryRow.kind == kind.value,
                    MemoryRow.subject == subject.strip(),
                )
            )
            if row is None:
                row = MemoryRow(
                    id=str(uuid4()),
                    namespace=ns,
                    kind=kind.value,
                    subject=subject.strip(),
                    content=dict(content),
                    importance=importance,
                    confidence=confidence,
                    source=source,
                    created_at=now,
                    updated_at=now,
                    expires_at=expires,
                )
                session.add(row)
            else:
                row.content = dict(content)
                row.importance = importance
                row.confidence = confidence
                row.source = source
                row.updated_at = now
                row.expires_at = expires
                row.superseded_by = None
            session.flush()
            return self._record(row)

    def recall(
        self,
        query: str,
        *,
        namespace: str | None = None,
        kinds: tuple[MemoryKind, ...] | None = None,
        limit: int = 8,
    ) -> tuple[MemoryRecord, ...]:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        ns = namespace or self.default_namespace
        now = datetime.now(UTC)
        terms = {term for term in query.casefold().split() if len(term) > 1}
        with self.database.transaction() as session:
            statement = select(MemoryRow).where(
                MemoryRow.namespace == ns,
                MemoryRow.superseded_by.is_(None),
            )
            if kinds:
                statement = statement.where(MemoryRow.kind.in_([item.value for item in kinds]))
            rows = list(session.scalars(statement).all())
        live = [
            self._record(row)
            for row in rows
            if self._utc(row.expires_at) is None or (self._utc(row.expires_at) or now) > now
        ]

        def score(item: MemoryRecord) -> tuple[float, float]:
            haystack = f"{item.subject} {item.content}".casefold()
            lexical = sum(1.0 for term in terms if term in haystack)
            return lexical + item.importance + item.confidence * 0.25, item.updated_at.timestamp()

        ranked = sorted(live, key=score, reverse=True)
        if terms:
            ranked = [
                item
                for item in ranked
                if any(term in f"{item.subject} {item.content}".casefold() for term in terms)
            ]
        return tuple(ranked[:limit])

    def forget(self, memory_id: str) -> bool:
        with self.database.transaction() as session:
            row = session.get(MemoryRow, memory_id)
            if row is None:
                return False
            session.delete(row)
            return True

    def prune_expired(self) -> int:
        now = datetime.now(UTC)
        with self.database.transaction() as session:
            rows = list(
                session.scalars(select(MemoryRow).where(MemoryRow.expires_at.is_not(None))).all()
            )
            expired = [row for row in rows if (self._utc(row.expires_at) or now) <= now]
            for row in expired:
                session.delete(row)
            return len(expired)

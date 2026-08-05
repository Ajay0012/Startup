"""Compatibility facade for the single lifecycle-owned SQLAlchemy database."""

from __future__ import annotations

from pathlib import Path

from .database import DatabaseService


class Database(DatabaseService):
    """Legacy import name; no SQLite connection or schema is owned here."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)

    def close(self) -> None:
        self.stop()

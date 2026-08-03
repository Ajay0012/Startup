from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .contracts import CommandEnvelope, ToolResult


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: sqlite3.Connection | None = None

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            "CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, utterance TEXT NOT NULL, trace_id TEXT NOT NULL, created_at TEXT NOT NULL); CREATE TABLE IF NOT EXISTS audit_entries(id INTEGER PRIMARY KEY, command_id TEXT, status TEXT, message TEXT, evidence TEXT, created_at TEXT NOT NULL);"
        )

    def record(self, command: CommandEnvelope, result: ToolResult) -> None:
        assert self.connection
        self.connection.execute(
            "INSERT INTO commands VALUES(?,?,?,?)",
            (
                command.command_id,
                command.original_utterance,
                command.trace_id,
                command.timestamp.isoformat(),
            ),
        )
        self.connection.execute(
            "INSERT INTO audit_entries(command_id,status,message,evidence,created_at) VALUES(?,?,?,?,?)",
            (
                command.command_id,
                result.status,
                result.message,
                json.dumps(result.evidence),
                command.timestamp.isoformat(),
            ),
        )
        self.connection.commit()

    def close(self) -> None:
        if self.connection:
            self.connection.close()
            self.connection = None

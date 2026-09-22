import json
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Optional

from .models import Event, ExecutionRun, ModelProfile


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._migrate()

    def _migrate(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS models (
                    id TEXT PRIMARY KEY, body TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, body TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    kind TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
                """
            )

    def save_models(self, models: Iterable[ModelProfile]) -> None:
        with self.lock, self.connection:
            for model in models:
                self.connection.execute(
                    "INSERT OR REPLACE INTO models(id, body, updated_at) VALUES (?, ?, ?)",
                    (model.id, model.model_dump_json(), model.effective_at.isoformat()),
                )

    def delete_models_except(self, keep_ids: Iterable[str]) -> None:
        keep = list(keep_ids)
        placeholders = ",".join("?" for _ in keep)
        with self.lock, self.connection:
            self.connection.execute(f"DELETE FROM models WHERE id NOT IN ({placeholders})", keep)

    def list_models(self) -> list[ModelProfile]:
        with self.lock:
            rows = self.connection.execute("SELECT body FROM models ORDER BY id").fetchall()
        return [ModelProfile.model_validate_json(row["body"]) for row in rows]

    def save_run(self, run: ExecutionRun) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """INSERT OR REPLACE INTO runs(id, status, body, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)""",
                (
                    run.id,
                    run.status,
                    run.model_dump_json(),
                    run.created_at.isoformat(),
                    run.updated_at.isoformat(),
                ),
            )

    def get_run(self, run_id: str) -> Optional[ExecutionRun]:
        with self.lock:
            row = self.connection.execute("SELECT body FROM runs WHERE id = ?", (run_id,)).fetchone()
        return ExecutionRun.model_validate_json(row["body"]) if row else None

    def list_runs(self) -> list[ExecutionRun]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT body FROM runs ORDER BY created_at DESC"
            ).fetchall()
        return [ExecutionRun.model_validate_json(row["body"]) for row in rows]

    def add_event(self, event: Event) -> Event:
        with self.lock, self.connection:
            cursor = self.connection.execute(
                "INSERT INTO events(run_id, kind, body, created_at) VALUES (?, ?, ?, ?)",
                (event.run_id, event.kind, json.dumps(event.data), event.created_at.isoformat()),
            )
            event.id = cursor.lastrowid
        return event

    def events(self, run_id: str, after: int = 0) -> list[Event]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM events WHERE run_id = ? AND id > ? ORDER BY id",
                (run_id, after),
            ).fetchall()
        return [
            Event(
                id=row["id"], run_id=row["run_id"], kind=row["kind"],
                data=json.loads(row["body"]), created_at=row["created_at"]
            )
            for row in rows
        ]

import json
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Optional

from .models import Event, ExecutionRun, ModelProfile, RunStatus


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
                CREATE TABLE IF NOT EXISTS catalog_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1), body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS benchmark_cache (
                    source TEXT PRIMARY KEY, body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recovery_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1), body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shared_repairs (
                    id TEXT PRIMARY KEY, body TEXT NOT NULL, created_at TEXT NOT NULL
                );
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

    def save_catalog_state(self, state: dict) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO catalog_state(id, body) VALUES (1, ?)",
                (json.dumps(state),),
            )

    def load_catalog_state(self) -> dict:
        with self.lock:
            row = self.connection.execute("SELECT body FROM catalog_state WHERE id = 1").fetchone()
        return json.loads(row["body"]) if row else {}

    def save_benchmark_cache(self, source: str, body: dict) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO benchmark_cache(source, body) VALUES (?, ?)",
                (source, json.dumps(body, ensure_ascii=False)),
            )

    def load_benchmark_cache(self) -> dict:
        with self.lock:
            rows = self.connection.execute("SELECT source, body FROM benchmark_cache").fetchall()
        return {row["source"]: json.loads(row["body"]) for row in rows}

    def list_models(self) -> list[ModelProfile]:
        with self.lock:
            rows = self.connection.execute("SELECT body FROM models ORDER BY id").fetchall()
        return [ModelProfile.model_validate_json(row["body"]) for row in rows]

    def load_recovery_state(self) -> dict:
        with self.lock:
            row = self.connection.execute("SELECT body FROM recovery_state WHERE id = 1").fetchone()
        return json.loads(row["body"]) if row else {}

    def save_recovery_state(self, state: dict) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO recovery_state(id, body) VALUES (1, ?)",
                (json.dumps(state, ensure_ascii=False),),
            )

    def save_shared_repair(self, repair: dict) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO shared_repairs(id, body, created_at) VALUES (?, ?, ?)",
                (repair["id"], json.dumps(repair, ensure_ascii=False), repair["created_at"]),
            )

    def shared_repairs(self, limit: int = 10) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT body FROM shared_repairs ORDER BY created_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [json.loads(row["body"]) for row in rows]

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

    def delete_run(self, run_id: str) -> bool:
        """Delete a finished run and its events atomically, leaving workspace files alone."""
        with self.lock, self.connection:
            deleted = self.connection.execute(
                "DELETE FROM runs WHERE id = ? AND status IN (?, ?, ?, ?)",
                (run_id, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INCOMPLETE),
            )
            if not deleted.rowcount:
                exists = self.connection.execute(
                    "SELECT 1 FROM runs WHERE id = ?", (run_id,),
                ).fetchone()
                if exists:
                    raise ValueError("只能删除已完成、失败或已取消的任务记录。")
                return False
            self.connection.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
        return True

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

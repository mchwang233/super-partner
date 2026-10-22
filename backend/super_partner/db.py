import json
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Optional

from .models import (
    EngineerJob,
    Event,
    ExecutionRun,
    Improvement,
    LearningReview,
    ModelProfile,
    RunStatus,
)


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
                CREATE TABLE IF NOT EXISTS engineer_jobs (
                    id TEXT PRIMARY KEY, body TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS retrospectives (
                    run_id TEXT PRIMARY KEY, body TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS learning_reviews (
                    run_id TEXT PRIMARY KEY, body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS improvements (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS improvement_effects (
                    improvement_id TEXT NOT NULL, run_id TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(improvement_id, run_id)
                );
                CREATE TABLE IF NOT EXISTS learning_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1), body TEXT NOT NULL
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

    def save_retrospective(self, run_id: str, body: dict) -> None:
        """A finished task's look back (retrospect.py), replacing an earlier one."""
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO retrospectives(run_id, body, created_at) VALUES (?, ?, ?)",
                (run_id, json.dumps(body, ensure_ascii=False), body.get("started_at") or ""),
            )

    def retrospective(self, run_id: str) -> Optional[dict]:
        with self.lock:
            row = self.connection.execute("SELECT body FROM retrospectives WHERE run_id = ?", (run_id,)).fetchone()
        return json.loads(row["body"]) if row else None

    def retrospectives(self) -> list[dict]:
        with self.lock:
            rows = self.connection.execute("SELECT body FROM retrospectives ORDER BY created_at").fetchall()
        return [json.loads(row["body"]) for row in rows]

    def save_engineer_job(self, job: EngineerJob) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO engineer_jobs(id, body, created_at) VALUES (?, ?, ?)",
                (job.id, job.model_dump_json(), job.created_at.isoformat()),
            )

    def learning_settings(self) -> dict:
        with self.lock:
            row = self.connection.execute("SELECT body FROM learning_settings WHERE id = 1").fetchone()
        return json.loads(row["body"]) if row else {}

    def save_learning_settings(self, value: dict) -> None:
        with self.lock, self.connection:
            self.connection.execute("INSERT OR REPLACE INTO learning_settings VALUES (1, ?)",
                                    (json.dumps(value),))

    def save_learning_review(self, review: LearningReview) -> None:
        with self.lock, self.connection:
            self.connection.execute("INSERT OR REPLACE INTO learning_reviews VALUES (?, ?)",
                                    (review.run_id, review.model_dump_json()))

    def learning_review(self, run_id: str) -> Optional[LearningReview]:
        with self.lock:
            row = self.connection.execute("SELECT body FROM learning_reviews WHERE run_id = ?", (run_id,)).fetchone()
        return LearningReview.model_validate_json(row["body"]) if row else None

    def learning_reviews(self) -> list[LearningReview]:
        with self.lock:
            rows = self.connection.execute("SELECT body FROM learning_reviews").fetchall()
        return [LearningReview.model_validate_json(row["body"]) for row in rows]

    def save_improvement(self, item: Improvement) -> None:
        with self.lock, self.connection:
            self.connection.execute("INSERT OR REPLACE INTO improvements VALUES (?, ?, ?)",
                                    (item.id, item.run_id, item.model_dump_json()))

    def improvement(self, item_id: str) -> Optional[Improvement]:
        with self.lock:
            row = self.connection.execute("SELECT body FROM improvements WHERE id = ?", (item_id,)).fetchone()
        return Improvement.model_validate_json(row["body"]) if row else None

    def improvements(self, run_id: Optional[str] = None) -> list[Improvement]:
        with self.lock:
            rows = self.connection.execute("SELECT body FROM improvements" + (" WHERE run_id = ?" if run_id else ""),
                                           (run_id,) if run_id else ()).fetchall()
        return [Improvement.model_validate_json(row["body"]) for row in rows]

    def save_improvement_effect(self, item_id: str, run_id: str, body: dict) -> None:
        with self.lock, self.connection:
            self.connection.execute("INSERT OR REPLACE INTO improvement_effects VALUES (?, ?, ?)",
                                    (item_id, run_id, json.dumps(body, ensure_ascii=False)))

    def improvement_effects(self, item_id: str) -> list[dict]:
        with self.lock:
            rows = self.connection.execute("SELECT body FROM improvement_effects WHERE improvement_id = ?",
                                           (item_id,)).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def get_engineer_job(self, job_id: str) -> Optional[EngineerJob]:
        with self.lock:
            row = self.connection.execute("SELECT body FROM engineer_jobs WHERE id = ?", (job_id,)).fetchone()
        return EngineerJob.model_validate_json(row["body"]) if row else None

    def engineer_jobs(self, limit: int = 50) -> list[EngineerJob]:
        """Newest first."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT body FROM engineer_jobs ORDER BY created_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [EngineerJob.model_validate_json(row["body"]) for row in rows]

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
            self.connection.execute("DELETE FROM retrospectives WHERE run_id = ?", (run_id,))
            self.connection.execute("DELETE FROM learning_reviews WHERE run_id = ?", (run_id,))
            self.connection.execute("DELETE FROM improvement_effects WHERE run_id = ? OR improvement_id IN "
                                    "(SELECT id FROM improvements WHERE run_id = ?)", (run_id, run_id))
            self.connection.execute("DELETE FROM improvements WHERE run_id = ?", (run_id,))
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

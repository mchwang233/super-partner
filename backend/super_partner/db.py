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
                CREATE TABLE IF NOT EXISTS conversations (
                    run_id TEXT NOT NULL, node_id TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(run_id, node_id)
                );
                CREATE TABLE IF NOT EXISTS conversation_archives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    node_id TEXT NOT NULL, body TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_archives_scope ON conversation_archives(run_id, node_id, id);
                CREATE TABLE IF NOT EXISTS model_calls (
                    id TEXT PRIMARY KEY, run_id TEXT, node_id TEXT, started_at TEXT NOT NULL, body TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_calls_run ON model_calls(run_id, started_at);
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
        """Delete a finished run and its events atomically, leaving workspace files alone. What was
        learned from it stays: its retrospective, learning review, improvements and their effects
        are what later tasks are planned with."""
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
            for table in ("conversations", "conversation_archives", "model_calls"):
                self.connection.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
        return True

    def save_conversation(self, run_id: str, node_id: str, body: dict) -> None:
        with self.lock, self.connection:
            # A late checkpoint must not resurrect a deleted task's private history.
            self.connection.execute(
                "INSERT OR REPLACE INTO conversations(run_id, node_id, body) "
                "SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM runs WHERE id = ?)",
                (run_id, node_id, json.dumps(body, ensure_ascii=False), run_id),
            )

    def conversation(self, run_id: str, node_id: str) -> Optional[dict]:
        with self.lock:
            row = self.connection.execute("SELECT body FROM conversations WHERE run_id = ? AND node_id = ?",
                                          (run_id, node_id)).fetchone()
        return json.loads(row["body"]) if row else None

    def archive_conversation(self, run_id: str, node_id: str, body: dict) -> int:
        with self.lock, self.connection:
            cursor = self.connection.execute(
                "INSERT INTO conversation_archives(run_id, node_id, body) "
                "SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM runs WHERE id = ?)",
                (run_id, node_id, json.dumps(body, ensure_ascii=False), run_id),
            )
            return cursor.lastrowid if cursor.rowcount else 0

    def conversation_archive(self, run_id: str, node_id: str, archive_id: int) -> Optional[dict]:
        with self.lock:
            row = self.connection.execute(
                "SELECT body FROM conversation_archives WHERE run_id = ? AND node_id = ? AND id = ?",
                (run_id, node_id, archive_id),
            ).fetchone()
        return json.loads(row["body"]) if row else None

    def save_model_call(self, body: dict) -> None:
        with self.lock, self.connection:
            if body.get("run_id") and not self.connection.execute("SELECT 1 FROM runs WHERE id = ?",
                                                                 (body["run_id"],)).fetchone():
                return
            self.connection.execute("INSERT OR REPLACE INTO model_calls VALUES (?, ?, ?, ?, ?)",
                                    (body["id"], body.get("run_id"), body.get("node_id"), body["started_at"],
                                     json.dumps(body, ensure_ascii=False)))

    def model_calls(self, run_id: Optional[str] = None, limit: int = 1000) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT body FROM model_calls " + ("WHERE run_id = ? " if run_id else "") +
                "ORDER BY started_at DESC LIMIT ?", ((run_id,) if run_id else ()) + (max(1, min(limit, 10000)),),
            ).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def interrupt_model_calls(self):
        """Called once at service startup, before admitting new work."""
        with self.lock, self.connection:
            rows = self.connection.execute("SELECT id, body FROM model_calls").fetchall()
            for row in rows:
                body = json.loads(row["body"])
                if body.get("status") == "running":
                    body.update(status="interrupted", usage_known=False)
                    body.setdefault("warnings", []).append("服务中断，可能存在未收到的用量；请勿按零消耗处理。")
                    self.connection.execute("UPDATE model_calls SET body = ? WHERE id = ?",
                                            (json.dumps(body, ensure_ascii=False), row["id"]))

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

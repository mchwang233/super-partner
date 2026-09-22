from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .config import settings
from .db import Database
from .models import Event, ExecutionRun, NodeStatus, RunStatus
from .providers import ProviderRegistry


class Executor:
    def __init__(self, db: Database, providers: ProviderRegistry, model_lookup: dict):
        self.db = db
        self.providers = providers
        self.model_lookup = model_lookup
        self.tasks: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    def start(self, run_id: str) -> None:
        task = self.tasks.get(run_id)
        if not task or task.done():
            self.tasks[run_id] = asyncio.create_task(self.execute(run_id))

    def emit(self, run_id: str, kind: str, **data) -> None:
        self.db.add_event(Event(run_id=run_id, kind=kind, data=data))

    async def execute(self, run_id: str) -> None:
        run = self.db.get_run(run_id)
        if not run or run.status not in {RunStatus.QUEUED, RunStatus.PAUSED}:
            return
        run.status = RunStatus.RUNNING
        self._save(run)
        self.emit(run.id, "run.started")
        try:
            while True:
                run = self.db.get_run(run_id)
                if not run or run.status in {RunStatus.CANCELLED, RunStatus.PAUSED}:
                    return
                pending = [node for node in run.plan.nodes if node.status == NodeStatus.PENDING]
                if not pending:
                    break
                completed = {node.id for node in run.plan.nodes if node.status == NodeStatus.SUCCEEDED}
                ready = [node for node in pending if set(node.dependencies) <= completed]
                if not ready:
                    raise RuntimeError("Task graph is blocked by failed or cyclic dependencies")
                await asyncio.gather(*(self._execute_node(run_id, node.id) for node in ready[:3]))
            run = self.db.get_run(run_id)
            if not run:
                return
            failed = [node for node in run.plan.nodes if node.status == NodeStatus.FAILED]
            if failed:
                raise RuntimeError(f"{len(failed)} task node(s) failed")
            run.status = RunStatus.SUCCEEDED
            run.final_result = run.plan.nodes[-1].result
            self._save(run)
            self.emit(run.id, "run.succeeded", actual_cost_usd=run.actual_cost_usd)
        except Exception as exc:
            run = self.db.get_run(run_id)
            if run:
                run.status = RunStatus.FAILED
                run.error = str(exc)
                self._save(run)
                self.emit(run.id, "run.failed", error=str(exc))

    async def _execute_node(self, run_id: str, node_id: str) -> None:
        lock = self.locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            run = self.db.get_run(run_id)
            if not run:
                return
            node = next(node for node in run.plan.nodes if node.id == node_id)
            node.status = NodeStatus.RUNNING
            self._save(run)
        self.emit(run_id, "node.started", node_id=node.id, model_id=node.model_id)
        context = [
            f"{item.title}: {item.result}" for item in run.plan.nodes
            if item.id in node.dependencies and item.result
        ]
        prompt = f"Task: {node.instructions}\n\nDependency results:\n" + "\n".join(context)
        try:
            workspace = settings.workspace_root / run_id
            workspace.mkdir(parents=True, exist_ok=True)
            result = await self.providers.complete(node.model_id or "mock:local", prompt, workspace)
            async with lock:
                latest = self.db.get_run(run_id)
                if not latest:
                    return
                node = next(item for item in latest.plan.nodes if item.id == node_id)
                charged_model_id = result.used_model_id or node.model_id
                model = self.model_lookup[charged_model_id]
                actual = (
                    result.input_tokens * model.input_per_million
                    + result.output_tokens * model.output_per_million
                ) / 1_000_000
                node.result = result.text
                node.status = NodeStatus.SUCCEEDED
                latest.actual_cost_usd = round(latest.actual_cost_usd + actual, 6)
                if latest.actual_cost_usd > latest.budget_usd:
                    latest.status = RunStatus.PAUSED
                    self.emit(run_id, "approval.required", reason="budget_exceeded", actual_cost_usd=latest.actual_cost_usd)
                self._save(latest)
            self.emit(
                run_id, "node.succeeded", node_id=node.id, cost_usd=actual,
                selected_model_id=node.model_id, used_model_id=result.used_model_id,
            )
        except Exception as exc:
            async with lock:
                latest = self.db.get_run(run_id)
                if latest:
                    node = next(item for item in latest.plan.nodes if item.id == node_id)
                    node.status = NodeStatus.FAILED
                    node.result = str(exc)
                    self._save(latest)
                    self.emit(run_id, "node.failed", node_id=node.id, error=str(exc))

    def _save(self, run: ExecutionRun) -> None:
        run.updated_at = datetime.now(timezone.utc)
        self.db.save_run(run)

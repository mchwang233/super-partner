from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from .cli_channels import CLI_SPECS
from .config import settings
from .db import Database
from .models import Event, ExecutionRun, NodeStatus, RunStatus
from .projects import git_snapshot
from .providers import ProviderRegistry
from .routing import node_cost


class Executor:
    def __init__(self, db: Database, providers: ProviderRegistry, model_lookup: dict):
        self.db = db
        self.providers = providers
        self.model_lookup = model_lookup
        self.tasks: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.project_locks: dict[str, asyncio.Lock] = {}

    def start(self, run_id: str) -> None:
        task = self.tasks.get(run_id)
        if not task or task.done():
            self.tasks[run_id] = asyncio.create_task(self.execute(run_id))

    def cancel(self, run_id: str) -> None:
        task = self.tasks.get(run_id)
        if task and not task.done():
            task.cancel()

    def emit(self, run_id: str, kind: str, **data) -> None:
        self.db.add_event(Event(run_id=run_id, kind=kind, data=data))

    async def execute(self, run_id: str) -> None:
        run = self.db.get_run(run_id)
        if not run or run.status not in {RunStatus.QUEUED, RunStatus.PAUSED}:
            return
        run.status = RunStatus.RUNNING
        # Nodes left RUNNING by an interrupted process never finished; run them again.
        for node in run.plan.nodes:
            if node.status == NodeStatus.RUNNING:
                node.status = NodeStatus.PENDING
        self._save(run)
        self.emit(run.id, "run.started")
        if run.project_dir:
            self.emit(
                run.id, "workspace.snapshot", project_dir=run.project_dir,
                **await asyncio.to_thread(git_snapshot, Path(run.project_dir)),
            )
        running: dict[asyncio.Task, str] = {}
        try:
            while True:
                run = self.db.get_run(run_id)
                if not run:
                    return
                if run.status in {RunStatus.CANCELLED, RunStatus.PAUSED}:
                    # Let in-flight nodes finish so their results and costs are recorded.
                    if not running:
                        return
                else:
                    dispatched_for_budget = self._dispatch(run, running)
                    if not running:
                        if dispatched_for_budget:
                            self._pause_for_budget(run)
                            return
                        break
                done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    running.pop(task)
            run = self.db.get_run(run_id)
            if not run:
                return
            failed = [node for node in run.plan.nodes if node.status == NodeStatus.FAILED]
            if failed:
                raise RuntimeError(f"{len(failed)} task node(s) failed")
            if any(node.status != NodeStatus.SUCCEEDED for node in run.plan.nodes):
                raise RuntimeError("Task graph is blocked by failed or cyclic dependencies")
            run.status = RunStatus.SUCCEEDED
            run.final_result = self._final_result(run)
            self._save(run)
            self.emit(run.id, "run.succeeded", actual_cost_usd=run.actual_cost_usd)
        except asyncio.CancelledError:
            await self._stop_nodes(running)
            run = self.db.get_run(run_id)
            # Only a user cancel marks nodes cancelled; on shutdown they stay RUNNING to resume.
            if run and run.status == RunStatus.CANCELLED:
                for node in run.plan.nodes:
                    if node.status == NodeStatus.RUNNING:
                        node.status = NodeStatus.CANCELLED
                self._save(run)
            raise
        except Exception as exc:
            await self._stop_nodes(running)
            run = self.db.get_run(run_id)
            if run:
                run.status = RunStatus.FAILED
                run.error = str(exc)
                self._save(run)
                self.emit(run.id, "run.failed", error=str(exc))

    def _dispatch(self, run: ExecutionRun, running: dict[asyncio.Task, str]) -> bool:
        """Start ready nodes up to the parallel limit; return True if budget held any back."""
        in_flight = set(running.values())
        completed = {node.id for node in run.plan.nodes if node.status == NodeStatus.SUCCEEDED}
        ready = [
            node for node in run.plan.nodes
            if node.status == NodeStatus.PENDING and node.id not in in_flight
            and set(node.dependencies) <= completed
        ]
        reserved = run.actual_cost_usd + sum(
            node.estimated_cost_usd for node in run.plan.nodes if node.id in in_flight
        )
        held_back = False
        for node in ready:
            if len(running) >= settings.max_parallel_nodes:
                break
            if node.estimated_cost_usd and reserved + node.estimated_cost_usd > run.budget_usd:
                held_back = True
                continue
            reserved += node.estimated_cost_usd
            running[asyncio.create_task(self._execute_node(run.id, node.id))] = node.id
        return held_back

    def _pause_for_budget(self, run: ExecutionRun) -> None:
        run.status = RunStatus.PAUSED
        self._save(run)
        self.emit(
            run.id, "approval.required", reason="budget_insufficient",
            actual_cost_usd=run.actual_cost_usd, budget_usd=run.budget_usd,
        )

    @staticmethod
    async def _stop_nodes(running: dict[asyncio.Task, str]) -> None:
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)

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
        in_project = bool(run.project_dir and node.needs_workspace)
        if in_project:
            prompt = (
                f"You are working in the user's existing project at {run.project_dir}. "
                "Edit files in place, keep changes focused on the task, and do not commit.\n\n"
                + prompt
            )
        try:
            if in_project:
                workspace = Path(run.project_dir)
                # CLI agents editing the same checkout concurrently would clobber each other.
                async with self.project_locks.setdefault(run.project_dir, asyncio.Lock()):
                    result = await self.providers.complete(
                        node.model_id or "mock:local", prompt, workspace, allow_api_fallback=False,
                    )
            else:
                workspace = settings.workspace_root / run_id
                workspace.mkdir(parents=True, exist_ok=True)
                result = await self.providers.complete(
                    node.model_id or "mock:local", prompt, workspace,
                    allow_api_fallback=self._fallback_allowed(run, node),
                )
            async with lock:
                latest = self.db.get_run(run_id)
                if not latest:
                    return
                node = next(item for item in latest.plan.nodes if item.id == node_id)
                actual = self._actual_cost(result, node)
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

    def _actual_cost(self, result, node) -> float:
        """Charge by token usage; if the model or its price has since left the catalog, the
        work is still done, so charge the routed estimate rather than failing the node."""
        model = self.model_lookup.get(result.used_model_id or node.model_id)
        if not model or model.input_per_million is None or model.output_per_million is None:
            return node.estimated_cost_usd
        return (
            result.input_tokens * model.input_per_million
            + result.output_tokens * model.output_per_million
        ) / 1_000_000

    @staticmethod
    def _final_result(run: ExecutionRun):
        """Results of the graph's end nodes, which nothing else depends on, in plan order."""
        required = {dep for node in run.plan.nodes for dep in node.dependencies}
        sinks = [node for node in run.plan.nodes if node.id not in required]
        if len(sinks) == 1:
            return sinks[0].result
        return "\n\n".join(f"{node.title}:\n{node.result}" for node in sinks if node.result) or None

    def _fallback_allowed(self, run: ExecutionRun, node) -> bool:
        """A CLI node may fall back to its metered API only if enabled and within budget."""
        spec = CLI_SPECS.get(node.model_id or "")
        if not spec:
            return True
        model = self.model_lookup.get(spec.fallback_model_id)
        if not model or not model.enabled or not model.routing_ready:
            return False
        # CLI nodes reserve nothing, so the fallback's cost must fit what other nodes leave.
        reserved = run.actual_cost_usd + sum(
            item.estimated_cost_usd for item in run.plan.nodes
            if item.status == NodeStatus.RUNNING and item.id != node.id
        )
        return reserved + node_cost(node, model) <= run.budget_usd

    def _save(self, run: ExecutionRun) -> None:
        run.updated_at = datetime.now(timezone.utc)
        self.db.save_run(run)

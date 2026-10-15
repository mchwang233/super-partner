from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from . import agent as agent_tools
from . import environment, quality
from .budget import BudgetExceeded, RequestBudget
from .cli_channels import CLI_SPECS
from .config import settings
from .db import Database
from .diagnosis import parse as parse_diagnosis
from .diagnosis import prompt as diagnosis_prompt
from .diagnosis import repair_prompt, user_action
from .models import Diagnosis, Event, ExecutionRun, NodeAttempt, NodeStatus, RunStatus
from .projects import git_snapshot
from .providers import ProviderRegistry
from .quota import ModelUnavailable, failure_kind
from .routing import (
    NoEligibleModelError,
    can_act,
    diagnosers,
    eligible_for_node,
    node_cost,
    pick_diagnoser,
    pick_reviewer,
    route_node,
    strongest_for,
)

# Environment repairs per node that do not count as failed attempts.
MAX_ENVIRONMENT_FIXES = 2
# Models tried in turn for one repair when the previous one fails.
MAX_DIAGNOSERS = 3
# Reviewers tried in turn when one is out of quota.
MAX_BACKUPS = 3
# A membership CLI installing software can take a while.
REPAIR_TIMEOUT = 1800


class Executor:
    def __init__(self, db: Database, providers: ProviderRegistry, model_lookup: dict):
        self.db = db
        self.providers = providers
        self.model_lookup = model_lookup
        self.tasks: dict[str, asyncio.Task] = {}
        self.diagnosing: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.project_locks: dict[str, asyncio.Lock] = {}
        self.node_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.reservations: dict[str, dict] = {}
        self.budget_events: dict[str, asyncio.Event] = {}
        # Shared environment repairs must not overwrite one another across projects.
        self.repair_lock = asyncio.Lock()

    def start(self, run_id: str) -> None:
        task = self.tasks.get(run_id)
        if not task or task.done():
            self.tasks[run_id] = asyncio.create_task(self.execute(run_id))

    def cancel(self, run_id: str) -> None:
        for task in {self.tasks.get(run_id), self.diagnosing.get(run_id)} - {None}:
            if not task.done():
                task.cancel()

    async def shutdown(self) -> None:
        tasks = set(self.tasks.values()) | set(self.diagnosing.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def emit(self, run_id: str, kind: str, **data) -> None:
        self.db.add_event(Event(run_id=run_id, kind=kind, data=data))

    async def execute(self, run_id: str) -> None:
        run = self.db.get_run(run_id)
        if not run or run.status != RunStatus.QUEUED:
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
                    if not task.cancelled():
                        task.result()
            run = self.db.get_run(run_id)
            if not run:
                return
            failed = [node for node in run.plan.nodes if node.status == NodeStatus.FAILED]
            if failed:
                raise RuntimeError(f"{len(failed)} 个步骤失败：" + "；".join(
                    f"{node.title}（{(node.review or (node.attempts[-1].reason if node.attempts else '') or '未知原因')[:160]}）"
                    for node in failed
                ))
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
            if node.status != NodeStatus.PENDING or run.status != RunStatus.RUNNING:
                return
            if not self._out_of_quota(node.model_id):
                try:
                    self.refresh_model(run, node)
                except NoEligibleModelError as exc:
                    run.status, run.error = RunStatus.PAUSED, str(exc)
                    self._save(run)
                    self.emit(run_id, "approval.required", reason="model_unavailable", node_id=node.id)
                    return
                if node.estimated_cost_usd and run.actual_cost_usd + node.estimated_cost_usd > run.budget_usd:
                    self._pause_for_budget(run)
                    return
            node.status = NodeStatus.RUNNING
            node.started_at = datetime.now(timezone.utc)
            self._save(run)
        self.emit(run_id, "node.started", node_id=node.id, model_id=node.model_id,
                  attempt=len(node.attempts) + 1)
        generation = node.generation
        self.node_tasks[run_id, node_id] = asyncio.current_task()
        inputs = []
        work_accounted = 0.0
        try:
            prompt, inputs = quality.node_prompt(node, run, settings.handoff_max_chars)
            if self._out_of_quota(node.model_id):
                # Routed before its model ran out (at planning, or before a pause).
                raise self.providers.quota.error(node.model_id)
            result, changed, cost, project = await self._work(
                run, node, node.model_id or "mock:local", prompt, self._fallback_allowed(run, node),
            )
            work_accounted = result.accounted_cost_usd
            latest = self.db.get_run(run_id)
            stale = latest and next(n for n in latest.plan.nodes if n.id == node_id).generation != generation
            verdict = quality.Verdict(False, "上游已返工，本次结果失效。") if stale else await self._check(run, node, result.text, changed, project)
            await self._finish(run_id, node_id, result, verdict, inputs, changed, cost, None, generation=generation)
        except asyncio.CancelledError as exc:
            exc.accounted_cost_usd = getattr(exc, "accounted_cost_usd", 0.0) + work_accounted
            latest = self.db.get_run(run_id)
            if latest:
                current = next(n for n in latest.plan.nodes if n.id == node_id)
                if len(current.attempts) == len(node.attempts):
                    current.attempts.append(NodeAttempt(model_id=node.model_id, outcome="superseded" if current.generation != generation else "cancelled",
                                                        reason="上游返工或任务取消，调用已停止。", cost_usd=getattr(exc, "accounted_cost_usd", 0.0)))
                self._save(latest)
                if current.generation != generation:
                    return
            raise
        except BudgetExceeded as exc:
            exc.accounted_cost_usd = getattr(exc, "accounted_cost_usd", 0.0) + work_accounted
            await self._finish(run_id, node_id, None, quality.Verdict(False, str(exc)), inputs, [],
                               getattr(exc, "accounted_cost_usd", 0.0), "budget", generation=generation,
                               accounted=getattr(exc, "accounted_cost_usd", 0.0))
        except ModelUnavailable as exc:
            await self._switch_model(run_id, node_id, exc)
            return
        except Exception as exc:
            exc.accounted_cost_usd = getattr(exc, "accounted_cost_usd", 0.0) + work_accounted
            await self._finish(run_id, node_id, None, quality.Verdict(False, str(exc)), inputs, [],
                               getattr(exc, "accounted_cost_usd", 0.0), "error", generation=generation,
                               accounted=getattr(exc, "accounted_cost_usd", 0.0))
        finally:
            self.node_tasks.pop((run_id, node_id), None)

    def refresh_model(self, run, node):
        """Revalidate a stored selection immediately before confirming or executing it."""
        model = self.model_lookup.get(node.model_id)
        configured = self._configured(run)
        available = eligible_for_node(node, list(self.model_lookup.values()), configured,
                                      any(item.kind == "image" for item in run.attachments))
        # Lightweight provider doubles without configuration still obey explicit switches.
        valid = model and model.enabled and (
            any(item.id == node.model_id for item in available)
            if hasattr(self.providers, "configured_models") else True
        )
        if not valid:
            previous = node.model_id
            route_node(node, list(self.model_lookup.values()), configured,
                       any(item.kind == "image" for item in run.attachments))
            self.emit(run.id, "node.model_switched", node_id=node.id, model_id=previous,
                      next_model_id=node.model_id, reason="model_configuration_changed")

    def _out_of_quota(self, model_id: Optional[str]) -> bool:
        quota = getattr(self.providers, "quota", None)
        return bool(quota and model_id and quota.exhausted(model_id))

    async def _switch_model(self, run_id: str, node_id: str, exc: ModelUnavailable) -> None:
        """The node's model is unavailable: hand the node to a backup without counting
        a failed attempt, or pause the task when no other model can take it."""
        async with self.locks.setdefault(run_id, asyncio.Lock()):
            run = self.db.get_run(run_id)
            if not run:
                return
            node = next(item for item in run.plan.nodes if item.id == node_id)
            node.attempts.append(NodeAttempt(model_id=exc.model_id, outcome=exc.kind, reason=str(exc),
                                            cost_usd=getattr(exc, "accounted_cost_usd", 0.0)))
            self._route_after_unavailable(run, node, exc)
            self._save(run)

    def _route_after_unavailable(self, run: ExecutionRun, node, exc: ModelUnavailable,
                                 diagnosis_id: Optional[str] = None) -> None:
        """Select a replacement under the run lock, for a provider error or a diagnosis."""
        # Older diagnoses may have told the step to wait for its old provider. That advice
        # must not block the replacement; preserve the original verification/work feedback.
        if node.feedback:
            for diagnosis in run.diagnoses:
                if diagnosis.node_id == node.id and diagnosis.advice and (
                    diagnosis.verdict in {"quota_exhausted", "model_unavailable"}
                    or failure_kind(diagnosis.report or "")
                ):
                    node.feedback = node.feedback.replace(f"\n诊断建议：{diagnosis.advice}", "")
            node.feedback = node.feedback.strip() or None
        node.status = NodeStatus.PENDING
        models = list(self.model_lookup.values())
        needs_vision = any(item.kind == "image" for item in run.attachments)
        context = {"diagnosis_id": diagnosis_id} if diagnosis_id else {}
        context["reason"] = "quota_exhausted" if exc.kind == "quota" else "model_unavailable"
        try:
            if node.failed_attempts:
                # Already retrying, so on the strongest model; keep it that way.
                backup = strongest_for(node, models, self._configured(run), needs_vision)
                if backup is None:
                    raise NoEligibleModelError("no backup")
                node.model_id, node.estimated_cost_usd = backup.id, round(node_cost(node, backup), 6)
            else:
                route_node(node, models, self._configured(run), needs_vision)
        except NoEligibleModelError:
            run.error = (f"步骤「{node.title}」：{exc}，目前没有其它可用模型能接替。任务已暂停，"
                         "模型恢复或启用其它模型后点“按此预算继续”。")
            if run.status == RunStatus.RUNNING:
                run.status = RunStatus.PAUSED
            self.emit(run.id, "approval.required", node_id=node.id,
                      model_id=exc.model_id, until=exc.until.isoformat(), **context)
        else:
            self.emit(run.id, "node.model_switched", node_id=node.id, model_id=exc.model_id,
                      next_model_id=node.model_id, until=exc.until.isoformat(), **context)

    async def _work(self, run: ExecutionRun, node, model_id: str, prompt: str, fallback: bool):
        """Have ``model_id`` do ``node`` in its working folder: the user's project or the
        task's scratch folder. Returns (result, changed files, cost, project or None)."""
        in_project = bool(run.project_dir and node.needs_workspace)
        if in_project:
            prompt = (
                f"You are working in the user's existing project at {run.project_dir}. "
                "Edit files in place, keep changes focused on the task, and do not commit.\n\n"
                + prompt
            )
        workspace = Path(run.project_dir) if in_project else settings.workspace_root / run.id
        workspace.mkdir(parents=True, exist_ok=True)
        model = self.model_lookup.get(model_id)
        # API models act on the workspace through tools; CLIs do so on their own.
        act = bool(model and model.channel != "cli" and can_act(model))
        uses_files = model_id.startswith("cli:") or act
        before = await asyncio.to_thread(quality.file_state, workspace) if uses_files else {}
        async with contextlib.AsyncExitStack() as stack:
            if in_project:
                # CLI agents editing the same checkout concurrently would clobber each other.
                await stack.enter_async_context(self.project_locks.setdefault(run.project_dir, asyncio.Lock()))
            result = await self._complete(
                run, node, model_id, prompt, workspace, allow_api_fallback=fallback and not in_project,
                attachments=run.attachments, act=act,
            )
            try:
                after = await asyncio.to_thread(quality.file_state, workspace) if uses_files else {}
            except (Exception, asyncio.CancelledError) as exc:
                exc.accounted_cost_usd = result.accounted_cost_usd
                raise
        # The node's own model falls back to its routed estimate; a takeover has none.
        cost = self._actual_cost(result, node, None if model_id == node.model_id else model_id)
        return result, quality.changed_files(before, after), cost, workspace if in_project else None

    async def _check(self, run: ExecutionRun, node, text: str, changed: list[str], project: Optional[Path]) -> quality.Verdict:
        """Accept or reject a result: verification nodes give their own verdict; others are
        reviewed by another model against the node's acceptance criteria."""
        if node.verifies:
            verdict = quality.verification_verdict(text)
            if verdict is None:
                return quality.Verdict(False, "验证结果没有给出 VERDICT 结论，尚未验证。", fix="实际执行验证并以 VERDICT: PASS 或 VERDICT: FAIL: 原因结束。", verified=False)
            passed, problems = verdict
            return quality.Verdict(passed, "验证通过。" if passed else f"验证发现问题：{problems or '未说明'}", fix=problems)
        if not settings.review_nodes:
            return quality.Verdict(True, "未开启验收。")
        summarize = len(text) > settings.handoff_max_chars
        prompt = quality.review_prompt(
            node, text, changed, await asyncio.to_thread(quality.git_diff, project, changed) if project else "",
            summarize,
        )
        folder = settings.workspace_root / run.id / ".review"
        folder.mkdir(parents=True, exist_ok=True)
        # A reviewer out of quota leaves routing, so the next pick is a backup.
        attempted, review_cost, payload = set(), 0.0, None
        problem = "没有可用的验收模型，尚未验收。"
        for _ in range(MAX_BACKUPS):
            reviewer = pick_reviewer(list(self.model_lookup.values()), self._configured(run) - attempted,
                                     node.model_id, len(prompt) // 3 + 1000)
            if reviewer is None:
                break
            attempted.add(reviewer.id)
            try:
                reply = await self._complete(run, node, reviewer.id, prompt, folder, cli_timeout=300, allow_api_fallback=False)
                review_cost += reply.accounted_cost_usd
                payload = quality.parse_review(reply.text)
                if payload is not None:
                    break
                problem = "验收回复无法解析，尚未验收。"
            except asyncio.CancelledError as exc:
                exc.accounted_cost_usd = getattr(exc, "accounted_cost_usd", 0.0) + review_cost
                raise
            except BudgetExceeded as exc:
                exc.accounted_cost_usd = getattr(exc, "accounted_cost_usd", 0.0) + review_cost
                raise
            except ModelUnavailable as exc:
                review_cost += getattr(exc, "accounted_cost_usd", 0.0)
                continue
            except Exception as exc:
                review_cost += getattr(exc, "accounted_cost_usd", 0.0)
                problem = f"验收模型调用失败（{str(exc)[:200]}），尚未验收。"
        else:
            payload = None
        if not attempted or reviewer is None or payload is None:
            return quality.Verdict(False, problem, fix="重新验收，取得明确的验收结论。",
                                   cost_usd=review_cost, accounted_cost_usd=review_cost, verified=False)
        summary = payload.get("summary") if summarize and isinstance(payload.get("summary"), str) else None
        return quality.Verdict(
            payload["passed"], str(payload.get("reason") or ("验收通过。" if payload["passed"] else "验收未通过。")),
            fix=str(payload.get("fix") or ""), summary=summary,
            reviewer_model_id=reviewer.id, cost_usd=review_cost, accounted_cost_usd=review_cost,
        )

    async def _finish(self, run_id: str, node_id: str, result, verdict: quality.Verdict, inputs, changed, cost: float, outcome: Optional[str],
                      generation: Optional[int] = None, accounted: float = 0.0) -> None:
        """Record an attempt, then accept the node, retry it, or send verified work back."""
        lock = self.locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            run = self.db.get_run(run_id)
            if not run:
                return
            node = next(item for item in run.plan.nodes if item.id == node_id)
            stale = generation is not None and node.generation != generation
            outcome = outcome or ("unverified" if not verdict.verified else "passed" if verdict.passed else "verify_failed" if node.verifies else "rejected")
            if stale:
                outcome = "superseded"
            spent = round(cost + verdict.cost_usd, 6)
            node.attempts.append(NodeAttempt(
                model_id=(result.used_model_id if result else None) or node.model_id, outcome=outcome,
                reason=verdict.reason, reviewer_model_id=verdict.reviewer_model_id, cost_usd=spent,
            ))
            accounted += getattr(result, "accounted_cost_usd", 0.0) + verdict.accounted_cost_usd
            run.actual_cost_usd = round(run.actual_cost_usd + max(0.0, spent - accounted), 6)
            if stale:
                self._save(run)
                self.emit(run_id, "node.superseded", node_id=node.id)
                return
            checked = verdict.verified and (settings.review_nodes or bool(node.verifies))
            node.inputs, node.review_passed, node.review = inputs, verdict.passed if checked else None, verdict.reason
            if result is not None:
                node.result, node.changed_files, node.summary = result.text, changed, verdict.summary
            if outcome == "budget":
                node.status = NodeStatus.PENDING
                self._pause_for_budget(run)
            elif outcome == "passed":
                node.status, node.feedback, node.failed_attempts = NodeStatus.SUCCEEDED, None, 0
                self.emit(run_id, "node.succeeded", node_id=node.id, cost_usd=spent,
                          selected_model_id=node.model_id, used_model_id=result.used_model_id if result else None)
            elif outcome == "verify_failed":
                self._send_back(run, node, verdict)
            elif settings.auto_diagnose:
                # Every failed attempt is diagnosed first; _repair_done then retries or pauses.
                node.status = NodeStatus.RUNNING
            else:
                self._retry_or_fail(run, node, verdict, outcome)
            if run.actual_cost_usd > run.budget_usd and run.status == RunStatus.RUNNING:
                run.status = RunStatus.PAUSED
                self.emit(run_id, "approval.required", reason="budget_exceeded", actual_cost_usd=run.actual_cost_usd)
            # A verification out of rework rounds is a failure too.
            diagnose = settings.auto_diagnose and (
                node.status == NodeStatus.RUNNING or (outcome == "verify_failed" and node.status == NodeStatus.FAILED)
            )
            self._save(run)
        if diagnose:
            await self._repair(run_id, node_id, verdict, outcome)

    async def _repair(self, run_id: str, node_id: str, verdict: quality.Verdict, outcome: str,
                      diagnosis_id: Optional[str] = None) -> None:
        """After a failed attempt, a strong model (a membership CLI when possible) looks for
        what blocks the node outside the task (missing software, sandbox or machine limits,
        configuration) and repairs it. Its status decides the rest (see _repair_done).

        Runs inside the node's task, so the run waits for it instead of moving on.
        """
        lock = self.locks.setdefault(run_id, asyncio.Lock())
        while True:
            async with lock:
                run = self.db.get_run(run_id)
                if not run or run.status == RunStatus.CANCELLED:
                    return
                if diagnosis_id:
                    node = next(item for item in run.plan.nodes if item.id == node_id)
                    diagnosis = next(item for item in run.diagnoses if item.id == diagnosis_id)
                    break
                busy = self.diagnosing.get(run_id)
                if not busy or busy.done():
                    node = next(item for item in run.plan.nodes if item.id == node_id)
                    diagnosis_id = self.begin_diagnosis(run, "auto", node_id)
                    diagnosis = run.diagnoses[-1]
                    if diagnosis_id:
                        self.diagnosing[run_id] = asyncio.current_task()
                    self._save(run)
                    break
            # Another diagnosis (manual, or another node's repair) is running; this one follows it.
            await asyncio.wait([busy])
        if not diagnosis_id:
            return await self._repair_done(run_id, node_id, diagnosis.id, None, None, 0.0, verdict, outcome)
        workspace = Path(run.project_dir) if run.project_dir and node.needs_workspace else settings.workspace_root / run_id
        workspace.mkdir(parents=True, exist_ok=True)
        events = [f"{event.created_at:%H:%M:%S} {event.kind} {event.data}" for event in self.db.events(run_id)]
        cost = 0.0
        try:
            async with self.repair_lock:
                shared = environment.prepare()
                before = await asyncio.to_thread(quality.file_state, shared)
                prompt = repair_prompt(run, node, str(workspace), str(shared), agent_tools.sandbox_ports(),
                                       events, self.db.shared_repairs(limit=5))
                result, _model_id, cost = await self._diagnose_with(run, diagnosis, node, prompt, workspace, repair=True)
                after = await asyncio.to_thread(quality.file_state, shared)
                shared_files = quality.changed_files(before, after)
        except asyncio.CancelledError as exc:
            if diagnosis.trigger != "manual":
                self.diagnosing.pop(run_id, None)
            # Stopped by a cancel or shutdown; the record must not stay "running".
            stopped = self.db.get_run(run_id)
            for item in stopped.diagnoses if stopped else []:
                if item.id == diagnosis.id and item.status == "running":
                    item.status, item.error = "failed", "修复被中断（任务取消或服务重启）。"
                    item.cost_usd = round(max(cost, getattr(exc, "accounted_cost_usd", 0.0)), 6)
                    item.finished_at = datetime.now(timezone.utc)
                    self._save(stopped)
            raise
        except Exception as exc:
            cost = max(cost, getattr(exc, "accounted_cost_usd", 0.0))
            if diagnosis.trigger != "manual":
                self.diagnosing.pop(run_id, None)
            if isinstance(exc, BudgetExceeded):
                async with self.locks.setdefault(run_id, asyncio.Lock()):
                    current = self.db.get_run(run_id)
                    target = next(n for n in current.plan.nodes if n.id == node_id)
                    target.status = NodeStatus.PENDING
                    record = next(d for d in current.diagnoses if d.id == diagnosis.id)
                    record.status, record.error = "failed", str(exc)
                    record.cost_usd = round(cost, 6)
                    record.finished_at = datetime.now(timezone.utc)
                    self._pause_for_budget(current)
                    self.emit(run_id, "run.diagnosed", diagnosis_id=record.id, status=record.status)
                return
            return await self._repair_done(run_id, node_id, diagnosis.id, None, str(exc)[:500],
                                           cost, verdict, outcome)
        if diagnosis.trigger != "manual":
            self.diagnosing.pop(run_id, None)
        await self._repair_done(run_id, node_id, diagnosis.id, result.text, None, cost, verdict, outcome, shared_files)

    async def _diagnose_with(self, run: ExecutionRun, diagnosis: Diagnosis, node, prompt: str, workspace: Path, repair: bool):
        """Run a diagnosis on its model, or, if that fails (a CLI at its usage limit, say), on
        the next diagnosers in turn. Returns the result and the model that gave it; switches are
        recorded as events, and the last failure is raised."""
        run_id, diagnosis_id = run.id, diagnosis.id
        candidates = ([diagnosis.model_id] + [
            model.id for model in diagnosers(list(self.model_lookup.values()), self._configured(run))
            if model.id != diagnosis.model_id
        ])[:MAX_DIAGNOSERS]
        cost = 0.0
        for index, model_id in enumerate(candidates):
            model = self.model_lookup.get(model_id)
            # Repairs act on the machine; an analysis only reads, which CLIs do on their own.
            act = bool(repair and node and model and model.channel != "cli" and can_act(model))
            run = self.db.get_run(run_id)
            try:
                result = await self._complete(
                    run, node, model_id, prompt, workspace, cli_timeout=REPAIR_TIMEOUT if repair else 600,
                    allow_api_fallback=False, act=act,
                    repair=repair,
                )
                return result, model_id, cost + result.accounted_cost_usd
            except asyncio.CancelledError as exc:
                exc.accounted_cost_usd = getattr(exc, "accounted_cost_usd", 0.0) + cost
                raise
            except Exception as exc:
                cost += getattr(exc, "accounted_cost_usd", 0.0)
                if isinstance(exc, BudgetExceeded) or index == len(candidates) - 1:
                    exc.accounted_cost_usd = cost
                    raise
                following = candidates[index + 1]
                async with self.locks.setdefault(run_id, asyncio.Lock()):
                    run = self.db.get_run(run_id)
                    for item in run.diagnoses:
                        if item.id == diagnosis_id:
                            item.model_id = following
                    self._save(run)
                self.emit(run_id, "run.diagnosis_fallback", diagnosis_id=diagnosis_id, model_id=model_id,
                          next_model_id=following, error=str(exc)[:300])

    async def _repair_done(self, run_id: str, node_id: str, diagnosis_id: str, text, error, cost: float,
                           verdict: quality.Verdict, outcome: str, shared_files: Optional[list[str]] = None) -> None:
        """Act on the diagnosis of a failed attempt:

        - FIXED: the environment was repaired; run the node again without counting the failure
          (at most MAX_ENVIRONMENT_FIXES times per node, then it counts as RETRY);
        - RETRY, or no diagnosis: the usual retry, with the diagnosis's advice; out of attempts,
          the task pauses with the report;
        - QUOTA_EXHAUSTED: exclude the execution model and hand its work to a backup;
        - BLOCKED: the user must act; the task pauses with the report.
        """
        async with self.locks.setdefault(run_id, asyncio.Lock()):
            run = self.db.get_run(run_id)
            if not run:
                return
            node = next(item for item in run.plan.nodes if item.id == node_id)
            diagnosis = next(item for item in run.diagnoses if item.id == diagnosis_id)
            if diagnosis.status == "running":
                diagnosis.status, diagnosis.error = ("failed", error) if error else ("done", None)
                diagnosis.cost_usd, diagnosis.finished_at = round(cost, 6), datetime.now(timezone.utc)
            if text is not None:
                diagnosis.report, diagnosis.advice, diagnosis.verdict = parse_diagnosis(text)
                diagnosis.user_action = user_action(text)
                # An answer without a status line still had its say; retrying is the safe reading.
                diagnosis.verdict = diagnosis.verdict or "retry"
            # A known provider limit must not become a user-blocking environment problem,
            # even if a diagnosis used the older BLOCKED response format.
            known_failure = failure_kind(verdict.reason) if outcome == "error" else None
            if diagnosis.status == "done" and known_failure:
                diagnosis.verdict = "quota_exhausted" if known_failure == "quota" else "model_unavailable"
            if diagnosis.verdict == "blocked" and not diagnosis.user_action:
                diagnosis.verdict = "retry"
                diagnosis.advice = (diagnosis.advice or "") + (
                    "\n诊断未指出必须由用户提供的权限、凭据、预算或决定；请执行可用的修复并验证结果，"
                    "公共环境问题修复到共享工具目录。")
            self.emit(run_id, "run.diagnosed", diagnosis_id=diagnosis_id, status=diagnosis.status,
                      model_id=diagnosis.model_id, verdict=diagnosis.verdict)
            if run.status == RunStatus.CANCELLED or node.status not in {NodeStatus.RUNNING, NodeStatus.FAILED}:
                self._save(run)
                return
            status = diagnosis.verdict if diagnosis.status == "done" else None
            if status in {"quota_exhausted", "model_unavailable"} and outcome == "error" and node.model_id:
                if not node.attempts:
                    node.attempts.append(NodeAttempt(model_id=node.model_id, outcome="error", reason=verdict.reason))
                failed_model = node.attempts[-1].model_id or node.model_id
                kind = "quota" if status == "quota_exhausted" else "unavailable"
                self.providers.quota.learn(failed_model, verdict.reason, kind, run.id, diagnosis.id)
                exc = self.providers.quota.mark(failed_model, f"{verdict.reason}\n{diagnosis.report or ''}", kind)
                # _finish already recorded this attempt. Reclassify it without duplicating
                # its cost/history or consuming a quality retry.
                node.attempts[-1].outcome = kind
                node.review, node.review_passed = str(exc), None
                self._route_after_unavailable(run, node, exc, diagnosis.id)
            elif status == "fixed" and node.environment_fixes < MAX_ENVIRONMENT_FIXES:
                if shared_files:
                    self.db.save_shared_repair({
                        "id": diagnosis.id, "run_id": run.id, "model_id": diagnosis.model_id,
                        "files": shared_files, "report": diagnosis.report or "",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    })
                    self.emit(run.id, "run.repair_shared", diagnosis_id=diagnosis.id, files=shared_files)
                node.environment_fixes += 1
                node.feedback = f"{verdict.reason}\n环境已修复，请重新执行这一步：\n{diagnosis.report or ''}".strip()
                node.status = NodeStatus.PENDING
                if outcome == "verify_failed":
                    # The verification had used up its rework rounds; it gets one more.
                    run.rework_rounds = max(0, settings.max_rework_rounds - 1)
                self.emit(run_id, "node.retry", node_id=node.id, reason="environment_fixed",
                          model_id=node.model_id, attempt=len(node.attempts) + 1)
            elif status == "blocked":
                node.status = NodeStatus.FAILED
                self._pause_for_diagnosis(run, node, diagnosis, "遇到需要你处理的问题")
            elif outcome == "verify_failed":
                self._pause_for_diagnosis(run, node, diagnosis, f"返工 {run.rework_rounds} 轮后验证仍未通过")
            else:
                self._retry_or_fail(run, node, verdict, outcome)
                if node.status == NodeStatus.PENDING and diagnosis.advice:
                    node.feedback = f"{node.feedback}\n诊断建议：{diagnosis.advice}"
                elif node.status == NodeStatus.FAILED:
                    why = f"重试 {node.failed_attempts} 次仍未通过" + (
                        f"（诊断没有完成：{diagnosis.error or '没有可用的诊断模型'}）" if diagnosis.status != "done" else "")
                    self._pause_for_diagnosis(run, node, diagnosis, why)
            self._save(run)

    def _pause_for_diagnosis(self, run: ExecutionRun, node, diagnosis: Diagnosis, why: str) -> None:
        run.error = f"步骤「{node.title}」{why}，任务已暂停。请查看诊断报告，处理后点“按此预算继续”。"
        if run.status == RunStatus.RUNNING:
            run.status = RunStatus.PAUSED
        self.emit(run.id, "approval.required", reason="diagnosis_unresolved",
                  node_id=node.id, diagnosis_id=diagnosis.id)

    def _retry_or_fail(self, run: ExecutionRun, node, verdict: quality.Verdict, outcome: str) -> None:
        node.failed_attempts += 1
        problem = verdict.reason + (f"\n应修改：{verdict.fix}" if verdict.fix else "")
        if node.failed_attempts >= settings.max_node_attempts:
            node.status = NodeStatus.FAILED
            if outcome == "error":
                node.result = verdict.reason
            self.emit(run.id, "node.failed", node_id=node.id, error=verdict.reason, attempts=len(node.attempts))
            return
        # Retry with the problems spelled out, on the strongest model that can take the node.
        node.feedback = problem
        stronger = self._strongest(run, node)
        if stronger and stronger.id != node.model_id:
            node.model_id = stronger.id
            node.estimated_cost_usd = round(node_cost(node, stronger), 6)
        node.status = NodeStatus.PENDING
        self.emit(run.id, "node.retry", node_id=node.id, reason=verdict.reason, model_id=node.model_id,
                  attempt=node.failed_attempts + 1)

    def _send_back(self, run: ExecutionRun, verifier, verdict: quality.Verdict) -> None:
        """A failed verification reopens the verified nodes with its findings, and everything
        that already used their results, including the verifier itself."""
        if run.rework_rounds >= settings.max_rework_rounds:
            verifier.status = NodeStatus.FAILED
            verifier.review = f"返工 {run.rework_rounds} 轮后验证仍未通过：{verdict.fix or verdict.reason}"
            self.emit(run.id, "node.failed", node_id=verifier.id, error=verifier.review)
            return
        run.rework_rounds += 1
        targets = {item for item in verifier.verifies if any(node.id == item for node in run.plan.nodes)}
        reopened = targets | quality.descendants(run, targets) | {verifier.id}
        for node in run.plan.nodes:
            if node.id not in reopened:
                continue
            node.generation += 1
            task = self.node_tasks.get((run.id, node.id))
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
            node.status, node.failed_attempts = NodeStatus.PENDING, 0
            node.feedback = f"验证（{verifier.title}）发现问题：{verdict.fix or verdict.reason}" if node.id in targets else None
        self.emit(run.id, "run.rework", round=run.rework_rounds, verifier=verifier.id,
                  reopened=sorted(reopened), problems=verdict.fix or verdict.reason)

    def begin_diagnosis(self, run: ExecutionRun, trigger: str, node_id: Optional[str] = None) -> Optional[str]:
        """Record a new diagnosis on ``run`` (the caller saves it); None if one is running."""
        task = self.diagnosing.get(run.id)
        if task and not task.done():
            return None
        model = pick_diagnoser(list(self.model_lookup.values()), self._configured(run))
        diagnosis = Diagnosis(id=uuid4().hex[:8], trigger=trigger, node_id=node_id, model_id=model.id if model else None)
        if not model:
            diagnosis.status, diagnosis.error = "failed", "没有可用的诊断模型：请启用一个会员 CLI 或 API 模型。"
            diagnosis.finished_at = datetime.now(timezone.utc)
        run.diagnoses.append(diagnosis)
        self.emit(run.id, "run.diagnosis_started", diagnosis_id=diagnosis.id, trigger=trigger,
                  node_id=node_id, model_id=diagnosis.model_id)
        return diagnosis.id if model else None

    def start_diagnosis(self, run_id: str, diagnosis_id: str) -> None:
        self.diagnosing[run_id] = asyncio.create_task(self._diagnose(run_id, diagnosis_id))

    async def _diagnose(self, run_id: str, diagnosis_id: str) -> None:
        try:
            await self._run_diagnosis(run_id, diagnosis_id)
        except (Exception, asyncio.CancelledError) as exc:
            async with self.locks.setdefault(run_id, asyncio.Lock()):
                latest = self.db.get_run(run_id)
                record = next((d for d in latest.diagnoses if d.id == diagnosis_id), None) if latest else None
                if record and record.status == "running":
                    record.status, record.error = "failed", str(exc) or "诊断被中断。"
                    record.cost_usd = round(getattr(exc, "accounted_cost_usd", 0.0), 6)
                    record.finished_at = datetime.now(timezone.utc)
                if latest:
                    for node in latest.plan.nodes:
                        if record and node.id == record.node_id and node.status == NodeStatus.RUNNING and latest.status != RunStatus.RUNNING:
                            node.status = NodeStatus.CANCELLED if latest.status == RunStatus.CANCELLED else NodeStatus.PENDING
                    self._save(latest)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            if self.diagnosing.get(run_id) is asyncio.current_task():
                self.diagnosing.pop(run_id, None)

    async def _run_diagnosis(self, run_id: str, diagnosis_id: str) -> None:
        run = self.db.get_run(run_id)
        diagnosis = next((item for item in run.diagnoses if item.id == diagnosis_id), None) if run else None
        if not diagnosis or not diagnosis.model_id:
            return
        node = next((item for item in run.plan.nodes if item.id == diagnosis.node_id), None)
        if node and run.status in {RunStatus.PAUSED, RunStatus.FAILED} and node.status in {NodeStatus.FAILED, NodeStatus.PENDING}:
            # A requested diagnosis of a stopped step uses the same repair/switching path
            # as automatic recovery, then continues within the previously approved budget.
            last = node.attempts[-1] if node.attempts else None
            verdict = quality.Verdict(False, (last.reason if last else None) or node.review or run.error or "步骤未完成")
            outcome = "error" if not last or last.outcome in {"error", "quota", "unavailable"} else last.outcome
            node.status, node.failed_attempts, node.environment_fixes = NodeStatus.RUNNING, 0, 0
            self._save(run)
            await self._repair(run_id, node.id, verdict, outcome, diagnosis_id=diagnosis_id)
            async with self.locks.setdefault(run_id, asyncio.Lock()):
                latest = self.db.get_run(run_id)
                current = next((item for item in latest.plan.nodes if item.id == node.id), None) if latest else None
                if not current or latest.status == RunStatus.CANCELLED or current.status != NodeStatus.PENDING:
                    return
                # A failed/budget-blocked diagnosis does not authorize another blind attempt.
                record = next(d for d in latest.diagnoses if d.id == diagnosis_id)
                if record.status != "done":
                    return
                try:
                    self.refresh_model(latest, current)
                except NoEligibleModelError:
                    return
                required = latest.actual_cost_usd + sum(n.estimated_cost_usd for n in latest.plan.nodes if n.status != NodeStatus.SUCCEEDED)
                if required > latest.budget_usd:
                    self._pause_for_budget(latest)
                    return
                active = self.tasks.get(run_id)
                latest.status = RunStatus.RUNNING if active and not active.done() else RunStatus.QUEUED
                latest.error = None
                self._save(latest)
                self.emit(run_id, "run.recovered", diagnosis_id=diagnosis_id)
                self.start(run_id)
            return
        workspace = Path(run.project_dir) if run.project_dir else settings.workspace_root / run_id
        workspace.mkdir(parents=True, exist_ok=True)
        events = [f"{event.created_at:%H:%M:%S} {event.kind} {event.data}" for event in self.db.events(run_id)]
        report = advice = error = None
        cost = 0.0
        try:
            result, _model_id, cost = await self._diagnose_with(
                run, diagnosis, None, diagnosis_prompt(run, diagnosis.node_id, str(workspace), events), workspace, repair=False,
            )
            report, advice, status = parse_diagnosis(result.text)
        except asyncio.CancelledError as exc:
            async with self.locks.setdefault(run_id, asyncio.Lock()):
                latest = self.db.get_run(run_id)
                target = next((d for d in latest.diagnoses if d.id == diagnosis_id), None) if latest else None
                if target:
                    target.status, target.error = "failed", "诊断被中断（任务取消或服务重启）。"
                    target.cost_usd = round(getattr(exc, "accounted_cost_usd", 0.0), 6)
                    target.finished_at = datetime.now(timezone.utc)
                    self._save(latest)
            raise
        except Exception as exc:
            error = str(exc)[:500]
            cost = getattr(exc, "accounted_cost_usd", 0.0)
        finally:
            self.diagnosing.pop(run_id, None)
        async with self.locks.setdefault(run_id, asyncio.Lock()):
            latest = self.db.get_run(run_id)
            target = next((item for item in latest.diagnoses if item.id == diagnosis_id), None) if latest else None
            if not target:
                return
            target.status = "failed" if error else "done"
            target.report, target.advice, target.error, target.cost_usd = report, advice, error, round(cost, 6)
            target.verdict = status if not error else None
            target.finished_at = datetime.now(timezone.utc)
            self._save(latest)
        self.emit(run_id, "run.diagnosed", diagnosis_id=diagnosis_id, status=target.status, model_id=target.model_id)

    async def _complete(self, run, node, model_id, prompt, workspace, **kwargs):
        """Persist each received usage report, even if a later round fails or is cancelled."""
        async def charge(cost):
            if not cost:
                return
            async with self.locks.setdefault(run.id, asyncio.Lock()):
                latest = self.db.get_run(run.id)
                if latest:
                    latest.actual_cost_usd = round(latest.actual_cost_usd + cost, 6)
                    self._save(latest)

        def remaining():
            latest = self.db.get_run(run.id)
            return latest.budget_usd - latest.actual_cost_usd if latest else float("-inf")

        budget = RequestBudget(self.model_lookup, remaining, self.reservations.setdefault(run.id, {}), charge,
                               self.budget_events.setdefault(run.id, asyncio.Event()))
        result = None
        try:
            result = await self.providers.complete(model_id, prompt, workspace, spend_ok=budget, **kwargs)
            # CLI calls and provider adapters without streaming usage are accounted here.
            extra = max(0.0, self._actual_cost(result, node, None if node and model_id == node.model_id else model_id) - budget.spent)
            await charge(extra)
            result.accounted_cost_usd = budget.spent + extra
            return result
        except (Exception, asyncio.CancelledError) as exc:
            usage = getattr(exc, "usage", None) or result
            extra = max(0.0, self._actual_cost(usage, node, model_id) - budget.spent) if usage else 0.0
            await charge(extra)
            exc.accounted_cost_usd = budget.spent + extra
            raise
        finally:
            budget.release()

    def _configured(self, run: ExecutionRun) -> set[str]:
        configured_models = getattr(self.providers, "configured_models", None)
        if configured_models is None:
            return set()
        configured = configured_models(list(self.model_lookup))
        if any(node.model_id and not node.model_id.startswith("mock:") for node in run.plan.nodes):
            configured = {item for item in configured if not item.startswith("mock:")}
        return configured if run.use_cli else {item for item in configured if not item.startswith("cli:")}

    def _strongest(self, run: ExecutionRun, node):
        needs_vision = any(item.kind == "image" for item in run.attachments)
        return strongest_for(node, list(self.model_lookup.values()), self._configured(run), needs_vision)

    def _reviewer(self, run: ExecutionRun, node, tokens: int):
        return pick_reviewer(list(self.model_lookup.values()), self._configured(run), node.model_id, tokens)

    def _actual_cost(self, result, node, model_id: Optional[str] = None) -> float:
        """Charge by token usage; if the model or its price has since left the catalog, the
        work is still done, so charge the routed estimate rather than failing the node."""
        model = self.model_lookup.get(result.used_model_id or model_id or node.model_id)
        if not model or model.input_per_million is None or model.output_per_million is None:
            return 0.0 if model_id or node is None else node.estimated_cost_usd
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

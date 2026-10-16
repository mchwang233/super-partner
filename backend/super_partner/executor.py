from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import quality, supervisor
from .budget import BudgetExceeded, RequestBudget
from .cli_channels import CLI_SPECS
from .config import settings
from .db import Database
from .diagnosing import Diagnosing
from .leading import Leading
from .models import Diagnosis, Event, ExecutionRun, NodeAttempt, NodeStatus, Outcome, RunStatus
from .platform_repair import PlatformRepairs
from .projects import git_snapshot
from .providers import ProviderRegistry
from .quota import ModelUnavailable, failure_kind
from .recovery import retry_model, turns_for
from .review import Reviewing
from .routing import NoEligibleModelError, can_act, eligible_for_node, node_cost, route_node
from .self_repair import SelfRepair

# How a model call that ran out of turns reads in an attempt's reason (providers.TURN_LIMIT_MARK).
TURN_LIMIT_HINT = "轮工具调用后仍未完成"

# Diagnosis report kept with a step that cannot be done.
MAX_UNABLE_REPORT = 1500


class Executor(Reviewing, Leading, Diagnosing, SelfRepair):
    """Runs a task's steps: scheduling, attempts, retries and step outcomes. Reviewing
    (review.py), the lead's decisions (leading.py), diagnosing (diagnosing.py) and self-repair
    (self_repair.py) are mixed in."""

    def __init__(self, db: Database, providers: ProviderRegistry, model_lookup: dict):
        self.db = db
        self.providers = providers
        self.model_lookup = model_lookup
        self.tasks: dict[str, asyncio.Task] = {}
        self.diagnosing: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        # One message at a time in each task's conversation with its lead.
        self.lead_locks: dict[str, asyncio.Lock] = {}
        self.project_locks: dict[str, asyncio.Lock] = {}
        self.node_tasks: dict[tuple[str, str], asyncio.Task] = {}
        # Tool calls of each node's attempt in progress, written onto the attempt when it ends.
        self.tool_logs: dict[tuple[str, str], list[dict]] = {}
        self.reservations: dict[str, dict] = {}
        self.budget_events: dict[str, asyncio.Event] = {}
        # Shared environment repairs must not overwrite one another across projects.
        self.repair_lock = asyncio.Lock()
        self.platform = PlatformRepairs(settings.platform_root, settings.data_dir, settings.api_base_url,
                                        settings.platform_self_repair,
                                        self.db.connection.execute("PRAGMA database_list").fetchone()[2])
        self.platform_pending = None
        self.maintenance_task: Optional[asyncio.Task] = None
        self.active_requests = 0

    def start(self, run_id: str) -> None:
        if self.platform_pending:
            run = self.db.get_run(run_id)
            if run:
                self._hold_for_platform(run)
            return
        task = self.tasks.get(run_id)
        if not task or task.done():
            self.tasks[run_id] = asyncio.create_task(self.execute(run_id))

    def cancel(self, run_id: str) -> None:
        for task in {self.tasks.get(run_id), self.diagnosing.get(run_id)} - {None}:
            if not task.done():
                task.cancel()

    async def shutdown(self) -> None:
        tasks = set(self.tasks.values()) | set(self.diagnosing.values())
        if self.maintenance_task:
            tasks.add(self.maintenance_task)
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
                    if self.platform_pending:
                        self._hold_for_platform(run)
                        if not running:
                            return
                        dispatched_for_budget = False
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
            unable = [node for node in run.plan.nodes if node.status == NodeStatus.UNABLE]
            if unable:
                self._end_incomplete(run, unable)
                return
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

    def _end_incomplete(self, run: ExecutionRun, unable: list) -> None:
        """Everything possible is done; say plainly what is not and why, instead of success."""
        skipped = quality.descendants(run, {node.id for node in unable})
        for node in run.plan.nodes:
            if node.id in skipped and node.status == NodeStatus.PENDING:
                node.status = NodeStatus.BLOCKED
                node.review = "依赖的步骤无法完成，这一步没有执行。"
        reasons = "；".join(f"{node.title}（{(node.unable_reason or '').splitlines()[0][:160]}）" for node in unable)
        done = self._final_result(run, completed_only=True)
        sections = [
            f"## 未能完成\n\n{len(unable)} 个步骤在当前环境中无法完成，任务没有全部做完：",
            *[f"### {node.title}\n\n{node.unable_reason or '未说明原因。'}" for node in unable],
        ]
        blocked = [node.title for node in run.plan.nodes if node.status == NodeStatus.BLOCKED]
        if blocked:
            sections.append("因此没有执行：" + "、".join(f"「{title}」" for title in blocked))
        if done:
            sections.append(f"## 已完成的部分\n\n{done}")
        run.status = RunStatus.INCOMPLETE
        run.final_result = "\n\n".join(sections)
        run.error = f"{len(unable)} 个步骤无法完成：{reasons}"
        self._save(run)
        self.emit(run.id, "run.incomplete", actual_cost_usd=run.actual_cost_usd,
                  unable=[node.id for node in unable], blocked=blocked)

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
            if self.platform_pending:
                self._hold_for_platform(run)
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
            result, changed, cost, workspace = await self._work(
                run, node, node.model_id or "mock:local", prompt, self._fallback_allowed(run, node),
            )
            work_accounted = result.accounted_cost_usd
            latest = self.db.get_run(run_id)
            stale = latest and next(n for n in latest.plan.nodes if n.id == node_id).generation != generation
            verdict = quality.Verdict(False, "上游已返工，本次结果失效。") if stale else await self._check(run, node, result.text, changed, workspace)
            if result.turn_limit and verdict.passed and not verdict.reviewer_model_id:
                # Nobody checked it, and a summary made at the turn limit says the work is unfinished.
                verdict = quality.Verdict(False, f"{TURN_LIMIT_HINT}，只交了进度总结，且没有验收模型确认已完成。")
            await self._finish(run_id, node_id, result, verdict, inputs, changed, cost, None, generation=generation)
        except asyncio.CancelledError as exc:
            exc.accounted_cost_usd = getattr(exc, "accounted_cost_usd", 0.0) + work_accounted
            latest = self.db.get_run(run_id)
            if latest:
                current = next(n for n in latest.plan.nodes if n.id == node_id)
                if len(current.attempts) == len(node.attempts):
                    current.attempts.append(NodeAttempt(model_id=node.model_id, outcome=Outcome.SUPERSEDED if current.generation != generation else Outcome.CANCELLED,
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
                # Already retrying, so on the strongest model that has not failed it; keep it that way.
                backup = retry_model(node, models, self._configured(run), needs_vision)
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
        task's scratch folder. Returns (result, changed files, cost, working folder)."""
        in_project = bool(run.project_dir and node.needs_workspace)
        if in_project:
            prompt = (
                f"You are working in the user's existing project at {run.project_dir}. "
                "Edit files in place, keep changes focused on the task, and do not commit.\n\n"
                + prompt
            )
        workspace = Path(run.project_dir) if in_project else settings.workspace_root / run.folder_id
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
            log = self.tool_logs[run.id, node.id] = []
            result = await self._complete(
                run, node, model_id, prompt, workspace, allow_api_fallback=fallback and not in_project,
                attachments=run.attachments, act=act, max_turns=turns_for(node), tool_log=log,
                watch=self._progress_watch(run, node, model_id),
            )
            try:
                after = await asyncio.to_thread(quality.file_state, workspace) if uses_files else {}
            except (Exception, asyncio.CancelledError) as exc:
                exc.accounted_cost_usd = result.accounted_cost_usd
                raise
        # The node's own model falls back to its routed estimate; a takeover has none.
        cost = self._actual_cost(result, node, None if model_id == node.model_id else model_id)
        return result, quality.changed_files(before, after), cost, workspace


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
            # A verification that ran out of turns has not finished, which says nothing against
            # the work it verifies: it is retried itself rather than sending that work back.
            unfinished = bool(getattr(result, "turn_limit", False)) and TURN_LIMIT_HINT in (verdict.reason or "")
            outcome = outcome or (Outcome.UNABLE if verdict.unable else Outcome.UNVERIFIED if not verdict.verified
                                  else Outcome.PASSED if verdict.passed
                                  else Outcome.VERIFY_FAILED if node.verifies and not unfinished
                                  else Outcome.REJECTED)
            if stale:
                outcome = Outcome.SUPERSEDED
            spent = round(cost + verdict.cost_usd, 6)
            log = self.tool_logs.pop((run_id, node_id), None) or []
            node.attempts.append(NodeAttempt(
                model_id=(result.used_model_id if result else None) or node.model_id, outcome=outcome,
                reason=verdict.reason, reviewer_model_id=verdict.reviewer_model_id, cost_usd=spent,
                turns=getattr(result, "turns", 0) or max((call["turn"] for call in log), default=0),
                turn_limit=bool(getattr(result, "turn_limit", False)) or TURN_LIMIT_HINT in (verdict.reason or ""),
                tool_calls=log,
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
                node.result, node.summary = result.text, verdict.summary
                node.changed_files = list(dict.fromkeys([*node.changed_files, *changed]))
            if outcome == Outcome.BUDGET:
                node.status = NodeStatus.PENDING
                self._pause_for_budget(run)
            elif outcome == Outcome.PASSED:
                node.status, node.feedback, node.failed_attempts = NodeStatus.SUCCEEDED, None, 0
                self.emit(run_id, "node.succeeded", node_id=node.id, cost_usd=spent,
                          selected_model_id=node.model_id, used_model_id=result.used_model_id if result else None)
            elif outcome == Outcome.VERIFY_FAILED:
                if self._can_lead():
                    # The lead judges the findings before any work is sent back (leading.py).
                    node.status = NodeStatus.RUNNING
                else:
                    self._send_back(run, node, verdict)
            elif settings.auto_diagnose:
                # Every failed attempt is diagnosed first; _repair_done then retries or pauses.
                node.status = NodeStatus.RUNNING
            elif outcome == Outcome.UNABLE:
                # Without a diagnosis to change the environment, another attempt meets the same limit.
                self._mark_unable(run, node, verdict.unable)
            else:
                self._retry_or_fail(run, node, verdict, outcome)
            if run.actual_cost_usd > run.budget_usd and run.status == RunStatus.RUNNING:
                run.status = RunStatus.PAUSED
                self.emit(run_id, "approval.required", reason="budget_exceeded", actual_cost_usd=run.actual_cost_usd)
            # A verification out of rework rounds is a failure too.
            diagnose = settings.auto_diagnose and (
                node.status == NodeStatus.RUNNING or (outcome == Outcome.VERIFY_FAILED and node.status == NodeStatus.FAILED)
            )
            # A failed attempt held for recovery goes to the task's lead (leading.py).
            lead = node.status == NodeStatus.RUNNING and self._can_lead() and (
                settings.auto_diagnose or outcome == Outcome.VERIFY_FAILED)
            self._save(run)
        if lead:
            await self._decide(run_id, node_id, verdict, outcome)
        elif diagnose:
            await self._repair(run_id, node_id, verdict, outcome)





    def _retry_or_fail(self, run: ExecutionRun, node, verdict: quality.Verdict, outcome: str) -> None:
        node.failed_attempts += 1
        problem = verdict.reason + (f"\n应修改：{verdict.fix}" if verdict.fix else "")
        if node.failed_attempts >= settings.max_node_attempts:
            if outcome == Outcome.UNABLE:
                self._mark_unable(run, node, verdict.unable or verdict.reason)
                return
            node.status = NodeStatus.FAILED
            if outcome == Outcome.ERROR:
                node.result = verdict.reason
            self.emit(run.id, "node.failed", node_id=node.id, error=verdict.reason, attempts=len(node.attempts))
            return
        # Retry with the problems spelled out, on the strongest model that can take the node; one
        # that failed it twice in a row makes way for another, a CLI included.
        node.feedback = quality.retry_feedback(node.feedback, problem)
        needs_vision = any(item.kind == "image" for item in run.attachments)
        stronger = retry_model(node, list(self.model_lookup.values()), self._configured(run), needs_vision)
        if stronger and stronger.id != node.model_id:
            node.model_id = stronger.id
            node.estimated_cost_usd = round(node_cost(node, stronger), 6)
        node.status = NodeStatus.PENDING
        self.emit(run.id, "node.retry", node_id=node.id, reason=verdict.reason, model_id=node.model_id,
                  attempt=node.failed_attempts + 1)

    def _mark_unable(self, run: ExecutionRun, node, reason: Optional[str], diagnosis: Optional[Diagnosis] = None,
                     user_action: Optional[str] = None) -> None:
        """End a step that cannot be done in this environment, saying why and what the user
        could do; steps that need its result are skipped when the task ends."""
        action = user_action or (diagnosis.user_action if diagnosis else None)
        report = (diagnosis.report or "")[:MAX_UNABLE_REPORT] if diagnosis and diagnosis.status == "done" else ""
        node.status = NodeStatus.UNABLE
        node.unable_reason = "\n\n".join(part for part in [
            (reason or "这一步在当前环境中无法完成。").strip(),
            f"需要你处理：{action}" if action else "",
            f"诊断结论：\n{report}" if report else "",
        ] if part)
        node.review = f"无法完成：{(reason or '').strip()}"[:500]
        self.emit(run.id, "node.unable", node_id=node.id, reason=reason, user_action=action)

    def _send_back(self, run: ExecutionRun, verifier, verdict: quality.Verdict, targets: Optional[set] = None,
                   note: Optional[str] = None, limited: bool = True) -> None:
        """A failed verification reopens the verified nodes (or the ``targets`` among them) with
        its findings and ``note``, and everything that already used their results, including the
        verifier itself. Without the lead deciding (``limited``), max_rework_rounds caps it."""
        if limited and run.rework_rounds >= settings.max_rework_rounds:
            verifier.status = NodeStatus.FAILED
            verifier.review = f"返工 {run.rework_rounds} 轮后验证仍未通过：{verdict.fix or verdict.reason}"
            self.emit(run.id, "node.failed", node_id=verifier.id, error=verifier.review)
            return
        run.rework_rounds += 1
        targets = {item for item in (targets or verifier.verifies) if item in verifier.verifies
                   and any(node.id == item for node in run.plan.nodes)}
        reopened = targets | quality.descendants(run, targets) | {verifier.id}
        asked = f"\n负责人要求：{note}" if note else ""
        for node in run.plan.nodes:
            if node.id not in reopened:
                continue
            node.generation += 1
            task = self.node_tasks.get((run.id, node.id))
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
            node.status, node.failed_attempts = NodeStatus.PENDING, 0
            node.feedback = (f"{quality.REWORK_PREFIX}{verifier.title}）发现问题：{verdict.fix or verdict.reason}{asked}"
                             if node.id in targets else None)
        self.emit(run.id, "run.rework", round=run.rework_rounds, verifier=verifier.id,
                  reopened=sorted(reopened), problems=verdict.fix or verdict.reason)











    async def ask_lead(self, run_id: str, prompt: str) -> str:
        """Ask the task's lead (supervisor.py); its conversation is saved with the task. One
        message at a time per task, so the conversation stays in order."""
        async with self.lead_locks.setdefault(run_id, asyncio.Lock()):
            run = self.db.get_run(run_id)
            previous = run.supervisor.model_id if run.supervisor else None

            async def charged(model_id, call):
                return await self._charged(run, None, model_id, call)

            text, session = await supervisor.ask(self.providers, list(self.model_lookup.values()),
                                                 self._configured(run), run, prompt, charged)
            async with self.locks.setdefault(run_id, asyncio.Lock()):
                latest = self.db.get_run(run_id)
                latest.supervisor = session
                self._save(latest)
            if session.model_id != previous:
                self.emit(run_id, "run.lead_changed", model_id=session.model_id, previous_model_id=previous)
            return text

    async def _complete(self, run, node, model_id, prompt, workspace, **kwargs):
        # Read fresh, so allowing the network takes effect from the task's next model call.
        latest = self.db.get_run(run.id)
        if latest and latest.allow_network and not kwargs.get("repair"):
            kwargs["network"] = True
        return await self._charged(run, node, model_id, lambda budget: self.providers.complete(
            model_id, prompt, workspace, spend_ok=budget, **kwargs))

    async def _charged(self, run, node, model_id, call):
        """Run ``call(budget)``, a model call, against the task's budget. Persist each received
        usage report, even if a later round fails or is cancelled."""
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
            result = await call(budget)
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
    def _final_result(run: ExecutionRun, completed_only: bool = False):
        """Results of the graph's end nodes, which nothing else depends on, in plan order;
        ``completed_only`` takes the furthest completed steps instead."""
        nodes = [node for node in run.plan.nodes if node.status == NodeStatus.SUCCEEDED] if completed_only else run.plan.nodes
        required = {dep for node in nodes for dep in node.dependencies}
        sinks = [node for node in nodes if node.id not in required]
        if completed_only and not sinks:
            return None
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

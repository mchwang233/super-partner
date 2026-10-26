from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from . import (
    activity,
    briefing,
    contracts,
    learning,
    outline,
    progress,
    quality,
    retrospect,
    scopes,
    supervisor,
    team,
)
from . import (
    conversation as memory,
)
from .budget import BudgetExceeded, RequestBudget, StepLimitExceeded, token_cost
from .cli_channels import CLI_SPECS
from .config import settings
from .db import Database
from .diagnosing import Diagnosing
from .engineer import Engineering
from .leading import Leading
from .learning import Learning
from .models import Diagnosis, Event, ExecutionRun, NodeAttempt, NodeStatus, Outcome, RunStatus
from .platform_repair import PlatformRepairs
from .preflight import Preflighting
from .projects import git_snapshot
from .providers import TOOL_LIMIT_REQUIREMENTS, Conversation, ProviderRegistry
from .quota import ModelUnavailable, failure_kind
from .recovery import retry_model, turns_for
from .refining import PREFIX, Refining
from .review import Reviewing
from .routing import (
    NoEligibleModelError,
    can_act,
    eligible_for_node,
    node_cost,
    route_node,
    spread_to,
)
from .self_repair import SelfRepair

# How a model call that ran out of turns reads in an attempt's reason (providers.TURN_LIMIT_MARK).
TURN_LIMIT_HINT = "轮工具调用后仍未完成"

# A membership CLI cannot count its tool calls, so a contract step it runs is bounded by time
# instead: this many minutes per declared tool call, checked with its progress checks.
CLI_MINUTES_PER_TOOL_CALL = 1.5

# Diagnosis report kept with a step that cannot be done.
MAX_UNABLE_REPORT = 1500


def trace_path(run_id: str, node_id: str, attempt: int) -> Path:
    """Where an attempt's full tool trace goes: under the data folder, which steps cannot read."""
    return Path(settings.data_dir).resolve() / "traces" / run_id / f"{node_id}-{attempt}.jsonl"


class Executor(Reviewing, Leading, Diagnosing, SelfRepair, Engineering, Refining, Preflighting, Learning):
    """Runs a task's steps: scheduling, attempts, retries and step outcomes. Reviewing
    (review.py), the lead's decisions (leading.py), diagnosing (diagnosing.py), self-repair
    (self_repair.py), the platform engineer (engineer.py) and splitting major parts into tasks
    (refining.py) are mixed in."""

    def __init__(self, db: Database, providers: ProviderRegistry, model_lookup: dict):
        self.db = db
        self.providers = providers
        self.model_lookup = model_lookup
        self.tasks: dict[str, asyncio.Task] = {}
        self.learning_tasks: dict[str, asyncio.Task] = {}
        # Naming a finished task's problems (retrospect.name_problems), one call each.
        self.naming_tasks: dict[str, asyncio.Task] = {}
        self.closing = False
        self.diagnosing: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        # One message at a time in each task's conversation with its lead.
        self.lead_locks: dict[str, asyncio.Lock] = {}
        # The user's messages to each task's lead, answered in the background.
        self.lead_tasks: dict[str, asyncio.Task] = {}
        # Steps working in one folder at once (scopes.py).
        self.folder_locks: dict[str, scopes.FolderLock] = {}
        self.node_tasks: dict[tuple[str, str], asyncio.Task] = {}
        # Tool calls of each node's attempt in progress, written onto the attempt when it ends.
        self.tool_logs: dict[tuple[str, str], list[dict]] = {}
        # Where each running attempt writes its full tool trace (NodeAttempt.trace).
        self.tool_traces: dict[tuple[str, str], Path] = {}
        self.work_results: dict[tuple[str, str], object] = {}
        # Each step's conversation with its API model, continued by its next attempt.
        self.conversations: dict[tuple[str, str], Conversation] = {}
        self.reservations: dict[str, dict] = {}
        self.budget_events: dict[str, asyncio.Event] = {}
        # Shared environment repairs must not overwrite one another across projects.
        self.repair_lock = asyncio.Lock()
        self.platform = PlatformRepairs(settings.platform_root, settings.data_dir, settings.api_base_url,
                                        settings.platform_self_repair,
                                        self.db.connection.execute("PRAGMA database_list").fetchone()[2])
        self.platform_pending = None
        self.maintenance_task: Optional[asyncio.Task] = None
        # The platform engineer's job at work, if any (engineer.py).
        self.engineer_task: Optional[asyncio.Task] = None
        self.engineer_job_id: Optional[str] = None
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
        self.skip_cancelled_learning(run_id)
        for task in {self.tasks.get(run_id), self.diagnosing.get(run_id)} - {None}:
            if not task.done():
                task.cancel()

    async def shutdown(self) -> None:
        self.closing = True
        tasks = set(self.tasks.values()) | set(self.diagnosing.values())
        tasks |= set(self.learning_tasks.values()) | set(self.naming_tasks.values())
        tasks |= {task for task in (self.maintenance_task, self.engineer_task) if task}
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def emit(self, run_id: str, kind: str, **data) -> None:
        self.db.add_event(Event(run_id=run_id, kind=kind, data=data))
        if kind in retrospect.END_EVENTS:
            # The task has ended: look back on it while its records are fresh.
            retrospect.record(self.db, run_id)
            self._name_problems(run_id)
            self.finish_learning(run_id)

    def _name_problems(self, run_id: str) -> None:
        """Name the problems of a task that has just ended by what they are about, in the background."""
        if not settings.problem_naming or self.closing:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        run = self.db.get_run(run_id)
        if not run or ((current := self.naming_tasks.get(run_id)) and not current.done()):
            return

        async def complete(model_id: str, prompt: str):
            return await self.providers.complete(model_id, prompt, allow_api_fallback=False, effort="low")

        self.naming_tasks[run_id] = loop.create_task(retrospect.name_problems(
            self.db, complete, list(self.model_lookup.values()), self._configured(run), run_id))

    async def execute(self, run_id: str) -> None:
        run = self.db.get_run(run_id)
        if not run or run.status != RunStatus.QUEUED:
            return
        run.status = RunStatus.RUNNING
        # Nodes left RUNNING by an interrupted process never finished; run them again.
        for node in run.plan.nodes:
            if node.status == NodeStatus.RUNNING:
                node.status = NodeStatus.PENDING
        for part in run.plan.parts:
            if part.status == "refining":
                part.status = "planned"
        self._save(run)
        self.emit(run.id, "run.started")
        if run.project_dir:
            self.emit(
                run.id, "workspace.snapshot", project_dir=run.project_dir,
                **await asyncio.to_thread(git_snapshot, Path(run.project_dir)),
            )
        if not await self._preflight(run_id):
            return  # paused: something the task needs is missing (preflight.py)
        run = self.db.get_run(run_id)
        if run and run.plan.employees:
            team.refresh_skills(run.plan)
            self._save(run)
        running: dict[asyncio.Task, str] = {}
        try:
            while True:
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
                unable = [node for node in run.plan.nodes if node.status in {NodeStatus.UNABLE, NodeStatus.BLOCKED}]
                if unable:
                    self._end_incomplete(run, unable)
                    return
                if unsplit := [part.title for part in run.plan.parts if not part.parent_id and part.status != "ready"]:
                    raise RuntimeError("这些大项还没细化成任务：" + "、".join(unsplit))
                if any(node.status != NodeStatus.SUCCEEDED for node in run.plan.nodes):
                    raise RuntimeError("Task graph is blocked by failed or cyclic dependencies")
                # The lead checks the whole result first; steps it reworks or adds run before the end.
                if await self._final_review(run_id):
                    continue
                run = self.db.get_run(run_id)
                run.status = RunStatus.SUCCEEDED
                run.final_result = self._final_result(run)
                self._save(run)
                self.emit(run.id, "run.succeeded", actual_cost_usd=run.actual_cost_usd)
                self._forget(run_id)
                return
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
        """Start splitting major parts not split yet, and ready nodes up to the parallel limit;
        return True if budget held any back."""
        self._split_parts(run, running)
        in_flight = set(running.values())
        completed = {node.id for node in run.plan.nodes if node.status == NodeStatus.SUCCEEDED}
        ready = [
            node for node in run.plan.nodes
            if node.status == NodeStatus.PENDING and node.id not in in_flight
            and set(node.dependencies) <= completed and not outline.waits_on_unsplit(run.plan, node)
        ]
        reserved = run.actual_cost_usd + sum(
            node.estimated_cost_usd for node in run.plan.nodes if node.id in in_flight
        )
        held_back = False
        provider_load = Counter(self.model_lookup[n.model_id].provider for n in run.plan.nodes
                                if n.id in in_flight and n.model_id in self.model_lookup)
        for node in ready:
            if node.status != NodeStatus.PENDING or not self._admit_tool_channel(run, node, reserved):
                continue
            model = self.model_lookup.get(node.model_id)
            if run.plan.employees:
                provider_cap = settings.provider_limits.get(model.provider, {}).get("concurrency", 0) if model else 0
                # An employee is a role, not a worker: its independent tasks run at the same time,
                # each in a session of its own; only a provider's capacity holds them back.
                if provider_cap and provider_load[model.provider] >= provider_cap:
                    continue
            # Splits at work do not count: they are the lead's conversations, not steps.
            limit = settings.max_parallel_nodes
            if limit and sum(not item.startswith(PREFIX) for item in running.values()) >= limit:
                break
            if node.estimated_cost_usd and reserved + node.estimated_cost_usd > run.budget_usd:
                held_back = True
                continue
            reserved += node.estimated_cost_usd
            if model:
                provider_load[model.provider] += 1
            running[asyncio.create_task(self._execute_node(run.id, node.id))] = node.id
        return held_back

    @staticmethod
    def _contract_limit_reason(node):
        reason = progress.limit_reason(node)
        if not reason and node.artifact_contract and node.contract_tool_calls >= node.artifact_contract.max_turns:
            reason = f"达到声明工具调用上限 {node.artifact_contract.max_turns}，停止工作包；保留产物与日志供诊断。"
        return reason

    def _admit_tool_channel(self, run, node, reserved=None, previous_model_id=None) -> bool:
        """Fail closed before starting work; replacements use ordinary routing and permissions."""
        limits = team.governors(run.plan, node)
        try:
            for upstream in run.plan.nodes:
                if upstream.id in node.dependencies:
                    contracts.accepted(run, upstream)
        except contracts.ArtifactBlocked as exc:
            self._stop_contract(run, node, str(exc), started=False, code="upstream_not_accepted",
                                affected_dependencies=sorted(quality.descendants(run, {node.id})))
            self._save(run)
            return False
        if not limits:
            return True
        configured = self._configured(run)
        eligible = {model.id for model in eligible_for_node(
            node, list(self.model_lookup.values()), configured,
            any(item.kind == "image" for item in run.attachments))
            if model.scores.score_for(node.capability) >= node.quality_threshold}
        if run.plan.employees:
            employee = team.member(run.plan, node.employee_id)
            if not employee or node.capability not in employee.capabilities:
                eligible.clear()
        if reserved is None:
            reserved = run.actual_cost_usd + sum(item.estimated_cost_usd for item in run.plan.nodes
                                                if item.status == NodeStatus.RUNNING and item.id != node.id)
        step_left = min(n.artifact_contract.max_cost_usd - n.contract_cost_usd for n in limits)
        declare = getattr(self.providers, "tool_limit_capabilities", None)
        channels, candidates = [], []
        for model in self.model_lookup.values():
            declared = declare(model.id) if callable(declare) else {}
            # Membership CLIs cannot count tool calls; they are bounded by time instead (_timed_watch).
            compatible = can_act(model) and (model.id.startswith("cli:") or all(
                declared.get(k) == v for k, v in TOOL_LIMIT_REQUIREMENTS.items()))
            cost = node_cost(node, model) if model.id in eligible else None
            affordable = cost is not None and reserved + cost <= run.budget_usd and cost <= step_left
            channels.append({"model_id": model.id, "declared_capabilities": declared,
                             "authorized": model.id in configured, "eligible": model.id in eligible,
                             "compatible": compatible, "affordable": affordable})
            retained = model.id == node.model_id and previous_model_id in {None, model.id}
            if compatible and model.id in eligible and (retained or affordable):
                candidates.append(model)
        reason = next((why for n in limits if (why := self._contract_limit_reason(n))), None)
        if candidates and not reason:
            if node.model_id not in {model.id for model in candidates}:
                previous = node.model_id
                route_node(node, candidates, configured, any(item.kind == "image" for item in run.attachments))
                self._save(run)
                self.emit(run.id, "node.model_switched", node_id=node.id, model_id=previous,
                          next_model_id=node.model_id, reason="tool_limit_compatibility")
            return True
        self._stop_contract(
            run, node, reason or "没有已授权且满足能力、预算及工具调用硬上限要求的执行通道。",
            Outcome.STOPPED if reason else Outcome.BLOCKED, started=False,
            code="contract_limit_exhausted" if reason else "tool_limit_incompatible",
            required_capabilities=dict(TOOL_LIMIT_REQUIREMENTS), channels=channels,
            actual_counts={n.id: {"requests": n.contract_turns, "tool_calls": n.contract_tool_calls,
                                  "cost_usd": n.contract_cost_usd, "limit": n.artifact_contract.max_turns}
                           for n in limits},
            affected_dependencies=sorted(quality.descendants(run, {node.id})),
        )
        self._save(run)
        return False

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
            if (any(n.status != NodeStatus.SUCCEEDED for n in run.plan.nodes if n.id in node.dependencies)
                    or outline.waits_on_unsplit(run.plan, node)):
                return
            if self.platform_pending:
                self._hold_for_platform(run)
                return
            admitted_model_id = node.model_id
            if not self._out_of_quota(node.model_id):
                try:
                    self.refresh_model(run, node)
                except NoEligibleModelError as exc:
                    if team.governors(run.plan, node):
                        if not self._admit_tool_channel(run, node):
                            return
                    else:
                        run.status, run.error = RunStatus.PAUSED, str(exc)
                        self._save(run)
                        self.emit(run_id, "approval.required", reason="model_unavailable", node_id=node.id)
                        return
                self._spread(run, node)
            # Refresh/spreading and concurrent work may have changed the final channel or limits.
            if not self._admit_tool_channel(run, node, previous_model_id=admitted_model_id):
                return
            if (not self._out_of_quota(node.model_id) and node.estimated_cost_usd
                    and run.actual_cost_usd + node.estimated_cost_usd > run.budget_usd):
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
                self._conversation(run, node),
            )
            work_accounted = result.accounted_cost_usd
            latest = self.db.get_run(run_id)
            stale = latest and next(n for n in latest.plan.nodes if n.id == node_id).generation != generation
            if run.plan.employees and result.text.lstrip().startswith(team.DELEGATE):
                try:
                    payload = team.parse_delegation(result.text)
                    await self._delegate_result(run_id, node_id, payload, result, inputs, changed, cost, generation)
                    return
                except (ValueError, TypeError) as exc:
                    verdict = quality.Verdict(False, f"委派请求未通过校验：{exc}", fix=str(exc))
            else:
                verdict = quality.Verdict(False, "上游已返工，本次结果失效。") if stale else await self._check(run, node, result.text, changed, workspace)
            if result.turn_limit and verdict.passed and not verdict.reviewer_model_id:
                # Nobody checked it, and a summary made at the turn limit says the work is unfinished.
                verdict = quality.Verdict(False, f"用完了工具调用轮次，{TURN_LIMIT_HINT}：只交了进度总结。")
            await self._finish(run_id, node_id, result, verdict, inputs, changed, cost, None, generation=generation)
        except asyncio.CancelledError as exc:
            exc.accounted_cost_usd = getattr(exc, "accounted_cost_usd", 0.0) + work_accounted
            latest = self.db.get_run(run_id)
            if latest:
                current = next(n for n in latest.plan.nodes if n.id == node_id)
                if len(current.attempts) == len(node.attempts):
                    current.attempts.append(NodeAttempt(model_id=node.model_id, outcome=Outcome.SUPERSEDED if current.generation != generation else Outcome.CANCELLED,
                                                        interrupted=latest.status != RunStatus.CANCELLED,
                                                        reason="上游返工或任务取消，调用已停止。", cost_usd=getattr(exc, "accounted_cost_usd", 0.0),
                                                        **self._take_log(run_id, node_id, getattr(exc, "usage", None))))
                self._save(latest)
                self._release_log(run_id, node_id)
                if current.generation != generation:
                    return
            raise
        except BudgetExceeded as exc:
            exc.accounted_cost_usd = getattr(exc, "accounted_cost_usd", 0.0) + work_accounted
            await self._finish(run_id, node_id, None, quality.Verdict(False, str(exc)), inputs, [],
                               getattr(exc, "accounted_cost_usd", 0.0), "budget", generation=generation,
                               accounted=getattr(exc, "accounted_cost_usd", 0.0), usage=getattr(exc, "usage", None))
        except (contracts.ArtifactBlocked, StepLimitExceeded) as exc:
            spent = getattr(exc, "accounted_cost_usd", 0.0) + work_accounted
            await self._finish(run_id, node_id, None, quality.Verdict(False, str(exc)), inputs, [], spent,
                               Outcome.STOPPED if isinstance(exc, StepLimitExceeded) else Outcome.BLOCKED,
                               generation=generation, accounted=spent, usage=getattr(exc, "usage", None))
        except ModelUnavailable as exc:
            await self._switch_model(run_id, node_id, exc)
            return
        except Exception as exc:
            exc.accounted_cost_usd = getattr(exc, "accounted_cost_usd", 0.0) + work_accounted
            await self._finish(run_id, node_id, None, quality.Verdict(False, str(exc)), inputs, [],
                               getattr(exc, "accounted_cost_usd", 0.0), "error", generation=generation,
                               accounted=getattr(exc, "accounted_cost_usd", 0.0), usage=getattr(exc, "usage", None))
        finally:
            self.node_tasks.pop((run_id, node_id), None)

    async def _delegate_result(self, run_id, node_id, payload, result, inputs, changed, cost, generation):
        """Commit the child graph and parent's yielded attempt in one saved run."""
        async with self.locks.setdefault(run_id, asyncio.Lock()):
            run = self.db.get_run(run_id)
            if not run:
                return
            node = next(n for n in run.plan.nodes if n.id == node_id)
            stale = node.generation != generation
            stopped = (run.status in {RunStatus.CANCELLED, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.INCOMPLETE}
                       or node.status != NodeStatus.RUNNING)
            outcome = Outcome.SUPERSEDED if stale else Outcome.CANCELLED if stopped else Outcome.DELEGATED
            children = []
            if not stale and not stopped:
                for governor in team.governors(run.plan, node):
                    if reason := self._contract_limit_reason(governor):
                        raise StepLimitExceeded(reason)
                existing = [n for n in run.plan.nodes if n.delegation_parent_id == node.id
                            and n.delegation_request_id == payload.get("request_id")]
                if existing and all(n.status == NodeStatus.SUCCEEDED for n in existing):
                    raise ValueError("该委派已完成，请使用已有子任务结果继续原任务，不要再次委派。")
                children = team.delegate(run, node, payload, list(self.model_lookup.values()), self._configured(run),
                                         any(item.kind == "image" for item in run.attachments))
                node = next(n for n in run.plan.nodes if n.id == node_id)
                node.status = NodeStatus.PENDING
                node.review_passed, node.feedback = None, None
                node.review = "等待委派任务验收：" + "、".join(n.title for n in children)
                node.result, node.inputs = result.text, inputs
                node.changed_files = list(dict.fromkeys([*node.changed_files, *changed]))
            node.attempts.append(NodeAttempt(model_id=result.used_model_id or node.model_id, outcome=outcome,
                                            reason=node.review if children else "任务已停止或上游变化，未发布子任务。",
                                            cost_usd=cost, **self._take_log(run_id, node_id, result)))
            run.actual_cost_usd = round(run.actual_cost_usd + max(0, cost - result.accounted_cost_usd), 6)
            from .routing import reestimate
            reestimate(run)
            self._save(run)
            self._release_log(run_id, node_id)
            if children:
                self.emit(run_id, "node.delegated", node_id=node_id, employee_id=node.employee_id,
                          children=[{"id": n.id, "employee_id": n.employee_id, "title": n.title} for n in children],
                          reason=str(payload.get("reason") or "")[:2000])

    def _conversation(self, run: ExecutionRun, node) -> Conversation:
        """Restore within the same task generation/dependency revision; model switches receive
        an archived handoff. Long contexts are compressed by the provider, never discarded."""
        key = (run.id, node.id)
        held, last = self.conversations.get(key), node.attempts[-1] if node.attempts else None
        if held is None and (saved := self.db.conversation(*key)):
            held = Conversation(model_id=saved.get("model_id"), state=saved.get("state"), fence=saved.get("fence"))
        fence = {"generation": node.generation, "project": run.project_dir,
                 "request": hashlib.sha256(json.dumps([run.prompt, node.instructions, node.acceptance_criteria,
                                                       node.files, node.reads, node.check], ensure_ascii=False).encode()).hexdigest(),
                 "dependencies": {item.id: [item.generation, item.attempts[-1].finished_at.isoformat() if item.attempts else None]
                                  for item in run.plan.nodes if item.id in node.dependencies}}

        def checkpoint(body):
            latest = self.db.get_run(run.id)
            current = next((item for item in latest.plan.nodes if item.id == node.id), None) if latest else None
            if current and current.generation == node.generation:
                self.db.save_conversation(*key, body)

        def bind(conversation):
            conversation.fence = fence
            conversation.checkpoint = checkpoint
            conversation.archive = lambda body: self.db.archive_conversation(*key, body)
            conversation.recall = lambda archive_id: self.db.conversation_archive(*key, archive_id)
            return conversation

        upstream = [item.attempts[-1].finished_at for item in run.plan.nodes
                    if item.id in node.dependencies and item.attempts]
        delegated_resume = False
        if held and held.fence and last and last.outcome == Outcome.DELEGATED:
            previous = held.fence
            child_ids = {n.id for n in run.plan.nodes if n.delegation_parent_id == node.id}
            delegated_resume = (all(previous.get(k) == fence[k] for k in ("generation", "project", "request"))
                                and all(fence["dependencies"].get(k) == v for k, v in previous.get("dependencies", {}).items())
                                and set(fence["dependencies"]) - set(previous.get("dependencies", {})) <= child_ids)
        valid = held and (held.fence is None or held.fence == fence or delegated_resume)
        if (valid and held.model_id == node.model_id
                and (last is None or (last.outcome != Outcome.SUPERSEDED
                     and (last.outcome != Outcome.CANCELLED or last.interrupted)
                     and progress.STOPPED not in (last.reason or "")
                     and (delegated_resume or not any(finished > last.finished_at for finished in upstream))))):
            bind(held)
            held.followup = quality.continue_prompt(node, run) if last else (
                "The service restarted during this step. Continue from its saved state. "
                "For any interrupted tool, inspect actual effects before repeating it.\n" + node.instructions)
            self.conversations[key] = held
            return held
        fresh = bind(Conversation())
        if valid and held.state:
            bind(held)
            fresh.handoff = memory.handoff(held)
        self.conversations[key] = fresh
        return self.conversations[key]

    @staticmethod
    def _with_files(run: ExecutionRun, node, workspace: Path, prompt: str) -> str:
        """``prompt`` with the full text of the files the step needs. A work package the planner
        scoped gets its own files and nothing else; any other step gets the list of the project's
        files and the files it most likely needs (briefing.py)."""
        if node.files or node.reads:
            attached = briefing.attached_files(workspace, node.files + node.reads, briefing.SCOPED_ATTACH_CHARS,
                                               briefing.SCOPED_ATTACH_CHARS)
            return "\n\n".join(part for part in (prompt, attached) if part)
        files = briefing.render(workspace, None)
        attached = briefing.attached_files(workspace, briefing.likely_files(workspace, node, run))
        return "\n\n".join(part for part in (prompt, files, attached) if part)

    def _forget(self, run_id: str) -> None:
        for key in [key for key in self.conversations if key[0] == run_id]:
            del self.conversations[key]

    def refresh_model(self, run, node):
        """Revalidate a stored selection immediately before confirming or executing it."""
        if run.plan.employees:
            team.route(node, run.plan, list(self.model_lookup.values()), self._configured(run),
                       any(item.kind == "image" for item in run.attachments))
            return
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
        elif not node.attempts and node.routing_note and hasattr(self.providers, "configured_models"):
            from .routing_feedback import collect
            previous = node.model_id
            route_node(node, list(self.model_lookup.values()), configured,
                       any(item.kind == "image" for item in run.attachments), feedback=collect(self.db))
            if node.model_id != previous:
                self.emit(run.id, "node.model_switched", node_id=node.id, model_id=previous,
                          next_model_id=node.model_id, reason="observed_delivery_cost")

    def _take_log(self, run_id: str, node_id: str, result=None) -> dict:
        """What an attempt did, for its NodeAttempt, however it ended (finished, cancelled, its
        model unavailable): its tool calls, its turns (from the model's usage, else the calls),
        and its full trace if one was written."""
        # Snapshot only. A failed model validation or DB write must leave the evidence available.
        key = (run_id, node_id)
        log = [dict(call) for call in self.tool_logs.get(key, [])]
        trace = self.tool_traces.get(key)
        answer = result if hasattr(result, "text") else self.work_results.get(key)
        turns = getattr(result, "turns", 0) or getattr(answer, "turns", 0) or max((call["turn"] for call in log), default=0)
        latest = self.db.get_run(run_id)
        node = next((item for item in latest.plan.nodes if item.id == node_id), None) if latest else None
        elapsed = max(0, (datetime.now(timezone.utc) - node.started_at).total_seconds()) if node and node.started_at else None
        return {"tool_calls": log, "turns": turns, "trace": str(trace) if trace and trace.is_file() else None,
                "duration_seconds": elapsed, "result": getattr(answer, "text", None),
                "result_source": "provider" if answer is not None else None}

    def _release_log(self, run_id: str, node_id: str) -> None:
        """Release an attempt's evidence only after its record was saved successfully."""
        key = (run_id, node_id)
        self.tool_logs.pop(key, None)
        self.tool_traces.pop(key, None)
        self.work_results.pop(key, None)

    def _spread(self, run, node) -> None:
        """Keep one API provider from carrying all of the task's running steps, so that its
        outage or rate limit does not stop them all: past ``max_steps_per_provider`` the node
        goes to another provider's model when one fits (routing.spread_to)."""
        if run.plan.employees:
            return  # the lead owns employee model assignments
        cap = settings.max_steps_per_provider
        provider = (node.model_id or "").split(":", 1)[0]
        if not cap or not node.model_id or provider in ("cli", "mock"):
            return
        running = Counter((item.model_id or "").split(":", 1)[0] for item in run.plan.nodes
                          if item.status == NodeStatus.RUNNING and item.id != node.id)
        if running[provider] < cap:
            return
        busy = {name for name, count in running.items() if count >= cap}
        other = spread_to(node, list(self.model_lookup.values()), self._configured(run), busy,
                          any(item.kind == "image" for item in run.attachments))
        if other is None:
            return
        previous, node.model_id = node.model_id, other.id
        node.estimated_cost_usd = round(node_cost(node, other), 6)
        self.emit(run.id, "node.model_switched", node_id=node.id, model_id=previous,
                  next_model_id=other.id, reason="spread_across_providers")

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
                                            cost_usd=getattr(exc, "accounted_cost_usd", 0.0),
                                            **self._take_log(run_id, node_id, getattr(exc, "usage", None))))
            if node.artifact_contract:
                node.contract_turns = max(node.contract_turns, sum(a.turns for a in node.attempts))
                node.contract_cost_usd = max(node.contract_cost_usd, sum(a.cost_usd for a in node.attempts))
            if reason := self._contract_limit_reason(node):
                node.attempts[-1].outcome = Outcome.STOPPED
                node.attempts[-1].reason = reason + "\n" + str(exc)
                self._stop_contract(run, node, node.attempts[-1].reason, Outcome.STOPPED)
            else:
                self._route_after_unavailable(run, node, exc)
            self._save(run)
            self._release_log(run_id, node_id)

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

    async def _work(self, run: ExecutionRun, node, model_id: str, prompt: str, fallback: bool,
                    conversation: Optional[Conversation] = None):
        """Have ``model_id`` do ``node`` in its working folder: the user's project or the
        task's scratch folder. Returns (result, changed files, cost, working folder).
        ``conversation`` continues the step's last attempt when it holds a followup."""
        async with self.locks.setdefault(run.id, asyncio.Lock()):
            latest = self.db.get_run(run.id)
            learning.attach(self.db, latest)
            for item in latest.improvement_uses:
                if learning.matches(item, learning.node_text(node), node.capability) and node.id not in item["node_ids"]:
                    item["node_ids"].append(node.id)
            run.improvement_uses = latest.improvement_uses
            self._save(latest)
        if lessons := learning.instruction(run.improvement_uses, node):
            prompt += "\n\n" + lessons
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
        scope = scopes.of(node)
        async with contextlib.AsyncExitStack() as stack:
            if uses_files:
                # Steps editing the same folder at once would clobber each other, unless the files
                # they change do not overlap (scopes.py).
                await stack.enter_async_context(self.folder_locks.setdefault(str(workspace), scopes.FolderLock()).hold(scope))
            before = await asyncio.to_thread(quality.file_state, workspace) if uses_files else {}
            if uses_files and before and not (conversation and conversation.followup):
                # A continued conversation has its files already.
                prompt = await asyncio.to_thread(self._with_files, run, node, workspace, prompt)
            self.work_results.pop((run.id, node.id), None)
            log = self.tool_logs[run.id, node.id] = []
            trace = self.tool_traces[run.id, node.id] = trace_path(run.id, node.id, len(node.attempts) + 1)
            for governor in team.governors(run.plan, node):
                if reason := self._contract_limit_reason(governor):
                    raise StepLimitExceeded(reason)
            if node.artifact_contract:
                await asyncio.to_thread(contracts.prepare, run, node)
                async with self.locks.setdefault(run.id, asyncio.Lock()):
                    latest = self.db.get_run(run.id)
                    current = next(item for item in latest.plan.nodes if item.id == node.id)
                    for field in ("contract_generation", "artifact_inputs", "artifact_baseline", "artifact_evidence"):
                        setattr(current, field, getattr(node, field))
                    self._save(latest)
            with activity.recording(log):
                result = await self._complete(
                    run, node, model_id, prompt, workspace, allow_api_fallback=fallback and not in_project,
                    attachments=run.attachments, act=act, max_turns=turns_for(node), tool_log=log, tool_trace=trace,
                    watch=self._timed_watch(run, node, model_id),
                    effort=settings.reasoning_by_difficulty.get(node.difficulty),
                    **({"conversation": conversation} if conversation else {}),
                    **({"overrun": overrun} if (overrun := self._overrun_check(run, node, model_id)) else {}),
                )
            self.work_results[run.id, node.id] = result
            try:
                after = await asyncio.to_thread(quality.file_state, workspace) if uses_files else {}
            except (Exception, asyncio.CancelledError) as exc:
                exc.accounted_cost_usd = result.accounted_cost_usd
                raise
        # The node's own model falls back to its routed estimate; a takeover has none.
        cost = self._actual_cost(result, node, None if model_id == node.model_id else model_id)
        return result, scope.own(quality.changed_files(before, after)), cost, workspace


    def _timed_watch(self, run, node, model_id: str):
        """The progress watch of an attempt; for a contract step on a membership CLI, which cannot
        count its tool calls, it also stops the attempt past the time its declared calls allow."""
        watch = self._progress_watch(run, node, model_id)
        limits = team.governors(run.plan, node)
        minutes = (min(n.artifact_contract.max_turns for n in limits) * CLI_MINUTES_PER_TOOL_CALL
                   if model_id.startswith("cli:") and limits else None)
        started = time.monotonic()

        async def timed(evidence: str) -> Optional[str]:
            if minutes is not None and (time.monotonic() - started) / 60 >= minutes:
                return f"达到用时上限 {minutes:.0f} 分钟（会员 CLI 无法统计工具调用次数，按用时约束）。"
            with activity.paused():  # the checker's and the lead's calls are not the step's work
                return await watch(evidence)

        return timed

    async def _finish(self, run_id: str, node_id: str, result, verdict: quality.Verdict, inputs, changed, cost: float, outcome: Optional[str],
                      generation: Optional[int] = None, accounted: float = 0.0, usage=None) -> None:
        """Record an attempt, then accept the node, retry it, or send verified work back."""
        lock = self.locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            run = self.db.get_run(run_id)
            if not run:
                return
            node = next(item for item in run.plan.nodes if item.id == node_id)
            stale = generation is not None and node.generation != generation
            returned = result or self.work_results.get((run_id, node_id))
            logged = self._take_log(run_id, node_id, result or usage)
            if node.artifact_contract and not stale:
                node.contract_turns = max(node.contract_turns, sum(a.turns for a in node.attempts) + logged["turns"])
                node.contract_cost_usd = max(node.contract_cost_usd, sum(a.cost_usd for a in node.attempts)
                                             + cost + verdict.cost_usd)
                node.artifact_evidence = verdict.artifact_evidence
                if verdict.passed:
                    try:
                        contracts.accepted(run, node)
                    except contracts.ArtifactBlocked as exc:
                        verdict.passed, verdict.reason = False, str(exc)
                if not verdict.passed and (reason := self._contract_limit_reason(node)):
                    outcome, verdict.reason = Outcome.STOPPED, reason + "\n" + verdict.reason
            # A verification that ran out of turns has not finished, which says nothing against
            # the work it verifies: it is retried itself rather than sending that work back.
            unfinished = bool(getattr(result, "turn_limit", False)) and TURN_LIMIT_HINT in (verdict.reason or "")
            outcome = outcome or (Outcome.BLOCKED if verdict.blocked else Outcome.UNABLE if verdict.unable else Outcome.UNVERIFIED if not verdict.verified
                                  else Outcome.PASSED if verdict.passed
                                  else Outcome.VERIFY_FAILED if node.verifies and not unfinished
                                  else Outcome.REJECTED)
            if stale:
                outcome = Outcome.SUPERSEDED
            spent = round(cost + verdict.cost_usd, 6)
            node.attempts.append(NodeAttempt(
                model_id=(returned.used_model_id if returned else None) or node.model_id, outcome=outcome,
                reason=verdict.reason, reviewer_model_id=verdict.reviewer_model_id, cost_usd=spent,
                turn_limit=bool(getattr(returned, "turn_limit", False)) or TURN_LIMIT_HINT in (verdict.reason or ""),
                **logged,
            ))
            accounted += getattr(result, "accounted_cost_usd", 0.0) + verdict.accounted_cost_usd
            run.actual_cost_usd = round(run.actual_cost_usd + max(0.0, spent - accounted), 6)
            if stale:
                self._save(run)
                self._release_log(run_id, node_id)
                self.emit(run_id, "node.superseded", node_id=node.id)
                return
            checked = verdict.verified and settings.acceptance and (settings.review_nodes or bool(node.verifies))
            node.inputs, node.review_passed, node.review = inputs, verdict.passed if checked else None, verdict.reason
            if logged["result"] is not None:
                node.result, node.summary = logged["result"], verdict.summary
                node.changed_files = list(dict.fromkeys([*node.changed_files, *changed]))
            # At a declared limit, or blocked (missing evidence, a target not reached, a platform
            # rule), the lead decides: more calls, rework upstream, a repair, ask the user, or give up
            # (leading.py). Without a lead it stops as declared.
            limited = self._can_lead() and (outcome == Outcome.BLOCKED or (
                outcome == Outcome.STOPPED and bool(self._contract_limit_reason(node))))
            if limited:
                node.status = NodeStatus.RUNNING
            elif outcome in {Outcome.BLOCKED, Outcome.STOPPED}:
                self._stop_contract(run, node, verdict.reason, outcome)
            elif outcome == Outcome.BUDGET:
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
            diagnose = settings.auto_diagnose and not limited and (
                node.status == NodeStatus.RUNNING or (outcome == Outcome.VERIFY_FAILED and node.status == NodeStatus.FAILED)
            )
            # A failed attempt held for recovery goes to the task's lead (leading.py).
            lead = node.status == NodeStatus.RUNNING and self._can_lead() and (
                settings.auto_diagnose or limited or outcome == Outcome.VERIFY_FAILED)
            self._save(run)
            self._release_log(run_id, node_id)
        if lead:
            await self._decide(run_id, node_id, verdict, outcome)
        elif diagnose:
            await self._repair(run_id, node_id, verdict, outcome)


    def _stop_contract(self, run, node, reason: str, outcome: str = Outcome.BLOCKED, **details) -> None:
        """Record a deterministic diagnosis, without another model call or automatic recovery."""
        node.status, node.review_passed = NodeStatus.BLOCKED, False
        node.review = node.unable_reason = reason
        if node.artifact_evidence:
            node.artifact_evidence.accepted = False
        if not node.result:
            node.result = f"BLOCKED: {reason}"
        report = (f"{reason}\n累计请求轮次：{node.contract_turns}；工具调用：{node.contract_tool_calls}；累计费用：${node.contract_cost_usd:.6f}。\n"
                  + contracts.prompt(node))
        if node.attempts and node.attempts[-1].trace:
            report += f"\n日志：{node.attempts[-1].trace}"
        run.diagnoses.append(Diagnosis(id=uuid4().hex[:8], trigger="artifact_contract", node_id=node.id,
                                       status="done", scope="task", verdict="blocked", report=report,
                                       finished_at=datetime.now(timezone.utc)))
        for downstream in run.plan.nodes:
            if downstream.id in quality.descendants(run, {node.id}) and downstream.status == NodeStatus.PENDING:
                downstream.status = NodeStatus.BLOCKED
                downstream.review = downstream.unable_reason = f"上游「{node.title}」没有已验收的交付：{reason}"
        self.emit(run.id, "node.blocked", node_id=node.id, reason=reason, outcome=outcome, **details)



    def _retry_or_fail(self, run: ExecutionRun, node, verdict: quality.Verdict, outcome: str) -> None:
        """The fallback without a lead's decision: count the failure, then retry or fail."""
        node.failed_attempts += 1
        problem = verdict.reason + (f"\n应修改：{verdict.fix}" if verdict.fix else "")
        if node.failed_attempts >= settings.max_node_attempts:
            if outcome == Outcome.UNABLE:
                self._mark_unable(run, node, verdict.unable or verdict.reason)
                return
            node.status = NodeStatus.FAILED
            if outcome == Outcome.ERROR and not (node.attempts and node.attempts[-1].result is not None):
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
            # What the platform engineer did for this task since the lead last heard.
            if news := self._engineer_news(run):
                prompt = supervisor.engineer_updates(news) + prompt

            async def charged(model_id, call):
                return await self._charged(run, None, model_id, call)

            text, session = await supervisor.ask(self.providers, list(self.model_lookup.values()),
                                                 self._configured(run), run, prompt, charged)
            async with self.locks.setdefault(run_id, asyncio.Lock()):
                latest = self.db.get_run(run_id)
                latest.supervisor = session
                latest.engineer_seen.update({job.id: job.status for job in news})
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
            model_id, prompt, workspace, spend_ok=budget, **kwargs), work=bool(kwargs.get("tool_trace")))

    async def _charged(self, run, node, model_id, call, *, work=False):
        """Run ``call(budget)``, a model call, against the task's budget. Persist each received
        usage report, even if a later round fails or is cancelled."""
        async def charge(cost):
            if not cost:
                return
            async with self.locks.setdefault(run.id, asyncio.Lock()):
                latest = self.db.get_run(run.id)
                if latest:
                    latest.actual_cost_usd = round(latest.actual_cost_usd + cost, 6)
                    current = next((item for item in latest.plan.nodes if node and item.id == node.id), None)
                    if current:
                        for governor in team.governors(latest.plan, current):
                            governor.contract_cost_usd += cost
                    self._save(latest)

        def remaining():
            latest = self.db.get_run(run.id)
            return latest.budget_usd - latest.actual_cost_usd if latest else float("-inf")

        def current_limits():
            latest = self.db.get_run(run.id)
            step = next(item for item in latest.plan.nodes if item.id == node.id)
            return team.governors(latest.plan, step)

        async def use_turn():
            async with self.locks.setdefault(run.id, asyncio.Lock()):
                latest = self.db.get_run(run.id)
                step = next(item for item in latest.plan.nodes if item.id == node.id)
                governors = team.governors(latest.plan, step)
                for governor in governors:
                    if reason := self._contract_limit_reason(governor):
                        raise StepLimitExceeded(reason)
                for governor in governors:
                    governor.contract_turns += 1
                self._save(latest)

        async def begin_tool():
            # The check and debit share the run lock, including delegated siblings. Never
            # refund an interrupted/failed invocation: its side effects may already exist.
            async with self.locks.setdefault(run.id, asyncio.Lock()):
                latest = self.db.get_run(run.id)
                step = next(item for item in latest.plan.nodes if item.id == node.id)
                governors = team.governors(latest.plan, step)
                for governor in governors:
                    if governor.contract_tool_calls >= governor.artifact_contract.max_turns:
                        raise StepLimitExceeded("达到声明工具调用上限，额外调用未执行。")
                for governor in governors:
                    governor.contract_tool_calls += 1
                self._save(latest)

        limits = team.governors(run.plan, node) if node else []
        # Turns and tool calls are counted for API models only; a membership CLI cannot report
        # them, and is bounded by time instead (_timed_watch). Cost limits apply to both.
        counted = bool(limits) and work and not model_id.startswith("cli:")
        budget = RequestBudget(self.model_lookup, remaining, self.reservations.setdefault(run.id, {}), charge,
                               self.budget_events.setdefault(run.id, asyncio.Event()),
                               step_remaining=(lambda: min(n.artifact_contract.max_cost_usd - n.contract_cost_usd for n in current_limits())) if limits else None,
                               turn_remaining=(lambda: min(n.artifact_contract.max_turns - n.contract_turns for n in current_limits())) if counted else None,
                               use_turn=use_turn if counted else None,
                               scope=(run.id, limits[-1].id) if limits else None, wait_for_step=work)
        budget.begin_tool = begin_tool if counted else None
        budget.tool_remaining = (lambda: min(n.artifact_contract.max_turns - n.contract_tool_calls
                                             for n in current_limits())) if counted else None
        budget.context = {"run_id": run.id, "node_id": node.id if node else None,
                          "purpose": "work" if work else "review_or_control"}
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
        return token_cost(model, result.input_tokens, result.output_tokens,
                          getattr(result, "cached_input_tokens", 0))

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

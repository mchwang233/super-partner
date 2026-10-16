"""Diagnosing failed or stuck steps and acting on the diagnosis (recovery.after_diagnosis
decides what it means). Mixed into executor.Executor."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from . import agent as agent_tools
from . import environment, quality
from .budget import BudgetExceeded
from .config import settings
from .diagnosis import interaction, repair_prompt, user_action
from .diagnosis import parse as parse_diagnosis
from .diagnosis import prompt as diagnosis_prompt
from .models import (
    Diagnosis,
    DiagnosisStatus,
    ExecutionRun,
    NodeAttempt,
    NodeStatus,
    Outcome,
    RunStatus,
)
from .platform_repair import RepairError
from .quota import failure_kind
from .recovery import after_diagnosis
from .routing import NoEligibleModelError, can_act, diagnosers, pick_diagnoser

# Models tried in turn for one repair when the previous one fails.
MAX_DIAGNOSERS = 3
# A membership CLI installing software can take a while.
REPAIR_TIMEOUT = 1800


class Diagnosing:
    """Uses the executor's run state, locks, model calls and step outcomes (_retry_or_fail,
    _mark_unable, _route_after_unavailable) and self-repair (_fix_platform)."""

    async def _repair(self, run_id: str, node_id: str, verdict: quality.Verdict, outcome: str,
                      diagnosis_id: Optional[str] = None, brief: str = "", decision_id: Optional[str] = None) -> None:
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
                    diagnosis_id = self.begin_diagnosis(run, "lead_repair" if decision_id else "auto", node_id)
                    diagnosis = run.diagnoses[-1]
                    diagnosis.decision_id = decision_id
                    if diagnosis_id:
                        self.diagnosing[run_id] = asyncio.current_task()
                    self._save(run)
                    break
            # Another diagnosis (manual, or another node's repair) is running; this one follows it.
            await asyncio.wait([busy])
        if not diagnosis_id:
            return await self._repair_done(run_id, node_id, diagnosis.id, None, None, 0.0, verdict, outcome)
        workspace = Path(run.project_dir) if run.project_dir and node.needs_workspace else settings.workspace_root / run.folder_id
        workspace.mkdir(parents=True, exist_ok=True)
        events = [f"{event.created_at:%H:%M:%S} {event.kind} {event.data}" for event in self.db.events(run_id)]
        cost = 0.0
        try:
            async with self.repair_lock:
                shared = environment.prepare()
                before = await asyncio.to_thread(quality.file_state, shared)
                prompt = repair_prompt(run, node, str(workspace), str(shared), agent_tools.sandbox_ports(),
                                       events, self.db.shared_repairs(limit=5), self.platform.context(), brief)
                if self.platform_pending:
                    text, extra = await self._fix_platform(run, diagnosis, node, "")
                    result = None
                else:
                    result, _model_id, cost = await self._diagnose_with(run, diagnosis, node, prompt, workspace, repair=True)
                    text = result.text
                    if parse_diagnosis(text)[2] == DiagnosisStatus.PLATFORM_REPAIR:
                        try:
                            text, extra = await self._fix_platform(run, diagnosis, node, text)
                        except BaseException as exc:
                            exc.accounted_cost_usd = getattr(exc, "accounted_cost_usd", 0.0) + cost
                            raise
                        cost += extra
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
                    if record.reply_to and current.waiting_diagnosis_id == record.reply_to:
                        current.waiting_diagnosis_id = None
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
        await self._repair_done(run_id, node_id, diagnosis.id, text, None, cost, verdict, outcome, shared_files)

    async def _diagnose_with(self, run: ExecutionRun, diagnosis: Diagnosis, node, prompt: str, workspace: Path,
                             repair: bool, edit: bool = False):
        """Run a diagnosis on its model, or, if that fails (a CLI at its usage limit, say), on
        the next diagnosers in turn. Returns the result and the model that gave it; switches are
        recorded as events, and the last failure is raised."""
        run_id, diagnosis_id = run.id, diagnosis.id
        available = diagnosers(list(self.model_lookup.values()), self._configured(run))
        if edit:
            available = [model for model in available if can_act(model)]
        allowed = {model.id for model in available}
        candidates = ([diagnosis.model_id] * (diagnosis.model_id in allowed) + [
            model.id for model in available
            if model.id != diagnosis.model_id
        ])[:MAX_DIAGNOSERS]
        if not candidates:
            raise RepairError("没有当前可用且具备所需能力的诊断模型。")
        if candidates[0] != diagnosis.model_id:
            latest = self.db.get_run(run_id)
            record = next(d for d in latest.diagnoses if d.id == diagnosis_id)
            record.model_id = candidates[0]
            self._save(latest)
            self.emit(run_id, "run.diagnosis_fallback", diagnosis_id=diagnosis_id, model_id=diagnosis.model_id,
                      next_model_id=candidates[0], error="原诊断模型已不可用或不具备此次修复需要的工具能力。")
        cost = 0.0
        for index, model_id in enumerate(candidates):
            model = self.model_lookup.get(model_id)
            # Repairs act on the machine; an analysis only reads, which CLIs do on their own.
            act = bool((repair or edit) and model and model.channel != "cli" and can_act(model))
            run = self.db.get_run(run_id)
            try:
                result = await self._complete(
                    run, node, model_id, prompt, workspace, cli_timeout=REPAIR_TIMEOUT if repair or edit else 600,
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
                           verdict: quality.Verdict, outcome: str, shared_files: Optional[list[str]] = None,
                           consult: bool = True) -> None:
        """Act on the diagnosis of a failed attempt: a repair the lead asked for goes back to the
        lead (``consult``); otherwise recovery.after_diagnosis decides what to do."""
        handover = False
        async with self.locks.setdefault(run_id, asyncio.Lock()):
            run = self.db.get_run(run_id)
            if not run:
                return
            node = next(item for item in run.plan.nodes if item.id == node_id)
            diagnosis = next(item for item in run.diagnoses if item.id == diagnosis_id)
            if error and diagnosis.reply_to and run.waiting_diagnosis_id == diagnosis.reply_to:
                run.waiting_diagnosis_id = None
            if diagnosis.status == "running":
                diagnosis.status, diagnosis.error = ("failed", error) if error else ("done", None)
                diagnosis.cost_usd, diagnosis.finished_at = round(cost, 6), datetime.now(timezone.utc)
            if text is not None:
                diagnosis.report, diagnosis.advice, diagnosis.verdict = parse_diagnosis(text)
                diagnosis.user_action = user_action(text)
                diagnosis.question, diagnosis.choices = interaction(text)
                # An answer without a status line still had its say; retrying is the safe reading.
                diagnosis.verdict = diagnosis.verdict or DiagnosisStatus.RETRY
                if diagnosis.verdict != DiagnosisStatus.NEEDS_INPUT:
                    run.waiting_diagnosis_id = None
            # A known provider limit must not become a user-blocking environment problem,
            # even if a diagnosis used the older BLOCKED response format.
            known_failure = failure_kind(verdict.reason) if outcome == Outcome.ERROR else None
            if diagnosis.status == "done" and known_failure and diagnosis.verdict not in {DiagnosisStatus.PLATFORM_PENDING, DiagnosisStatus.NEEDS_INPUT}:
                diagnosis.verdict = DiagnosisStatus.QUOTA_EXHAUSTED if known_failure == "quota" else DiagnosisStatus.MODEL_UNAVAILABLE
            if diagnosis.verdict == DiagnosisStatus.BLOCKED and not diagnosis.user_action:
                diagnosis.verdict = DiagnosisStatus.RETRY
                diagnosis.advice = (diagnosis.advice or "") + (
                    "\n诊断未指出必须由用户提供的权限、凭据、预算或决定；请执行可用的修复并验证结果，"
                    "公共环境问题修复到共享工具目录。")
            self.emit(run_id, "run.diagnosed", diagnosis_id=diagnosis_id, status=diagnosis.status,
                      model_id=diagnosis.model_id, verdict=diagnosis.verdict)
            if run.status == RunStatus.CANCELLED or node.status not in {NodeStatus.RUNNING, NodeStatus.FAILED}:
                self._save(run)
                return
            status = diagnosis.verdict if diagnosis.status == "done" else None
            # A repair the lead asked for goes back to it, unless it needs the user or a platform repair.
            handover = (consult and bool(diagnosis.decision_id) and self._can_lead()
                        and status not in {DiagnosisStatus.PLATFORM_PENDING, DiagnosisStatus.NEEDS_INPUT})
            action = "lead" if handover else after_diagnosis(outcome, status, has_model=bool(node.model_id),
                                                             environment_fixes=node.environment_fixes)
            if action == "lead":
                pass
            elif action == "hold_for_platform":
                node.status = NodeStatus.PENDING
                node.environment_fixes += 1
                self._hold_for_platform(run, force=diagnosis.trigger == "manual")
            elif action == "ask_user":
                node.status = NodeStatus.PENDING
                run.status = RunStatus.PAUSED
                diagnosis.question = diagnosis.question or "请补充你希望采用的方案或约束。"
                run.error = diagnosis.question
                run.waiting_diagnosis_id = diagnosis.id
                run.maintenance_id = None
                self.emit(run_id, "run.diagnosis_question", diagnosis_id=diagnosis.id,
                          question=diagnosis.question, choices=diagnosis.choices)
            elif action == "switch_model":
                if not node.attempts:
                    node.attempts.append(NodeAttempt(model_id=node.model_id, outcome=Outcome.ERROR, reason=verdict.reason))
                failed_model = node.attempts[-1].model_id or node.model_id
                kind = Outcome.QUOTA if status == DiagnosisStatus.QUOTA_EXHAUSTED else Outcome.UNAVAILABLE
                self.providers.quota.learn(failed_model, verdict.reason, kind, run.id, diagnosis.id)
                exc = self.providers.quota.mark(failed_model, f"{verdict.reason}\n{diagnosis.report or ''}", kind)
                # _finish already recorded this attempt. Reclassify it without duplicating
                # its cost/history or consuming a quality retry.
                node.attempts[-1].outcome = kind
                node.review, node.review_passed = str(exc), None
                self._route_after_unavailable(run, node, exc, diagnosis.id)
            elif action == "retry_fixed":
                if shared_files:
                    self.db.save_shared_repair({
                        "id": diagnosis.id, "run_id": run.id, "model_id": diagnosis.model_id,
                        "files": shared_files, "report": diagnosis.report or "",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    })
                    self.emit(run.id, "run.repair_shared", diagnosis_id=diagnosis.id, files=shared_files)
                node.environment_fixes += 1
                node.feedback = quality.retry_feedback(
                    node.feedback, f"{verdict.reason}\n环境已修复，请重新执行这一步：\n{diagnosis.report or ''}".strip())
                node.status = NodeStatus.PENDING
                if outcome == Outcome.VERIFY_FAILED:
                    # The verification had used up its rework rounds; it gets one more.
                    run.rework_rounds = max(0, settings.max_rework_rounds - 1)
                self.emit(run_id, "node.retry", node_id=node.id, reason="environment_fixed",
                          model_id=node.model_id, attempt=len(node.attempts) + 1)
            elif action == "unable":
                # The diagnosis could not lift the limit (or needs the user to): the step ends as
                # one that cannot be done here, with why, and the rest of the task goes on.
                self._mark_unable(run, node, verdict.unable or (
                    verdict.reason if outcome == Outcome.UNABLE else "诊断判断：这一步在当前环境中无法完成。"), diagnosis)
            elif action == "pause":
                node.status = NodeStatus.FAILED
                self._pause_for_diagnosis(run, node, diagnosis, "遇到需要你处理的问题")
            elif action == "pause_rework":
                self._pause_for_diagnosis(run, node, diagnosis, f"返工 {run.rework_rounds} 轮后验证仍未通过")
            else:
                self._retry_or_fail(run, node, verdict, outcome)
                if node.status == NodeStatus.PENDING and diagnosis.advice:
                    node.feedback = f"{node.feedback}{quality.ADVICE_NOTE}{diagnosis.advice}"
                elif node.status == NodeStatus.FAILED:
                    why = f"重试 {node.failed_attempts} 次仍未通过" + (
                        f"（诊断没有完成：{diagnosis.error or '没有可用的诊断模型'}）" if diagnosis.status != "done" else "")
                    self._pause_for_diagnosis(run, node, diagnosis, why)
            self._save(run)
        if handover and not await self._after_repair(run_id, node_id, diagnosis_id, verdict, outcome):
            await self._repair_done(run_id, node_id, diagnosis_id, None, None, 0.0, verdict, outcome,
                                    shared_files, consult=False)

    def _pause_for_diagnosis(self, run: ExecutionRun, node, diagnosis: Diagnosis, why: str) -> None:
        run.error = f"步骤「{node.title}」{why}，任务已暂停。请查看诊断报告，处理后点“按此预算继续”。"
        if run.status == RunStatus.RUNNING:
            run.status = RunStatus.PAUSED
        self.emit(run.id, "approval.required", reason="diagnosis_unresolved",
                  node_id=node.id, diagnosis_id=diagnosis.id)

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
                    if record and record.reply_to and latest.waiting_diagnosis_id == record.reply_to:
                        latest.waiting_diagnosis_id = None
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
        if node and run.status in {RunStatus.PAUSED, RunStatus.FAILED, RunStatus.INCOMPLETE} and node.status in {
                NodeStatus.FAILED, NodeStatus.PENDING, NodeStatus.UNABLE}:
            # A requested diagnosis of a stopped step uses the same repair/switching path
            # as automatic recovery, then continues within the previously approved budget.
            last = node.attempts[-1] if node.attempts else None
            verdict = quality.Verdict(False, (last.reason if last else None) or node.review or run.error or "步骤未完成")
            outcome = Outcome.ERROR if not last or last.outcome in {Outcome.ERROR, Outcome.QUOTA, Outcome.UNAVAILABLE} else last.outcome
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
                if record.status != "done" or record.verdict in {DiagnosisStatus.NEEDS_INPUT, DiagnosisStatus.PLATFORM_PENDING}:
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
        workspace = Path(run.project_dir) if run.project_dir else settings.workspace_root / run.folder_id
        workspace.mkdir(parents=True, exist_ok=True)
        events = [f"{event.created_at:%H:%M:%S} {event.kind} {event.data}" for event in self.db.events(run_id)]
        report = advice = error = None
        cost = 0.0
        try:
            result, _model_id, cost = await self._diagnose_with(
                run, diagnosis, None, diagnosis_prompt(run, diagnosis.node_id, str(workspace), events,
                                                      self.platform.context()), workspace, repair=False,
            )
            text = result.text
            if parse_diagnosis(text)[2] == DiagnosisStatus.PLATFORM_REPAIR:
                async with self.repair_lock:
                    try:
                        text, extra = await self._fix_platform(run, diagnosis, node, text)
                    except BaseException as exc:
                        exc.accounted_cost_usd = getattr(exc, "accounted_cost_usd", 0.0) + cost
                        raise
                    cost += extra
            report, advice, status = parse_diagnosis(text)
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
            answered_waiting = bool(target.reply_to and latest.waiting_diagnosis_id == target.reply_to)
            if answered_waiting:
                latest.waiting_diagnosis_id = None
            if not error:
                target.question, target.choices = interaction(text)
                if status == DiagnosisStatus.NEEDS_INPUT:
                    target.question = target.question or "请补充你希望采用的方案或约束。"
                    latest.waiting_diagnosis_id = target.id
                    if latest.status in {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.PAUSED}:
                        latest.status, latest.error = RunStatus.PAUSED, target.question
                        latest.maintenance_id = None
                    self.emit(run_id, "run.diagnosis_question", diagnosis_id=target.id,
                              question=target.question, choices=target.choices)
                elif status == DiagnosisStatus.PLATFORM_PENDING:
                    latest.waiting_diagnosis_id = None
                    self._hold_for_platform(latest, force=answered_waiting)
                elif answered_waiting and status in {DiagnosisStatus.RETRY, DiagnosisStatus.FIXED}:
                    if latest.status == RunStatus.PAUSED:
                        required = latest.actual_cost_usd + sum(n.estimated_cost_usd for n in latest.plan.nodes
                                                               if n.status != NodeStatus.SUCCEEDED)
                        if required <= latest.budget_usd:
                            active = self.tasks.get(run_id)
                            latest.status = RunStatus.RUNNING if active and not active.done() else RunStatus.QUEUED
                            latest.error = None
                        else:
                            self._pause_for_budget(latest)
            target.finished_at = datetime.now(timezone.utc)
            self._save(latest)
        self.emit(run_id, "run.diagnosed", diagnosis_id=diagnosis_id, status=target.status, model_id=target.model_id)
        if latest.status in {RunStatus.QUEUED, RunStatus.RUNNING}:
            self.start(run_id)

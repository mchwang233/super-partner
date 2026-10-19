"""Carrying out the task lead's decisions after a failed attempt, and handing the user's answers
to its questions back to it (supervisor.py words the questions and reads the answers). Mixed
into executor.Executor."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from . import quality, replan, supervisor
from .budget import BudgetExceeded
from .config import settings
from .models import (
    Decision,
    Diagnosis,
    DiagnosisStatus,
    LeadMessage,
    LeadThinking,
    NodeStatus,
    Outcome,
    RunStatus,
)
from .routing import eligible_for_node, node_cost


class Leading:
    """Uses the executor's run state, locks, ask_lead, and its step outcomes (_mark_unable,
    _pause_for_budget) and diagnosis (_repair, used for repairs and when no lead can decide)."""

    def _can_lead(self) -> bool:
        return settings.lead_decisions and hasattr(self.providers, "converse")

    @staticmethod
    def _decisions_left(run, node) -> tuple[int, int]:
        """Decisions the lead may still make for ``node`` and for the task before it pauses."""
        counted = [item for item in run.decisions if item.action != "fallback"
                   and (run.decisions_since is None or item.created_at >= run.decisions_since)]
        return (settings.max_node_decisions - sum(item.node_id == node.id for item in counted),
                settings.max_run_decisions - len(counted))

    def _choices(self, run, node) -> list:
        return eligible_for_node(node, list(self.model_lookup.values()), self._configured(run),
                                 any(item.kind == "image" for item in run.attachments))

    async def _decide(self, run_id: str, node_id: str, verdict: quality.Verdict, outcome: str) -> None:
        """An attempt of the step failed: the lead decides what to do, and it is done. Without a
        lead able to decide, the usual diagnosis takes over."""
        run = self.db.get_run(run_id)
        node = next(item for item in run.plan.nodes if item.id == node_id) if run else None
        if not node or run.status == RunStatus.CANCELLED:
            return
        left = self._decisions_left(run, node)
        if min(left) <= 0:
            async with self.locks.setdefault(run_id, asyncio.Lock()):
                latest = self.db.get_run(run_id)
                step = next(item for item in latest.plan.nodes if item.id == node_id)
                step.status = NodeStatus.PENDING
                which = f"步骤「{step.title}」已决策 {settings.max_node_decisions} 次" if left[0] <= 0 \
                    else f"整个任务已决策 {settings.max_run_decisions} 次"
                latest.error = f"{which}仍未解决，任务已暂停。看过情况后点“按此预算继续”，负责人会接着处理。"
                if latest.status in {RunStatus.RUNNING, RunStatus.QUEUED}:
                    latest.status = RunStatus.PAUSED
                self._save(latest)
            self.emit(run_id, "approval.required", reason="decision_limit", node_id=node_id)
            return
        choices = self._choices(run, node)
        verifying = outcome == Outcome.VERIFY_FAILED
        targets = self._targets(run, node) if verifying else None
        prompt = (supervisor.verification_message(run, node, verdict, choices, left) if verifying
                  else supervisor.failure_message(run, node, node.attempts[-1], verdict, choices, left))
        event = "verify_failed" if verifying else "attempt_failed"
        decision = await self._ask_for_decision(run_id, node_id, prompt, {item.id for item in choices}, targets,
                                                event=event)
        if decision is None:
            await self._record_fallback(run_id, node_id)
            if verifying:
                # As before the lead: the findings send the verified work back, within the usual rounds.
                async with self.locks.setdefault(run_id, asyncio.Lock()):
                    latest = self.db.get_run(run_id)
                    self._send_back(latest, next(item for item in latest.plan.nodes if item.id == node_id), verdict)
                    self._save(latest)
            else:
                await self._repair(run_id, node_id, verdict, outcome)
        elif decision != "budget":
            await self._apply_decision(run_id, node_id, decision, verdict, outcome, event)

    @staticmethod
    def _targets(run, node) -> set[str]:
        """The steps a verification may send back: those it checks."""
        return {item for item in node.verifies if any(other.id == item for other in run.plan.nodes)}

    @contextlib.asynccontextmanager
    async def _thinking(self, run_id: str, event: str, node_id: Optional[str]):
        """Shows that the lead is deciding ``event`` (the panel and the timeline) until it is done."""
        async def show(thinking: Optional[LeadThinking]):
            async with self.locks.setdefault(run_id, asyncio.Lock()):
                run = self.db.get_run(run_id)
                if run:
                    run.lead_thinking = thinking
                    self._save(run)

        await show(LeadThinking(event=event, node_id=node_id))
        self.emit(run_id, "run.lead_thinking", event=event, node_id=node_id)
        try:
            yield
        finally:
            await asyncio.shield(show(None))

    async def _ask_for_decision(self, run_id: str, node_id: Optional[str], prompt: str, model_ids: set[str],
                                targets: Optional[set[str]] = None, final: bool = False,
                                event: str = "attempt_failed"):
        """The lead's decision; None when no lead can give a valid one, or "budget" when the task
        has run out of budget for asking (it is then paused). ``targets``: the steps rework may
        send back; ``final``: every step has finished; ``event``: what it decides on."""
        async with self._thinking(run_id, event, node_id):
            return await self._decision(run_id, node_id, prompt, model_ids, targets, final)

    async def _decision(self, run_id: str, node_id: Optional[str], prompt: str, model_ids: set[str],
                        targets: Optional[set[str]], final: bool):
        def read(reply: str):
            decision = supervisor.parse_decision(reply, model_ids, targets, final)
            if decision and decision["action"] == "replan":
                problem = self._plan_problem(run_id, decision["plan"], node_id)
                if problem:
                    return None, f"That plan change cannot be made: {problem}"
            return decision, "That was not a valid decision."

        try:
            decision, problem = read(await self.ask_lead(run_id, prompt))
            if decision is None:
                # Same conversation: it only has to say what it decided in the asked form.
                decision, _ = read(await self.ask_lead(
                    run_id, f"{problem}\n" + supervisor.decision_format(bool(targets), final)))
            return decision
        except asyncio.CancelledError:
            raise
        except BudgetExceeded:
            async with self.locks.setdefault(run_id, asyncio.Lock()):
                run = self.db.get_run(run_id)
                for item in run.plan.nodes:
                    if item.id == node_id:
                        item.status = NodeStatus.PENDING
                self._pause_for_budget(run)
                self._save(run)
            return "budget"
        except Exception:
            return None

    def _change_plan(self, run, change: dict, deciding: Optional[str]) -> dict:
        return replan.change_plan(run, change, list(self.model_lookup.values()), self._configured(run),
                                  any(item.kind == "image" for item in run.attachments), deciding)

    def _plan_problem(self, run_id: str, change: dict, deciding: Optional[str]) -> Optional[str]:
        """Why a plan change cannot be made, tried on a copy of the task; None if it can."""
        try:
            self._change_plan(self.db.get_run(run_id).model_copy(deep=True), change, deciding)
        except (replan.PlanError, AttributeError, TypeError, KeyError) as exc:
            # A malformed change (not the shape asked for) is as unusable as an impossible one.
            return str(exc) or type(exc).__name__
        return None

    def _reopen(self, run, targets: set[str], note: str) -> list[str]:
        """Send finished steps back to be redone with ``note``, and everything built on them."""
        reopened = targets | quality.descendants(run, targets)
        for node in run.plan.nodes:
            if node.id not in reopened:
                continue
            node.generation += 1
            node.status, node.failed_attempts = NodeStatus.PENDING, 0
            node.feedback = f"负责人检查整体结果后要求返工：{note}" if node.id in targets else None
        return sorted(reopened)

    async def _final_review(self, run_id: str) -> bool:
        """Every step has finished: the lead checks the whole result against the request. True if
        it sent work back or changed the plan, so the task goes on; False to finish it."""
        run = self.db.get_run(run_id)
        if not self._can_lead() or not (run.supervisor and run.supervisor.model_id):
            return False
        counted = [item for item in run.decisions if item.action != "fallback"
                   and (run.decisions_since is None or item.created_at >= run.decisions_since)]
        left = settings.max_run_decisions - len(counted)
        if left <= 0:
            return False
        targets = {node.id for node in run.plan.nodes}
        decision = await self._ask_for_decision(run_id, None, supervisor.final_message(run, left), set(),
                                                targets, final=True, event="all_done")
        if decision == "budget":
            return True
        if decision is None:
            return False
        action, reason = decision["action"], decision.get("reason", "")
        async with self.locks.setdefault(run_id, asyncio.Lock()):
            run = self.db.get_run(run_id)
            run.decisions.append(Decision(event="all_done", action=action, reason=reason,
                                          instructions=decision.get("instructions"), targets=decision.get("targets", []),
                                          lead_model_id=run.supervisor.model_id))
            changes = None
            if action == "rework":
                self._reopen(run, set(decision["targets"]), decision.get("instructions") or reason)
            elif action == "replan":
                changes = self._change_plan(run, decision["plan"], None)
            self._save(run)
        self.emit(run_id, "run.decision", action=action, reason=reason, targets=decision.get("targets"),
                  event="all_done")
        if changes:
            self.emit(run_id, "run.plan_changed", **changes)
        return action != "done"

    async def _record_fallback(self, run_id: str, node_id: str) -> None:
        async with self.locks.setdefault(run_id, asyncio.Lock()):
            run = self.db.get_run(run_id)
            run.decisions.append(Decision(node_id=node_id, event="attempt_failed", action="fallback",
                                          reason="负责人暂时无法决策，改由诊断处理这次失败。"))
            self._save(run)
        self.emit(run_id, "run.decision", node_id=node_id, action="fallback",
                  reason="负责人暂时无法决策，改由诊断处理这次失败。")

    async def _apply_decision(self, run_id: str, node_id: str, decision: dict, verdict: quality.Verdict,
                              outcome: str, event: str) -> None:
        """Do what the lead decided for the step, and record it with its reason."""
        action, reason = decision["action"], decision.get("reason", "")
        instructions = decision.get("instructions")
        repair = changes = None
        async with self.locks.setdefault(run_id, asyncio.Lock()):
            run = self.db.get_run(run_id)
            node = next(item for item in run.plan.nodes if item.id == node_id)
            if run.status == RunStatus.CANCELLED or node.status not in {NodeStatus.RUNNING, NodeStatus.PENDING}:
                return
            made = Decision(
                node_id=node_id, event=event, action=action, reason=reason, instructions=instructions,
                model_id=decision.get("model_id"), targets=decision.get("targets", []),
                lead_model_id=run.supervisor.model_id if run.supervisor else None,
            )
            run.decisions.append(made)
            asked = f"负责人要求：{instructions}" if instructions else ""
            if action in {"retry", "switch_model"}:
                if action == "switch_model":
                    model = self.model_lookup[decision["model_id"]]
                    node.model_id, node.estimated_cost_usd = model.id, round(node_cost(node, model), 6)
                node.feedback = quality.retry_feedback(node.feedback, "\n".join(filter(None, [verdict.reason, asked])))
                node.status = NodeStatus.PENDING
                self.emit(run_id, "node.retry", node_id=node_id, reason="lead", model_id=node.model_id,
                          attempt=len(node.attempts) + 1)
            elif action == "rework":
                self._send_back(run, node, verdict, set(decision["targets"]), instructions, limited=False)
            elif action == "replan":
                changes = self._change_plan(run, decision["plan"], node_id)
                current = next((item for item in run.plan.nodes if item.id == node_id), None)
                if current is not None:
                    # The step stays in the plan: it runs again, after any steps added before it.
                    current.status = NodeStatus.PENDING
                    current.feedback = quality.retry_feedback(current.feedback, "\n".join(filter(None, [verdict.reason, asked])))
            elif action == "accept":
                node.status, node.feedback = NodeStatus.SUCCEEDED, None
                node.review = f"负责人接受：{reason}"
                self.emit(run_id, "node.succeeded", node_id=node_id, accepted_by_lead=True, reason=reason)
            elif action == "unable":
                self._mark_unable(run, node, reason or verdict.unable or verdict.reason,
                                  user_action=decision.get("user_action"))
            elif action == "request_component":
                job = self._engineer_for_lead(run, node, instructions or reason, made.id)
                if job:
                    made.engineer_job_id = job.id
                    self.emit(run_id, "run.engineer_requested", node_id=node_id, job_id=job.id)
                else:
                    self._mark_unable(run, node, f"这一步需要 Super Partner 新增能力，但平台工程师当前不可用：{instructions or reason}",
                                      user_action="启用平台自修复后，可以在“平台工程师”页面提出这个需求。")
            elif action == "ask_user":
                question = Diagnosis(
                    id=uuid4().hex[:8], trigger="lead", node_id=node_id,
                    model_id=run.supervisor.model_id if run.supervisor else None, status="done",
                    verdict=DiagnosisStatus.NEEDS_INPUT, report=reason, question=decision["question"],
                    choices=decision.get("options", []), finished_at=datetime.now(timezone.utc),
                )
                run.diagnoses.append(question)
                node.status = NodeStatus.PENDING
                if run.status in {RunStatus.RUNNING, RunStatus.QUEUED}:
                    run.status = RunStatus.PAUSED
                run.error, run.waiting_diagnosis_id = question.question, question.id
                self.emit(run_id, "run.diagnosis_question", diagnosis_id=question.id,
                          question=question.question, choices=question.choices)
            else:
                # A repair session with access to this machine (or a platform source repair) does
                # the work; its outcome comes back to the lead (_after_repair) before the step runs.
                node.status = NodeStatus.RUNNING
                what = ("Super Partner itself is at fault; make a platform repair (STATUS: PLATFORM_REPAIR)"
                        if action == "repair_platform" else "the environment needs a repair")
                repair = (verdict, f"{what}: {instructions or reason}")
            self._save(run)
        self.emit(run_id, "run.decision", node_id=node_id, action=action, reason=reason,
                  model_id=decision.get("model_id"), targets=decision.get("targets"), event=event)
        if changes:
            self.emit(run_id, "run.plan_changed", **changes)
        if repair:
            await self._repair(run_id, node_id, repair[0], outcome, brief=repair[1], decision_id=made.id)

    async def _after_repair(self, run_id: str, node_id: str, diagnosis_id: str, verdict: quality.Verdict,
                            outcome: str) -> bool:
        """A repair the lead asked for has ended: the lead sees how, and decides what happens to
        the step, so a repair that did not work does not send the step back into the same wall.
        False when no lead could decide; the usual handling of a repair then applies."""
        run = self.db.get_run(run_id)
        node = next(item for item in run.plan.nodes if item.id == node_id)
        diagnosis = next(item for item in run.diagnoses if item.id == diagnosis_id)
        left = self._decisions_left(run, node)
        if min(left) <= 0:
            return False
        choices = self._choices(run, node)
        decision = await self._ask_for_decision(
            run_id, node_id, supervisor.repair_message(run, node, diagnosis, choices, left),
            {item.id for item in choices}, event="repair_done")
        if decision is None:
            return False
        if decision != "budget":
            await self._apply_decision(run_id, node_id, decision, verdict, outcome, "repair_done")
        return True

    def start_lead_chat(self, run_id: str, text: str) -> bool:
        """The user writes to the lead: the message and a pending reply are saved at once, and the
        lead answers in the background. False if it is still answering an earlier message."""
        task = self.lead_tasks.get(run_id)
        if task and not task.done():
            return False
        run = self.db.get_run(run_id)
        for message in run.lead_messages:
            if message.status == "pending":
                # Its answer was lost (a restart while the lead was answering, say).
                message.status, message.text = "failed", "这条回复被中断了，请重新提问。"
        run.lead_messages += [LeadMessage(role="user", text=text), LeadMessage(role="lead", status="pending")]
        reply_id = run.lead_messages[-1].id
        self._save(run)
        self.emit(run_id, "run.lead_message", role="user", text=text)
        self.lead_tasks[run_id] = asyncio.create_task(self._lead_chat(run_id, text, reply_id))
        return True

    async def _lead_chat(self, run_id: str, text: str, reply_id: str) -> None:
        try:
            answer, status = await self.ask_lead(run_id, supervisor.chat_message(text)), "done"
        except asyncio.CancelledError:
            answer, status = "这条回复被中断了，请重新提问。", "failed"
            raise
        except Exception as exc:
            answer, status = f"负责人暂时无法回答：{str(exc)[:300]}", "failed"
        finally:
            async with self.locks.setdefault(run_id, asyncio.Lock()):
                run = self.db.get_run(run_id)
                reply = next((item for item in run.lead_messages if item.id == reply_id), None) if run else None
                if reply:
                    reply.text, reply.status = answer, status
                    self._save(run)
        self.emit(run_id, "run.lead_message", role="lead", status=status)

    def start_lead_answer(self, run_id: str, question_id: str) -> None:
        """The user answered the lead's question: it decides what to do with the step."""
        self.diagnosing[run_id] = asyncio.create_task(self._lead_answer(run_id, question_id))

    async def _lead_answer(self, run_id: str, question_id: str) -> None:
        try:
            run = self.db.get_run(run_id)
            question = next(item for item in run.diagnoses if item.id == question_id)
            node = next(item for item in run.plan.nodes if item.id == question.node_id)
            reply = question.messages[-1]
            answer = "\n".join(filter(None, [reply.choice, reply.text]))
            choices = self._choices(run, node)
            targets = self._targets(run, node) or None
            decision = await self._ask_for_decision(
                run_id, node.id, supervisor.answer_message(node, question.question, answer, bool(targets)),
                {m.id for m in choices}, targets, event="user_answer")
            if decision == "budget":
                return
            if decision is None:
                async with self.locks.setdefault(run_id, asyncio.Lock()):
                    latest = self.db.get_run(run_id)
                    latest.error = "负责人暂时无法处理你的回答，请稍后点“按此预算继续”。"
                    self._save(latest)
                return
            last = node.attempts[-1] if node.attempts else None
            verdict = quality.Verdict(False, (last.reason if last else None) or "步骤未完成")
            await self._apply_decision(run_id, node.id, decision, verdict, last.outcome if last else "error",
                                       "user_answer")
            await self._continue_after_answer(run_id)
        finally:
            if self.diagnosing.get(run_id) is asyncio.current_task():
                self.diagnosing.pop(run_id, None)

    async def _continue_after_answer(self, run_id: str) -> None:
        """A task paused for the lead's question goes on once nothing else holds it."""
        async with self.locks.setdefault(run_id, asyncio.Lock()):
            run = self.db.get_run(run_id)
            if (run.status != RunStatus.PAUSED or run.waiting_diagnosis_id or run.maintenance_id
                    or run.engineer_job_id):
                return
            required = run.actual_cost_usd + sum(node.estimated_cost_usd for node in run.plan.nodes
                                                 if node.status != NodeStatus.SUCCEEDED)
            if required > run.budget_usd:
                self._pause_for_budget(run)
                self._save(run)
                return
            active = self.tasks.get(run_id)
            run.status = RunStatus.RUNNING if active and not active.done() else RunStatus.QUEUED
            run.error = None
            self._save(run)
        self.emit(run_id, "run.recovered", reason="lead")
        self.start(run_id)

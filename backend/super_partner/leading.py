"""Carrying out the task lead's decisions after a failed attempt, and handing the user's answers
to its questions back to it (supervisor.py words the questions and reads the answers). Mixed
into executor.Executor."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from . import quality, supervisor
from .budget import BudgetExceeded
from .config import settings
from .models import Decision, Diagnosis, DiagnosisStatus, NodeStatus, RunStatus
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
        prompt = supervisor.failure_message(run, node, node.attempts[-1], verdict, choices, left)
        decision = await self._ask_for_decision(run_id, node_id, prompt, {item.id for item in choices})
        if decision is None:
            await self._record_fallback(run_id, node_id)
            await self._repair(run_id, node_id, verdict, outcome)
        elif decision != "budget":
            await self._apply_decision(run_id, node_id, decision, verdict, outcome, "attempt_failed")

    async def _ask_for_decision(self, run_id: str, node_id: str, prompt: str, model_ids: set[str]):
        """The lead's decision; None when no lead can give a valid one, or "budget" when the task
        has run out of budget for asking (it is then paused)."""
        try:
            decision = supervisor.parse_decision(await self.ask_lead(run_id, prompt), model_ids)
            if decision is None:
                # Same conversation: it only has to say what it decided in the asked form.
                decision = supervisor.parse_decision(await self.ask_lead(
                    run_id, "That was not a valid decision.\n" + supervisor.DECISION_FORMAT), model_ids)
            return decision
        except asyncio.CancelledError:
            raise
        except BudgetExceeded:
            async with self.locks.setdefault(run_id, asyncio.Lock()):
                run = self.db.get_run(run_id)
                next(item for item in run.plan.nodes if item.id == node_id).status = NodeStatus.PENDING
                self._pause_for_budget(run)
                self._save(run)
            return "budget"
        except Exception:
            return None

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
        repair = None
        async with self.locks.setdefault(run_id, asyncio.Lock()):
            run = self.db.get_run(run_id)
            node = next(item for item in run.plan.nodes if item.id == node_id)
            if run.status == RunStatus.CANCELLED or node.status not in {NodeStatus.RUNNING, NodeStatus.PENDING}:
                return
            run.decisions.append(Decision(
                node_id=node_id, event=event, action=action, reason=reason, instructions=instructions,
                model_id=decision.get("model_id"), lead_model_id=run.supervisor.model_id if run.supervisor else None,
            ))
            asked = f"负责人要求：{instructions}" if instructions else ""
            if action in {"retry", "switch_model"}:
                if action == "switch_model":
                    model = self.model_lookup[decision["model_id"]]
                    node.model_id, node.estimated_cost_usd = model.id, round(node_cost(node, model), 6)
                node.feedback = quality.retry_feedback(node.feedback, "\n".join(filter(None, [verdict.reason, asked])))
                node.status = NodeStatus.PENDING
                self.emit(run_id, "node.retry", node_id=node_id, reason="lead", model_id=node.model_id,
                          attempt=len(node.attempts) + 1)
            elif action == "accept":
                node.status, node.feedback = NodeStatus.SUCCEEDED, None
                node.review = f"负责人接受：{reason}"
                self.emit(run_id, "node.succeeded", node_id=node_id, accepted_by_lead=True, reason=reason)
            elif action == "unable":
                self._mark_unable(run, node, reason or verdict.unable or verdict.reason,
                                  user_action=decision.get("user_action"))
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
                # A repair session with access to this machine (or a platform source repair)
                # does the work; the diagnosis flow then runs the step again or pauses.
                node.status = NodeStatus.RUNNING
                what = ("Super Partner itself is at fault; make a platform repair (STATUS: PLATFORM_REPAIR)"
                        if action == "repair_platform" else "the environment needs a repair")
                repair = (verdict, f"{what}: {instructions or reason}")
            self._save(run)
        self.emit(run_id, "run.decision", node_id=node_id, action=action, reason=reason,
                  model_id=decision.get("model_id"), event=event)
        if repair:
            await self._repair(run_id, node_id, repair[0], outcome, brief=repair[1])

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
            decision = await self._ask_for_decision(
                run_id, node.id, supervisor.answer_message(node, question.question, answer), {m.id for m in choices})
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
            if run.status != RunStatus.PAUSED or run.waiting_diagnosis_id or run.maintenance_id:
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

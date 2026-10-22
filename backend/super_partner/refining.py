"""Splitting a running task's major parts into tasks (docs/design/plan-parts.md). A task is
planned as major parts only; when it runs, every part not yet split is split at the same time,
each by a lead model in a conversation of its own (outline.refine). Its tasks join the plan as
soon as they are routed, and run as their waits allow. A part that cannot be split pauses the
task with the reason; continuing it tries again."""

from __future__ import annotations

import asyncio
import logging

from . import contracts, outline, supervisor
from .config import settings
from .models import RunStatus
from .routing import NoEligibleModelError, fit_and_route, reestimate

logger = logging.getLogger(__name__)
# What the executor's run loop calls a split at work, beside its nodes.
PREFIX = "part:"


class Refining:
    """Mixed into Executor; uses its db, locks, model_lookup, providers, _charged, _save, emit."""

    def _split_parts(self, run, running: dict) -> None:
        """Start splitting every major part not split yet, all at once."""
        busy = set(running.values())
        for part in run.plan.parts:
            if part.parent_id is None and part.status in {"planned", "failed"} and PREFIX + part.id not in busy:
                running[asyncio.create_task(self._split_part(run.id, part.id))] = PREFIX + part.id

    async def _split_part(self, run_id: str, part_id: str) -> None:
        async with self.locks.setdefault(run_id, asyncio.Lock()):
            run = self.db.get_run(run_id)
            part = next(item for item in run.plan.parts if item.id == part_id)
            part.status, part.error = "refining", None
            effort = settings.lead_reasoning_effort
            if settings.adaptive_refinement:
                effort = part.refinement_effort or outline.REFINEMENT_EFFORT[part.difficulty]
                part.refinement_effort = effort
                if not part.refinement_reason:
                    part.refinement_reason = "旧计划未单独指定推理档，按大项难度选择。"
            else:
                part.refinement_effort = effort if effort in {"low", "medium", "high", "max"} else ""
                part.refinement_reason = "平台未启用按难度细化，使用统一的负责人推理档。"
            self._save(run)
        self.emit(run_id, "part.refining", part_id=part_id, title=part.title,
                  effort=effort, difficulty=part.difficulty, reason=part.refinement_reason)
        models = list(self.model_lookup.values())
        configured = self._configured(run)
        leads = supervisor.candidates(models, configured)
        # The task's own lead first: it made the outline.
        lead = run.supervisor.model_id if run.supervisor else None
        leads.sort(key=lambda model: model.id != lead)
        error, split, used = "没有可用的负责人模型", None, None
        for model in leads[:supervisor.MAX_LEADS]:
            session = supervisor.new_session(model)
            if settings.adaptive_refinement:
                session.reasoning_effort = effort
            self.emit(run_id, "part.refine_attempt", part_id=part_id, model_id=model.id, effort=effort)

            async def say(text: str, session=session, model=model) -> str:
                result = await self._charged(run, None, model.id, lambda budget: self.providers.converse(
                    session, text, spend_ok=budget))
                return result.text

            try:
                split = await outline.refine(say, run.plan, part, outline.part_opening(run.plan, part, run.allow_network))
                used = model.id
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("%s could not split part %s: %s", model.id, part.title, exc)
                error = str(exc)[:500]
        async with self.locks.setdefault(run_id, asyncio.Lock()):
            run = self.db.get_run(run_id)
            part = next(item for item in run.plan.parts if item.id == part_id)
            if split:
                parts, nodes = split
                vision = any(item.kind == "image" for item in run.attachments)
                try:
                    for node in nodes:
                        node.needs_workspace = bool(run.parent_run_id) or (run.plan.in_project and node.needs_workspace)
                        fit_and_route(node, models, self._configured(run), vision)
                except NoEligibleModelError as exc:
                    split, error = None, f"没有模型能做「{node.title}」：{exc}"
            if split:
                previous = run.plan.model_copy(deep=True)
                run.plan.parts += parts
                run.plan.nodes += nodes
                part.status = "ready"
                outline.link(run.plan)
                try:
                    contracts.validate(run.plan, partial=True)
                except contracts.ContractError as exc:
                    run.plan = previous
                    part = next(item for item in run.plan.parts if item.id == part_id)
                    error = str(exc)
                else:
                    reestimate(run)
                    self._save(run)
                    self.emit(run_id, "part.refined", part_id=part_id, title=part.title, tasks=len(nodes),
                              parts=len(parts), model_id=used)
                    return
            part.status, part.error = "failed", error
            if run.status == RunStatus.RUNNING:
                run.status = RunStatus.PAUSED
                run.error = f"大项「{part.title}」没能细化成任务：{error}。可以继续执行，再试一次。"
            self._save(run)
            self.emit(run_id, "part.refine_failed", part_id=part_id, title=part.title, error=error)

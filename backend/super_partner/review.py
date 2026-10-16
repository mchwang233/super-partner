"""Accepting or rejecting a step's result: a verification's own verdict, or a reviewer
model against the acceptance criteria. Mixed into executor.Executor."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional

from . import progress, quality, supervisor
from .budget import BudgetExceeded
from .config import settings
from .models import ExecutionRun
from .quota import ModelUnavailable
from .routing import can_act, pick_reviewer

# Reviewers tried in turn when one is out of quota.
MAX_BACKUPS = 3


class Reviewing:
    """Uses the executor's model calls (_complete), catalog and configured models."""

    async def _check(self, run: ExecutionRun, node, text: str, changed: list[str], workspace: Optional[Path]) -> quality.Verdict:
        """Accept or reject a result: verification nodes give their own verdict; others are
        reviewed by another model against the node's acceptance criteria. A result whose model
        says part of the task cannot be done here is not accepted, whatever else it did."""
        claim = quality.unable_claim(text)
        if claim:
            return quality.Verdict(False, f"无法完成：{claim}", fix=claim, unable=claim)
        if node.verifies:
            verdict = quality.verification_verdict(text)
            if verdict is None:
                return quality.Verdict(False, "验证结果没有给出 VERDICT 结论，尚未验证。", fix="实际执行验证并以 VERDICT: PASS 或 VERDICT: FAIL: 原因结束。", verified=False)
            passed, problems = verdict
            return quality.Verdict(passed, "验证通过。" if passed else f"验证发现问题：{problems or '未说明'}", fix=problems)
        if not settings.review_nodes:
            return quality.Verdict(True, "未开启验收。")
        summarize = len(text) > settings.handoff_max_chars
        # Even an attempt with fresh edits can depend on code from earlier attempts. Keep it
        # and the task's explicit references in view, including cited verification logs.
        references = quality.mentioned_files(workspace, "\n".join(filter(None, [
            text, node.result, node.instructions, node.acceptance_criteria, node.feedback,
        ]))) if workspace else []
        evidence_files = list(dict.fromkeys([*changed, *node.changed_files, *references]))
        prompt = quality.review_prompt(
            node, text, changed,
            await asyncio.to_thread(quality.change_evidence, workspace, evidence_files, referenced=references) if workspace else "",
            summarize,
        )
        folder = settings.workspace_root / run.folder_id / ".review"
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
                reply = await self._complete(
                    run, node, reviewer.id, prompt, folder, cli_timeout=300, allow_api_fallback=False,
                    review_workspace=workspace if can_act(reviewer) else None, review_files=references,
                )
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
        unable = payload.get("unable") if isinstance(payload.get("unable"), str) and payload["unable"].strip() else None
        passed = payload["passed"] and not unable
        reason = str(payload.get("reason") or ("验收通过。" if passed else "验收未通过。"))
        return quality.Verdict(
            passed, f"无法完成：{unable.strip()}" if unable else reason,
            fix=str(payload.get("fix") or unable or ""), summary=summary, unable=unable.strip()[:1000] if unable else None,
            reviewer_model_id=reviewer.id, cost_usd=review_cost, accounted_cost_usd=review_cost,
        )

    def _progress_watch(self, run: ExecutionRun, node, model_id: str) -> progress.Watch:
        """Checks on an attempt of ``node`` by ``model_id`` that runs long: another model says
        whether it is stuck. A check that cannot be made lets the attempt go on."""
        started = time.monotonic()

        async def watch(evidence: str) -> Optional[str]:
            minutes = (time.monotonic() - started) / 60
            prompt = progress.check_prompt(node, minutes, evidence)
            checker = pick_reviewer(list(self.model_lookup.values()), self._configured(run), model_id,
                                    len(prompt) // 3 + 1000)
            reason = None
            if checker:
                folder = settings.workspace_root / run.folder_id / ".review"
                folder.mkdir(parents=True, exist_ok=True)
                try:
                    reply = await self._complete(run, node, checker.id, prompt, folder, cli_timeout=300,
                                                 allow_api_fallback=False)
                    reason = progress.parse(reply.text)
                except (asyncio.CancelledError, BudgetExceeded):
                    raise
                except Exception:
                    reason = None
            self.emit(run.id, "node.progress_check", node_id=node.id, model_id=model_id,
                      checker_model_id=checker.id if checker else None, minutes=round(minutes),
                      stop=bool(reason), reason=reason)
            return reason

        return watch

    def _overrun_check(self, run: ExecutionRun, node, model_id: str) -> Optional[tuple[int, progress.Watch]]:
        """For a work package with a lead: (expected turns, a check that asks the lead whether an
        attempt far past them should go on). None for other steps."""
        expected = progress.expected_turns(node)
        if not expected or not run.supervisor or not run.supervisor.model_id or not self._can_lead():
            return None

        async def check(evidence: str) -> Optional[str]:
            turns = int(evidence.split(" ", 1)[0]) if evidence[:1].isdigit() else 0
            try:
                async with self._thinking(run.id, "overrun", node.id):
                    reply = await self.ask_lead(run.id, supervisor.overrun_message(node, turns, expected, evidence))
                reason = progress.parse(reply)
            except (asyncio.CancelledError, BudgetExceeded):
                raise
            except Exception:
                reason = None  # without an answer the attempt goes on
            self.emit(run.id, "node.overrun_check", node_id=node.id, model_id=model_id, turns=turns,
                      expected=expected, stop=bool(reason), reason=reason)
            return reason

        return expected, check

    def _reviewer(self, run: ExecutionRun, node, tokens: int):
        return pick_reviewer(list(self.model_lookup.values()), self._configured(run), node.model_id, tokens)

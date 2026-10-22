"""Accepting or rejecting a step's result: a verification's own verdict, or a reviewer
model against the acceptance criteria. Mixed into executor.Executor."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional

from . import agent, contracts, progress, quality, supervisor
from .budget import BudgetExceeded, StepLimitExceeded
from .config import settings
from .models import ArtifactEvidence, ExecutionRun, NodeStatus
from .quota import ModelUnavailable
from .routing import can_act, loop_turns, pick_reviewer

# Reviewers tried in turn when one is out of quota.
MAX_BACKUPS = 3
# Reasoning effort for the check on a long attempt (a short yes or no).
PROGRESS_EFFORT = "low"


class Reviewing:
    """Uses the executor's model calls (_complete), catalog and configured models."""

    async def _check(self, run: ExecutionRun, node, text: str, changed: list[str], workspace: Optional[Path]) -> quality.Verdict:
        if blocked := quality.blocked_claim(text):
            return quality.Verdict(False, blocked, blocked=blocked)
        evidence = None
        if node.artifact_contract:
            evidence, problem = await self._artifact_check(run, node)
            if problem:
                # A missing audit deliverable says nothing against its already accepted inputs.
                return quality.Verdict(False, problem, verified=False, artifact_evidence=evidence)
        verdict = await self._review_result(run, node, text, changed, workspace, evidence)
        if evidence:
            evidence.accepted = verdict.passed
            verdict.artifact_evidence = evidence
        return verdict

    async def _artifact_check(self, run, node):
        """Execute declared acceptance commands in the ordinary task sandbox, keeping evidence."""
        evidence = ArtifactEvidence(generation=node.generation, inputs=node.artifact_inputs)
        try:
            outputs = await asyncio.to_thread(contracts.delivery_snapshot, run, node)
        except contracts.ArtifactBlocked as exc:
            return evidence, str(exc)
        latest = self.db.get_run(run.id)
        ws = agent.Workspace(contracts.workspace(run, node), network=bool(latest and latest.allow_network))
        try:
            for command in node.artifact_contract.acceptance_commands:
                output, ok = await ws.run("Bash", {"command": command})
                evidence.checks.append({"command": command, "ok": ok, "output": output})
                entry = {"turn": 0, "tool": "Acceptance", "detail": command, "ok": ok, "output": output}
                self.tool_logs.setdefault((run.id, node.id), []).append(entry)
                if trace := self.tool_traces.get((run.id, node.id)):
                    from .providers import _trace
                    _trace(trace, entry)
                if not ok:
                    return evidence, f"产物验收命令失败：{command}\n{output}"
            if await asyncio.to_thread(contracts.delivery_snapshot, run, node) != outputs:
                return evidence, "验收期间产物变化，验收证据失效。"
            evidence.outputs = outputs
            return evidence, None
        except contracts.ArtifactBlocked as exc:
            return evidence, str(exc)
        finally:
            ws.close()

    async def _review_result(self, run: ExecutionRun, node, text: str, changed: list[str], workspace: Optional[Path],
                             artifact_evidence=None) -> quality.Verdict:
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
        if artifact_evidence:
            prompt += "\nPlatform artifact checks (executed in the task sandbox):\n" + artifact_evidence.model_dump_json()
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
                    # A harder step's result deserves a harder look.
                    effort=settings.reasoning_by_difficulty.get(node.difficulty),
                )
                review_cost += reply.accounted_cost_usd
                payload = quality.parse_review(reply.text)
                if payload is not None:
                    break
                problem = "验收回复无法解析，尚未验收。"
            except asyncio.CancelledError as exc:
                exc.accounted_cost_usd = getattr(exc, "accounted_cost_usd", 0.0) + review_cost
                raise
            except (BudgetExceeded, StepLimitExceeded) as exc:
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
        blocked = payload.get("blocked") if isinstance(payload.get("blocked"), str) and payload["blocked"].strip() else None
        passed = payload["passed"] and not unable and not blocked
        reason = str(payload.get("reason") or ("验收通过。" if passed else "验收未通过。"))
        return quality.Verdict(
            passed, f"无法完成：{unable.strip()}" if unable else reason,
            fix=str(payload.get("fix") or unable or ""), summary=summary, unable=unable.strip()[:1000] if unable else None,
            reviewer_model_id=reviewer.id, cost_usd=review_cost, accounted_cost_usd=review_cost,
            blocked=blocked,
        )

    def _progress_watch(self, run: ExecutionRun, node, model_id: str) -> progress.Watch:
        """Checks on an attempt of ``node`` by ``model_id`` that runs long: another model says
        whether it is stuck. A check that cannot be made lets the attempt go on."""
        started = time.monotonic()
        asked = [0]  # how many times the lead was asked about this attempt's time

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
                    # Whether an attempt is stuck is a quick call: little thinking needed.
                    reply = await self._complete(run, node, checker.id, prompt, folder, cli_timeout=300,
                                                 allow_api_fallback=False, effort=PROGRESS_EFFORT)
                    reason = progress.parse(reply.text)
                except (asyncio.CancelledError, BudgetExceeded, StepLimitExceeded):
                    raise
                except Exception:
                    reason = None
            self.emit(run.id, "node.progress_check", node_id=node.id, model_id=model_id,
                      checker_model_id=checker.id if checker else None, minutes=round(minutes),
                      stop=bool(reason), reason=reason)
            return reason or await self._critical_check(run.id, node, model_id, minutes, evidence, asked)

        return watch

    async def _critical_check(self, run_id: str, node, model_id: str, minutes: float, evidence: str,
                              asked: list) -> Optional[str]:
        """A step others wait for that runs far past its expected time is not stuck, but holds the
        whole task: the lead says whether it goes on or stops for a faster model. Asked again after
        each further stretch of that length."""
        expected = loop_turns(node) * settings.minutes_per_turn
        due = expected * settings.critical_overrun_factor * (asked[0] + 1)
        run = self.db.get_run(run_id)
        if not run or minutes < due or not run.supervisor or not run.supervisor.model_id or not self._can_lead():
            return None
        waiting = [item.title for item in run.plan.nodes
                   if item.id in quality.descendants(run, {node.id}) and item.status == NodeStatus.PENDING]
        if not waiting:
            return None
        asked[0] += 1
        try:
            async with self._thinking(run_id, "critical_overrun", node.id):
                reply = await self.ask_lead(run_id, supervisor.critical_overrun_message(
                    node, model_id, minutes, expected, waiting, evidence))
            reason = progress.parse(reply)
        except (asyncio.CancelledError, BudgetExceeded, StepLimitExceeded):
            raise
        except Exception:
            reason = None  # without an answer the attempt goes on
        self.emit(run_id, "node.critical_overrun_check", node_id=node.id, model_id=model_id, minutes=round(minutes),
                  expected_minutes=round(expected), waiting=len(waiting), stop=bool(reason), reason=reason)
        return f"{progress.CRITICAL_OVERRUN}{reason}" if reason else None

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
            except (asyncio.CancelledError, BudgetExceeded, StepLimitExceeded):
                raise
            except Exception:
                reason = None  # without an answer the attempt goes on
            self.emit(run.id, "node.overrun_check", node_id=node.id, model_id=model_id, turns=turns,
                      expected=expected, stop=bool(reason), reason=reason)
            return reason

        return expected, check

    def _reviewer(self, run: ExecutionRun, node, tokens: int):
        return pick_reviewer(list(self.model_lookup.values()), self._configured(run), node.model_id, tokens)

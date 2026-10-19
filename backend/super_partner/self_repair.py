"""Repairing Super Partner itself: an edited and checked copy (platform_repair.py) applied
by a restart, while tasks wait. Mixed into executor.Executor."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import quality
from .config import settings
from .diagnosis import parse as parse_diagnosis
from .models import DiagnosisStatus, NodeStatus, RunStatus
from .platform_repair import RepairError, repair_guide
from .recovery import MAX_ENVIRONMENT_FIXES


class SelfRepair:
    """Uses the executor's run state, locks and diagnosis model calls (_diagnose_with)."""

    def _hold_for_platform(self, run, force: bool = False) -> None:
        if not self.platform_pending or (not force and run.status not in {RunStatus.QUEUED, RunStatus.RUNNING}):
            return
        run.maintenance_id = self.platform_pending["id"]
        run.status = RunStatus.PAUSED
        run.error = "Super Partner 的一项改动已通过验证，正在等待在途步骤结束并自动生效，之后继续任务。"
        self._save(run)
        self.emit(run.id, "run.platform_waiting", repair_id=run.maintenance_id)

    async def _fix_platform(self, run, diagnosis, node, problem: str) -> tuple[str, float]:
        """Prepare source changes separately; only independent successful checks can apply them."""
        latest = self.db.get_run(run.id)
        record = next(d for d in latest.diagnoses if d.id == diagnosis.id)
        record.scope = "super_partner"
        self._save(latest)
        if self.platform_pending:
            record.repair_id = self.platform_pending["id"]
            self._save(latest)
            return "已有公共平台修复通过验证，等待自动生效。\nSTATUS: PLATFORM_PENDING", 0.0
        if node and node.environment_fixes >= MAX_ENVIRONMENT_FIXES:
            raise RepairError("同一步骤的公共修复已达到本轮上限，请结合已有验证结果重新定位，避免重复修改平台。")
        job = await self.platform.prepare(run.id, diagnosis.id, allow_network=latest.allow_network)
        # The platform engineer's page lists it too.
        record_job = self._record_platform_repair(run, diagnosis, node, problem, job)
        latest = self.db.get_run(run.id)
        record = next(d for d in latest.diagnoses if d.id == diagnosis.id)
        record.repair_id = job["id"]
        self._save(latest)
        self.emit(run.id, "run.platform_repair", diagnosis_id=diagnosis.id, repair_id=job["id"], phase="editing")
        replied = sorted((d for d in latest.diagnoses if d.messages), key=lambda d: d.messages[-1].created_at)
        replies = [{"question": d.question, "answers": [m.model_dump(mode="json") for m in d.messages[-5:]]}
                   for d in replied[-5:]]
        base = (
            "[[super-partner:platform-repair]]\n"
            "Fix an evidenced defect in Super Partner itself in this isolated source copy. "
            "The user authorizes this repair. Preserve unrelated existing changes. Modify "
            "application code and add a regression test reproducing the defect; do not weaken "
            "or delete existing tests. Do not change task data, credentials, Git history, live "
            "source, or restart any services. The controller independently runs backend tests, "
            "frontend tests and a build, then applies and activates a passing patch for all projects. "
            "Use file tools to implement the fix, not merely report advice. If a real user choice "
            "is required, use QUESTION, OPTIONS (JSON array), and STATUS: NEEDS_INPUT; otherwise "
            "describe your fix in Chinese and end with STATUS: FIXED.\n"
            "Follow the repair skill below. Start from the diagnosis's LOCATION lines; read only "
            "the code around them.\n\n"
            f"{repair_guide(Path(job['folder']) / 'source')}\n\n"
            f"Original task: {run.prompt}\nDiagnosis: {problem}\n"
            f"User replies: {json.dumps(replies, ensure_ascii=False)}\n"
        )
        feedback, cost = "", 0.0
        try:
            for attempt in range(settings.platform_repair_attempts):
                result, _model_id, extra = await self._diagnose_with(
                    run, diagnosis, node, base + feedback, Path(job["folder"]) / "source", repair=False, edit=True,
                )
                cost += extra
                record_job.model_id = _model_id
                if parse_diagnosis(result.text)[2] in {DiagnosisStatus.NEEDS_INPUT, DiagnosisStatus.BLOCKED}:
                    job.update(status="awaiting_input", report=result.text)
                    self.platform.save(job)
                    record_job.status, record_job.report = "failed", parse_diagnosis(result.text)[0]
                    record_job.error = "修复需要你回答问题，已交回任务的诊断处理。"
                    record_job.cost_usd = round(cost, 6)
                    self._engineer_end(record_job)
                    return result.text, cost
                record_job.status = "validating"
                self._engineer_save(record_job)
                self.emit(run.id, "run.platform_repair", repair_id=job["id"], phase="validating", attempt=attempt + 1)
                try:
                    # The user can change network permission while the candidate is
                    # being edited. Validation follows the current task setting too.
                    current = self.db.get_run(run.id)
                    job["allow_network"] = bool(current and current.allow_network)
                    checks = await self.platform.check(job)
                    break
                except RepairError as exc:
                    if attempt + 1 == settings.platform_repair_attempts:
                        raise
                    feedback = f"\nThe independent check failed. Fix it and recheck:\n{str(exc)[-6000:]}"
            report = parse_diagnosis(result.text)[0]
            report += "\n\n## 自动验证\n" + "\n".join(f"- {check['name']}: 通过" for check in checks)
            report += "\n\n## 影响范围\nSuper Partner 公共代码；生效后供所有项目使用。\n修改：" + "、".join(job["files"])
            job.update(report=report, model_id=_model_id)
            record_job.status, record_job.report, record_job.files = "applying", parse_diagnosis(result.text)[0], job["files"]
            record_job.checks, record_job.diff = [check["name"] for check in checks], self.platform.diff(job)
            record_job.cost_usd = round(cost, 6)
            self._engineer_save(record_job)
            self.platform.schedule(job)
            self.platform_pending = job
            self.maintenance_task = asyncio.create_task(self._activate_platform(job))
            return report + "\nSTATUS: PLATFORM_PENDING", cost
        except BaseException as exc:
            job.update(status="failed", error=str(exc) or "平台修复被取消。")
            self.platform.save(job)
            record_job.status, record_job.error = "failed", job["error"][:2000]
            record_job.cost_usd = round(cost + getattr(exc, "accounted_cost_usd", 0.0), 6)
            self._engineer_end(record_job)
            exc.accounted_cost_usd = getattr(exc, "accounted_cost_usd", 0.0) + cost
            raise

    async def _activate_platform(self, job: dict) -> None:
        try:
            # No active model call is interrupted by our restart; usage and results drain first.
            while self.active_requests or any(not task.done() for task in set(self.tasks.values()) | set(self.diagnosing.values())):
                await asyncio.sleep(0.2)
            # A change the user asked the platform engineer for belongs to no task.
            initiating = self.db.get_run(job["run_id"]) if job.get("run_id") else None
            if job.get("run_id") and (not initiating or initiating.status == RunStatus.CANCELLED):
                job.update(status="cancelled", error="发起诊断的任务已取消或删除，未应用平台改动。")
                self.platform.save(job)
                self._finish_platform(job)
                return
            await self.platform.launch(job)
            await self._watch_platform()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            job.update(status="failed", error=str(exc))
            self.platform.save(job)
            self._finish_platform(job)

    def restore_platform(self) -> None:
        """Called on API startup; ordinary interrupted tasks still require confirmation."""
        pending = self.platform.pending()
        if pending:
            self.platform_pending = pending
            if pending["status"] == "validated":
                pending["old_pid"] = os.getpid()
                self.maintenance_task = asyncio.create_task(self._activate_platform(pending))
            else:
                self.maintenance_task = asyncio.create_task(self._watch_platform())

    async def _watch_platform(self) -> None:
        deadline = time.monotonic() + 420
        while time.monotonic() < deadline:
            job = self.platform.pending()
            if not job:
                return
            if job["status"] in {"activated", "failed", "rolled_back", "cancelled"}:
                self._finish_platform(job)
                return
            await asyncio.sleep(0.5)
        job.update(status="failed", error="平台修复生效超时；任务状态已保留，未宣称修复成功。")
        self.platform.save(job)
        self._finish_platform(job)

    def _finish_platform(self, job: dict) -> None:
        success = job["status"] == "activated"
        ready, to_lead = [], []
        for run in self.db.list_runs():
            records = [d for d in run.diagnoses if d.repair_id == job["id"]]
            held = run.maintenance_id == job["id"]
            if not records and not held:
                continue
            for record in records:
                record.status = "done" if success else "failed"
                record.verdict = DiagnosisStatus.PLATFORM_FIXED if success else DiagnosisStatus.PLATFORM_FAILED
                record.error = None if success else job.get("error", "平台修复未生效。")
                record.report = (record.report or "") + ("\n\n平台修复已生效，服务健康检查通过。" if success
                                                      else f"\n\n平台修复未生效：{record.error}")
            # A repair the lead asked for goes back to it, applied or not, before the step runs again.
            asked = next((record for record in records if record.trigger == "lead_repair" and record.node_id
                          and self._can_lead()), None)
            if held:
                run.maintenance_id = None
                if asked and run.status == RunStatus.PAUSED and not run.waiting_diagnosis_id:
                    to_lead.append((run.id, asked.id))
                    if not success:
                        run.error = job.get("error", "平台修复未生效，任务现场已保留。")
                elif run.status == RunStatus.PAUSED:
                    if (success or (job["status"] == "cancelled" and run.id != job["run_id"])) and not run.waiting_diagnosis_id:
                        required = run.actual_cost_usd + sum(n.estimated_cost_usd for n in run.plan.nodes
                                                           if n.status != NodeStatus.SUCCEEDED)
                        if required <= run.budget_usd:
                            run.status, run.error = RunStatus.QUEUED, None
                            ready.append(run.id)
                        else:
                            self._pause_for_budget(run)
                    else:
                        run.error = job.get("error", "平台修复未生效，任务现场已保留。")
            self._save(run)
            self.emit(run.id, "run.platform_activated" if success else "run.platform_failed",
                      repair_id=job["id"], files=job["files"], error=job.get("error"))
        self.db.save_shared_repair({
            "id": job["id"], "scope": "super_partner", "status": job["status"],
            "run_id": job["run_id"], "model_id": job.get("model_id"), "files": job["files"],
            "checks": job.get("checks", []), "report": job.get("report", ""), "error": job.get("error"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        self.platform.clear_pending(job)
        self.platform_pending = None
        self._engineer_finished(job)
        for run_id in ready:
            self.start(run_id)
        for run_id, diagnosis_id in to_lead:
            asyncio.create_task(self._platform_to_lead(run_id, diagnosis_id))
        self._engineer_kick()

    async def _platform_to_lead(self, run_id: str, diagnosis_id: str) -> None:
        """The platform repair the lead asked for has been applied, or not: it decides what happens
        to the step (leading._after_repair); without a decision the task goes on as before."""
        run = self.db.get_run(run_id)
        diagnosis = next((item for item in run.diagnoses if item.id == diagnosis_id), None) if run else None
        node = next((item for item in run.plan.nodes if item.id == diagnosis.node_id), None) if diagnosis else None
        if not node:
            return
        last = node.attempts[-1] if node.attempts else None
        verdict = quality.Verdict(False, (last.reason if last else None) or "步骤未完成")
        if not await self._after_repair(run_id, node.id, diagnosis_id, verdict, last.outcome if last else "error"):
            async with self.locks.setdefault(run_id, asyncio.Lock()):
                latest = self.db.get_run(run_id)
                if latest.status != RunStatus.PAUSED or diagnosis.verdict != DiagnosisStatus.PLATFORM_FIXED:
                    return
                latest.status, latest.error = RunStatus.QUEUED, None
                self._save(latest)
            self.start(run_id)
            return
        await self._continue_after_answer(run_id)

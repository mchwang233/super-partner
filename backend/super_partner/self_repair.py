"""Repairing Super Partner itself: an edited and checked copy (platform_repair.py) applied
by a restart, while tasks wait. Mixed into executor.Executor."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

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
        run.error = "Super Partner 修复已通过验证，正在等待在途步骤结束并自动生效，之后继续任务。"
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
                if parse_diagnosis(result.text)[2] in {DiagnosisStatus.NEEDS_INPUT, DiagnosisStatus.BLOCKED}:
                    job.update(status="awaiting_input", report=result.text)
                    self.platform.save(job)
                    return result.text, cost
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
            self.platform.schedule(job)
            self.platform_pending = job
            self.maintenance_task = asyncio.create_task(self._activate_platform(job))
            return report + "\nSTATUS: PLATFORM_PENDING", cost
        except BaseException as exc:
            job.update(status="failed", error=str(exc) or "平台修复被取消。")
            self.platform.save(job)
            exc.accounted_cost_usd = getattr(exc, "accounted_cost_usd", 0.0) + cost
            raise

    async def _activate_platform(self, job: dict) -> None:
        try:
            # No active model call is interrupted by our restart; usage and results drain first.
            while self.active_requests or any(not task.done() for task in set(self.tasks.values()) | set(self.diagnosing.values())):
                await asyncio.sleep(0.2)
            initiating = self.db.get_run(job["run_id"])
            if not initiating or initiating.status == RunStatus.CANCELLED:
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
        ready = []
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
            if held:
                run.maintenance_id = None
                if run.status == RunStatus.PAUSED:
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
        for run_id in ready:
            self.start(run_id)

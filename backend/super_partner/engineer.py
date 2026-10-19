"""The platform engineer: changes to Super Partner itself, as jobs (models.EngineerJob,
docs/design/platform-engineer.md). The user asks for a repair or a component; a task's lead may ask
for a component. A component is planned first and the user approves the plan; every change is made
in an isolated copy and checked (platform_repair.py), and applied by a restart (self_repair.py), a
component only once the user approves it. Repairs a task's diagnosis makes stay in that task
(self_repair._fix_platform) and are only recorded here. Mixed into executor.Executor."""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import diagnosis as diagnosis_text
from . import quality, supervisor
from .budget import BudgetExceeded, RequestBudget
from .config import settings
from .models import (
    ENGINEER_DONE,
    ENGINEER_WAITING,
    EngineerJob,
    EngineerMessage,
    NodeStatus,
    RunStatus,
)
from .platform_repair import COMPONENT_GUIDES, REPAIR_GUIDES, RepairError, repair_guide
from .routing import can_act, diagnosers

ENGINEER_TIMEOUT = 1800
# Models tried in turn when one fails (its usage ran out, say).
MAX_ENGINEERS = 3
STATUS = re.compile(r"^\s*STATUS:\s*(PLAN|FIXED|NEEDS_INPUT)\b", re.IGNORECASE | re.MULTILINE)
# A job in one of these states has the engineer at work on it.
WORKING = {"planning", "editing", "validating"}
ROLE = ("You are Super Partner's platform engineer: you change Super Partner itself (its backend, web "
        "UI, tools, skills and model channels), never the user's projects. ")


class EngineerError(ValueError):
    """A request the engineer cannot take now; the message is for the user."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def title_of(request: str) -> str:
    line = next((item.strip(" #-*") for item in request.splitlines() if item.strip()), "")
    return line[:40] + ("…" if len(line) > 40 else "")


def _conversation(job: EngineerJob) -> str:
    return "\n".join(f"{item.role}: {item.text[:2000]}" for item in job.messages[-20:]) or "(none)"


def plan_prompt(job: EngineerJob, guide: str) -> str:
    asker = "The lead of a running task asks for it" if job.origin == "lead" else "The user asks for it"
    return (
        "[[super-partner:platform-engineer:plan]]\n" + ROLE
        + "The working directory is an isolated copy of Super Partner's source. Read it to plan the "
        "change below; do not edit anything yet. Read docs/ARCHITECTURE.md first, then only the code "
        f"you need.\n{asker}:\n{job.request}\n\nConversation so far:\n{_conversation(job)}\n"
        + (f"\nYour previous plan, which the user wants changed:\n{job.plan}\n" if job.plan else "")
        + "\nWrite the plan in Chinese, short, for the user to approve:\n"
        "- 目标: what will work afterwards.\n"
        "- 改动: each file and what changes in it.\n"
        "- 测试: the tests you will add and what they prove.\n"
        "- 风险: what could break, and what is left out.\n"
        "End with `STATUS: PLAN`. If a choice belongs to the user before you can plan, ask instead:\n"
        'QUESTION: <one question>\nOPTIONS: ["<option 1>", "<option 2>"]\nSTATUS: NEEDS_INPUT\n\n'
        + guide
    )


def edit_prompt(job: EngineerJob, guide: str, feedback: str = "") -> str:
    what = (f"Add this component as the approved plan says.\nRequest:\n{job.request}\n\nApproved plan:\n{job.plan}\n"
            if job.kind == "component" else f"Repair this defect in Super Partner:\n{job.request}\n")
    after = (", shows the user the change, and applies it once they approve" if job.kind == "component"
             else ", and applies a passing change")
    return (
        "[[super-partner:platform-engineer:edit]]\n" + ROLE
        + "The working directory is an isolated copy of Super Partner's source; make the change there "
        "with your file tools. Preserve unrelated existing changes. Add tests that prove the change (for "
        "a repair, a regression test that fails without it); never weaken or delete existing tests. Do "
        "not touch task data, credentials, Git history, the live source, or running services. The "
        f"controller then runs the backend tests, frontend tests and build independently{after}.\n\n"
        + what + f"\nConversation so far:\n{_conversation(job)}\n" + feedback
        + "\nWhen done, report in Chinese (改了什么, 测试, 验证) and end with `STATUS: FIXED`. If a real "
        "choice belongs to the user, ask with QUESTION, OPTIONS (a JSON array) and `STATUS: NEEDS_INPUT`.\n\n"
        + guide
    )


def read_reply(text: str) -> tuple[str, Optional[str], Optional[str], list[str]]:
    """The engineer's report, its status (plan | fixed | needs_input), and a question with options."""
    text = text or ""
    found = STATUS.search(text)
    body = text[:found.start()] if found else text
    question, choices = diagnosis_text.interaction(text)
    body = diagnosis_text.QUESTION.sub("", diagnosis_text.OPTIONS.sub("", body)).strip()
    return body, found.group(1).lower() if found else None, question, choices


def commit(root: Path, files: list[str], message: str) -> str:
    """Commit just ``files`` on the checkout's current branch, leaving anything else the user has
    staged alone; the new commit's id."""
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=120,
                              check=False)

    if git("rev-parse", "--is-inside-work-tree").returncode:
        raise RepairError("源码目录不是 git 仓库。")
    for step in (("add", "-A", "--", *files), ("commit", "-m", message, "--", *files)):
        result = git(*step)
        if result.returncode:
            raise RepairError((result.stderr or result.stdout).strip()[-500:] or "git 提交失败。")
    return git("rev-parse", "HEAD").stdout.strip()


class Engineering:
    """Uses the executor's models and providers, the platform copies (self.platform) and their
    activation (self_repair.py), and for components a lead asked for, the lead's decisions
    (leading.py)."""

    engineer_task: Optional[asyncio.Task] = None
    engineer_job_id: Optional[str] = None

    # --- what the user and the API see ----------------------------------------------------------

    def engineer_available(self) -> bool:
        return bool(self.platform.enabled and self.platform.root)

    def engineer_overview(self, limit: int = 30) -> dict:
        return {
            "available": self.engineer_available(),
            "working_on": self.engineer_job_id if self.engineer_task and not self.engineer_task.done() else None,
            "pending": self.platform_pending.get("id") if self.platform_pending else None,
            "jobs": [job.model_dump(mode="json") for job in self.db.engineer_jobs(limit)],
        }

    def _engineer_save(self, job: EngineerJob) -> None:
        job.updated_at = _now()
        self.db.save_engineer_job(job)

    def _engineer_job(self, job_id: str) -> EngineerJob:
        job = self.db.get_engineer_job(job_id)
        if not job:
            raise EngineerError("找不到这项平台工程师工作。")
        return job

    def engineer_request(self, request: str, kind: str = "component", origin: str = "user", *,
                         run_id: Optional[str] = None, node_id: Optional[str] = None,
                         decision_id: Optional[str] = None, budget_usd: Optional[float] = None) -> EngineerJob:
        """A new job: a component is planned first, a repair is made at once. It waits its turn."""
        if not self.engineer_available():
            raise EngineerError("平台工程师不可用：平台自修复未启用，或当前安装没有可编辑的 Super Partner 源码。")
        request = (request or "").strip()
        if not request:
            raise EngineerError("请写明要修复什么，或要添加什么组件。")
        if kind not in {"repair", "component"}:
            raise EngineerError("工作类型只能是修复或添加组件。")
        job = EngineerJob(
            kind=kind, origin=origin, request=request, title=title_of(request),
            phase="plan" if kind == "component" else "edit", run_id=run_id, node_id=node_id,
            decision_id=decision_id,
            budget_usd=settings.engineer_budget_usd if budget_usd is None else budget_usd,
            messages=[EngineerMessage(role="lead" if origin == "lead" else "user", text=request)],
        )
        self._engineer_save(job)
        self._engineer_kick()
        return job

    def engineer_reply(self, job_id: str, text: str, choice: Optional[str] = None) -> EngineerJob:
        """The user answers a question, or asks for a plan or a change to be redone differently."""
        job = self._engineer_job(job_id)
        said = "\n".join(filter(None, [choice, (text or "").strip()]))
        if not said:
            raise EngineerError("请写下你的回复。")
        if job.status not in ENGINEER_WAITING:
            raise EngineerError("平台工程师现在不在等你回复。")
        job.messages.append(EngineerMessage(role="user", text=said))
        if job.status == "plan_ready":
            job.phase = "plan"
        elif job.status == "ready":
            job.phase = "edit"
        job.status, job.question, job.choices = "queued", None, []
        self._engineer_save(job)
        self._engineer_kick()
        return job

    def engineer_approve(self, job_id: str) -> EngineerJob:
        """Approve a plan (the change is then made) or a checked change (it is then applied)."""
        job = self._engineer_job(job_id)
        if job.status == "plan_ready":
            job.messages.append(EngineerMessage(role="system", text="你批准了方案，开始实施。"))
            job.status, job.phase = "queued", "edit"
            self._engineer_save(job)
            self._engineer_kick()
            return job
        if job.status == "ready":
            return self._engineer_apply(job)
        raise EngineerError("这项工作现在没有等你批准的方案或改动。")

    async def engineer_reject(self, job_id: str) -> EngineerJob:
        """The user gives the job up; work in progress stops."""
        job = self._engineer_job(job_id)
        if job.status in ENGINEER_DONE or job.status == "applying":
            raise EngineerError("这项工作已经结束或正在生效，不能放弃。")
        if self.engineer_job_id == job_id and self.engineer_task and not self.engineer_task.done():
            self.engineer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.engineer_task
            job = self._engineer_job(job_id)
        job.messages.append(EngineerMessage(role="system", text="你放弃了这项工作。"))
        job.status = "rejected"
        self._engineer_end(job)
        return job

    def restore_engineer(self) -> None:
        """On API startup: work a restart cut off starts again."""
        for job in self.db.engineer_jobs(200):
            if job.status in WORKING or (job.status == "applying" and not self.platform_pending):
                if job.status == "applying":
                    # The restart applied it or not; the platform record says which.
                    record = self.platform.load(job.repair_id)
                    if record and record.get("status") in {"activated", "failed", "rolled_back", "cancelled"}:
                        self._engineer_finished(record)
                        continue
                job.messages.append(EngineerMessage(role="system", text="服务重启打断了这项工作，重新开始。"))
                job.status = "queued"
                if job.phase == "edit" and job.kind == "component" and not job.plan:
                    job.phase = "plan"
                self._engineer_save(job)
        self._engineer_kick()

    # --- the work ---------------------------------------------------------------------------------

    def _engineer_kick(self) -> None:
        """Start the next queued job if the engineer is free and no change is waiting to apply."""
        if (self.engineer_task and not self.engineer_task.done()) or self.platform_pending:
            return
        queued = sorted((job for job in self.db.engineer_jobs(200) if job.status == "queued"),
                        key=lambda job: job.created_at)
        if not queued:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self.engineer_job_id = queued[0].id
        self.engineer_task = loop.create_task(self._engineer_run(queued[0].id))

    async def _engineer_run(self, job_id: str) -> None:
        job = self._engineer_job(job_id)
        stopped = False
        try:
            if job.phase == "plan":
                await self._engineer_plan(job)
            else:
                await self._engineer_edit(job)
        except asyncio.CancelledError:
            stopped = True
            raise
        except Exception as exc:
            job.cost_usd = round(job.cost_usd + getattr(exc, "accounted_cost_usd", 0.0), 6)
            job.status, job.error = "failed", str(exc)[:2000] or type(exc).__name__
            job.messages.append(EngineerMessage(role="system", text=f"平台工程师没能完成：{job.error[:500]}"))
            self._engineer_end(job)
        finally:
            if self.engineer_task is asyncio.current_task():
                self.engineer_task, self.engineer_job_id = None, None
            if not stopped:
                self._engineer_kick()

    async def _engineer_plan(self, job: EngineerJob) -> None:
        job.status = "planning"
        self._engineer_save(job)
        copy = await self.platform.prepare(job.run_id, None)
        try:
            source = Path(copy["folder"]) / "source"
            text = await self._engineer_call(job, plan_prompt(job, repair_guide(source, COMPONENT_GUIDES)), source)
        finally:
            shutil.rmtree(copy["folder"], ignore_errors=True)
        report, status, question, choices = read_reply(text)
        if status == "needs_input" and question:
            self._engineer_ask(job, report, question, choices)
            return
        job.plan, job.status = report, "plan_ready"
        job.messages.append(EngineerMessage(role="engineer", text="方案写好了，请看过后批准，或回复要改的地方。"))
        self._engineer_save(job)

    def _engineer_ask(self, job: EngineerJob, report: str, question: str, choices: list[str]) -> None:
        job.status, job.question, job.choices = "needs_input", question, choices
        job.messages.append(EngineerMessage(role="engineer", text="\n\n".join(filter(None, [report, question]))))
        self._engineer_save(job)

    async def _engineer_edit(self, job: EngineerJob) -> None:
        job.status = "editing"
        self._engineer_save(job)
        copy = self.platform.load(job.repair_id)
        if not copy or copy.get("status") not in {"editing", "validating", "validated"} or self.platform.stale(copy):
            # A first edit, or the live source has changed since: a fresh copy, so applying it
            # cannot overwrite what changed.
            copy = await self.platform.prepare(job.run_id, None)
            copy["engineer_job_id"] = job.id
            self.platform.save(copy)
            job.repair_id = copy["id"]
            self._engineer_save(job)
        source = Path(copy["folder"]) / "source"
        guide = repair_guide(source, COMPONENT_GUIDES if job.kind == "component" else REPAIR_GUIDES)
        feedback = ""
        for attempt in range(settings.platform_repair_attempts):
            text = await self._engineer_call(job, edit_prompt(job, guide, feedback), source)
            report, status, question, choices = read_reply(text)
            if status == "needs_input" and question:
                self._engineer_ask(job, report, question, choices)
                return
            job.status = "validating"
            self._engineer_save(job)
            try:
                checks = await self.platform.check(copy)
                break
            except RepairError as exc:
                if attempt + 1 == settings.platform_repair_attempts:
                    raise
                feedback = f"\nThe independent check failed. Fix it and recheck:\n{str(exc)[-6000:]}\n"
                job.status = "editing"
                self._engineer_save(job)
        job.files, job.checks = copy["files"], [item["name"] for item in checks]
        job.diff, job.report, job.error = self.platform.diff(copy), report, None
        job.messages.append(EngineerMessage(role="engineer", text=report or "改好了，检查全部通过。"))
        if job.kind == "component":
            job.status = "ready"
            self._engineer_save(job)
            return
        # A repair applies on its own once checked, after any change already waiting to apply.
        while self.platform_pending:
            await asyncio.sleep(1)
        self._engineer_apply(job)

    async def _engineer_call(self, job: EngineerJob, prompt: str, workspace: Path) -> str:
        """One piece of the engineer's work on the strongest model that can edit files (membership
        CLIs first), the next one when it fails; charged to the job's own budget."""
        configured = getattr(self.providers, "configured_models", None)
        configured = configured(list(self.model_lookup)) if configured else set()
        candidates = [model for model in diagnosers(list(self.model_lookup.values()), configured)
                      if can_act(model)][:MAX_ENGINEERS]
        if not candidates:
            raise RepairError("没有可用、并且能修改文件的模型来做这项工作。")
        last: Optional[Exception] = None
        for model in candidates:
            job.model_id = model.id
            self._engineer_save(job)
            try:
                result = await self._engineer_charged(job, model.id, lambda budget, model=model: self.providers.complete(
                    model.id, prompt, workspace, spend_ok=budget, cli_timeout=ENGINEER_TIMEOUT,
                    allow_api_fallback=False, act=model.channel != "cli", repair=False))
                return result.text
            except asyncio.CancelledError:
                raise
            except BudgetExceeded:
                raise
            except Exception as exc:
                last = exc
                job.messages.append(EngineerMessage(role="system", text=f"{model.id} 没能完成，换下一个模型：{str(exc)[:200]}"))
                self._engineer_save(job)
        raise RepairError(f"所有可用模型都没能完成：{last}")

    async def _engineer_charged(self, job: EngineerJob, model_id: str, call):
        async def charge(cost):
            if cost:
                job.cost_usd = round(job.cost_usd + cost, 6)
                self._engineer_save(job)

        budget = RequestBudget(self.model_lookup, lambda: job.budget_usd - job.cost_usd, {}, charge, asyncio.Event())
        try:
            result = await call(budget)
            await charge(max(0.0, self._actual_cost(result, None, model_id) - budget.spent))
            return result
        except (Exception, asyncio.CancelledError) as exc:
            usage = getattr(exc, "usage", None)
            if usage:
                await charge(max(0.0, self._actual_cost(usage, None, model_id) - budget.spent))
            raise
        finally:
            budget.release()

    # --- applying, and what follows ---------------------------------------------------------------

    def _engineer_apply(self, job: EngineerJob) -> EngineerJob:
        """Apply a checked change by a restart (self_repair._activate_platform)."""
        if self.platform_pending:
            raise EngineerError("已有一项平台改动正在等待生效，请等它完成后再应用。")
        copy = self.platform.load(job.repair_id)
        if not copy or copy.get("status") != "validated":
            raise EngineerError("这份改动还没有通过检查，不能应用。")
        if self.platform.stale(copy):
            job.messages.append(EngineerMessage(role="system", text=(
                "Super Partner 的源码在这期间有了更新，直接应用会覆盖它。平台工程师会在新的副本上"
                "按同样的要求重做，检查通过后" + ("再请你确认。" if job.kind == "component" else "自动生效。"))))
            job.status, job.phase = "queued", "edit"
            self._engineer_save(job)
            self._engineer_kick()
            return job
        copy.update(report=job.report or "", model_id=job.model_id, engineer_job_id=job.id)
        job.status = "applying"
        job.messages.append(EngineerMessage(role="system", text="开始生效：等进行中的模型调用结束后重启服务。"))
        self._engineer_save(job)
        self.platform.schedule(copy)
        self.platform_pending = copy
        self.maintenance_task = asyncio.create_task(self._activate_platform(copy))
        return job

    def _engineer_finished(self, record: dict) -> None:
        """A platform change has been applied or not (self_repair._finish_platform): update the
        job it belongs to, and commit an applied change."""
        job = self.db.get_engineer_job(record.get("engineer_job_id") or "")
        if not job or job.status in ENGINEER_DONE:
            return
        job.files = record.get("files") or job.files
        if record.get("status") == "activated":
            job.status = "applied"
            note = "已生效，服务健康检查通过。"
            if settings.engineer_commit and job.files:
                try:
                    job.commit = commit(Path(record["root"]), job.files,
                                        f"{job.title or '平台改动'}\n\nMade by Super Partner's platform engineer (job {job.id}).")
                    note += f"已提交到 git：{job.commit[:8]}（没有推送）。"
                except (RepairError, OSError, subprocess.SubprocessError) as exc:
                    note += f"没有提交到 git：{exc}"
            job.messages.append(EngineerMessage(role="system", text=note))
        else:
            job.status, job.error = "failed", record.get("error") or "改动没有生效。"
            job.messages.append(EngineerMessage(role="system", text=f"没有生效：{job.error}"))
        self._engineer_end(job)

    def _engineer_end(self, job: EngineerJob) -> None:
        job.finished_at = _now()
        self._engineer_save(job)
        if job.kind == "component" and job.origin == "lead" and job.run_id and job.node_id:
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().create_task(self._engineer_to_lead(job.id))

    async def _engineer_to_lead(self, job_id: str) -> None:
        """A component a task's lead asked for is done with (applied, failed or given up): the lead
        decides what happens to the step that waited for it."""
        job = self._engineer_job(job_id)
        async with self.locks.setdefault(job.run_id, asyncio.Lock()):
            run = self.db.get_run(job.run_id)
            if not run or run.engineer_job_id != job.id or run.status == RunStatus.CANCELLED:
                return
            run.engineer_job_id = None
            self._save(run)
        node = next((item for item in run.plan.nodes if item.id == job.node_id), None)
        if not node:
            return
        last = node.attempts[-1] if node.attempts else None
        verdict = quality.Verdict(False, (last.reason if last else None) or "步骤未完成")
        decision = None
        if self._can_lead() and min(self._decisions_left(run, node)) > 0:
            choices = self._choices(run, node)
            decision = await self._ask_for_decision(
                run.id, node.id, supervisor.engineer_message(run, node, job, choices, self._decisions_left(run, node)),
                {item.id for item in choices}, event="engineer_done")
        if decision == "budget":
            return
        if decision is None:
            async with self.locks.setdefault(run.id, asyncio.Lock()):
                latest = self.db.get_run(run.id)
                latest.error = (f"平台工程师的工作已结束（{job.status}），负责人暂时无法决定下一步。"
                                "看过后点“按此预算继续”。")
                self._save(latest)
            return
        await self._apply_decision(run.id, node.id, decision, verdict, last.outcome if last else "error",
                                   "engineer_done")
        await self._continue_after_answer(run.id)

    def _engineer_for_lead(self, run, node, request: str, decision_id: str) -> Optional[EngineerJob]:
        """The lead asks for a component (called holding the run's lock): the task waits for it.
        None if the engineer cannot take it."""
        try:
            job = self.engineer_request(request, "component", "lead", run_id=run.id, node_id=node.id,
                                        decision_id=decision_id)
        except EngineerError:
            return None
        node.status = NodeStatus.PENDING
        if run.status in {RunStatus.RUNNING, RunStatus.QUEUED}:
            run.status = RunStatus.PAUSED
        run.engineer_job_id = job.id
        run.error = "负责人请平台工程师添加一个组件。请在“平台工程师”页面看过方案后批准，组件生效后任务继续。"
        return job

    def _record_platform_repair(self, run, diagnosis, node, problem: str, copy: dict) -> EngineerJob:
        """A task's platform repair (self_repair._fix_platform), recorded as an engineer job."""
        job = EngineerJob(
            kind="repair", origin="lead" if diagnosis.trigger == "lead_repair" else "diagnosis",
            request=problem or "任务的诊断发现 Super Partner 自身有缺陷。", status="editing", phase="edit",
            run_id=run.id, node_id=node.id if node else None, decision_id=diagnosis.decision_id,
            diagnosis_id=diagnosis.id, repair_id=copy["id"], budget_usd=0.0,
        )
        job.title = title_of(job.request)
        job.messages.append(EngineerMessage(role="system", text="任务「" + run.prompt[:60] + "」的修复，花费记在该任务上。"))
        self._engineer_save(job)
        copy["engineer_job_id"] = job.id
        self.platform.save(copy)
        return job

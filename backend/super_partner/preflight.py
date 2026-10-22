"""The check before a task's first step: is everything the task needs here and working?

Problems with the machine used to show only when a step failed on them, after it had spent
turns and money: no simulator, an unreachable license daemon, a sandbox whose commands printed
nothing. Before the first step this module runs, in a real step sandbox with the task's own
settings (internet or not, bridged ports):

- the sandbox itself: output, errors and the exit code come back, the working directory and
  /tmp can be written;
- the tools the plan needs: the commands the lead listed (``TaskPlan.requires``); the programs
  the steps' checks call are looked for too, as a warning only (many are built by the task);
- the internet, if the task allows it (a warning only);
- each API model the plan uses, with a tiny request (a warning only: a missing one is replaced).

A failing blocking check gets one environment repair, then the checks run again; if they still
fail the task pauses with what is missing. Going on after that runs the steps anyway.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import agent as agent_tools
from . import environment, retrospect, skills
from .config import settings
from .diagnosis import parse as parse_diagnosis
from .models import ExecutionRun, NodeStatus, Preflight, PreflightCheck, RunStatus, TaskNode
from .routing import route_node

# The sandbox check: every line must come back, in order, with the exit code.
SANDBOX_COMMAND = ("echo sp-out; echo sp-err >&2; printf x > .sp-preflight && rm .sp-preflight && echo sp-write; "
                   "printf sp-tmp > /tmp/.sp-preflight && cat /tmp/.sp-preflight && echo; id -u; exit 7")


def sandbox_expected() -> str:
    """What the sandbox check prints when all is well: the step is the user, not root."""
    return f"Exit code 7\nsp-out\nsp-err\nsp-write\nsp-tmp\n{os.getuid()}"
# Programs that are part of every shell, not tools a task brings.
BUILTIN = {"cd", "echo", "test", "true", "false", "exit", "export", "source", "printf", "set", "read", "wait"}
MAX_PROGRAMS = 12
# Shorter words found in a check command are pieces of it (sed or awk expressions), not programs.
MIN_PROGRAM = 3
OUTPUT_KEPT = 600


def programs(run: ExecutionRun) -> list[str]:
    """Programs the steps' check commands call that the task does not make itself."""
    made = {Path(name).name for node in run.plan.nodes for name in node.files}
    found = []
    for node in run.plan.nodes:
        for part in (node.check or "").split("&&"):
            name = retrospect.program(part)
            if (name and len(name) >= MIN_PROGRAM and name not in BUILTIN and name not in made
                    and "." not in name.strip(".")):
                found.append(name)
    return list(dict.fromkeys(found))[:MAX_PROGRAMS]


def tool_commands(run: ExecutionRun) -> list[tuple[str, bool]]:
    """The commands that check the task's tools, and whether each blocks: the lead's do; then
    ``command -v`` for each program a step's check calls that the lead did not already name,
    which only warns, since a step may build that program itself."""
    listed = list(run.plan.requires)
    named = {retrospect.program(command) for command in listed}
    return [(command, True) for command in listed] + [
        (f"command -v {shlex.quote(name)}", False) for name in programs(run) if name not in named]


async def sandbox_checks(run: ExecutionRun, workspace: Path) -> list[PreflightCheck]:
    """The checks that run in a step's sandbox: the sandbox itself, the tools, the internet."""
    sandbox = agent_tools.Workspace(workspace, network=run.allow_network)
    timeout = settings.preflight_command_timeout * 1000
    checks = []
    try:
        if not agent_tools.sandbox_available():
            return [PreflightCheck(name="步骤沙箱", ok=False, output="没有安装 bubblewrap (bwrap)，API 模型的步骤无法运行命令")]
        output = await sandbox.call("Bash", {"command": SANDBOX_COMMAND, "timeout": timeout})
        checks.append(PreflightCheck(
            name="步骤沙箱：命令的输出、错误输出、退出码，写工作目录和 /tmp，以用户本人（不是 root）运行",
            command="(内置)", ok=output.strip() == sandbox_expected(), output=output[:OUTPUT_KEPT]))
        for command, blocking in tool_commands(run):
            output, ok = await sandbox.run("Bash", {"command": command, "timeout": timeout})
            checks.append(PreflightCheck(name="工具" if blocking else "步骤检查命令用到的程序", command=command,
                                         ok=ok, blocking=blocking, output=output[:OUTPUT_KEPT]))
        if run.allow_network:
            url = shlex.quote(settings.preflight_network_url)
            # Only whether it answers: a HEAD request, not the page.
            command = (f"curl -sS -I -o /dev/null -m 20 {url} 2>&1 || python3 -c "
                       "'import sys, urllib.request; urllib.request.urlopen(urllib.request.Request("
                       f"sys.argv[1], method=\"HEAD\"), timeout=20)' {url}")
            output, ok = await sandbox.run("Bash", {"command": command, "timeout": 60_000})
            checks.append(PreflightCheck(name="互联网（任务允许联网）", command=f"访问 {settings.preflight_network_url}",
                                         ok=ok, blocking=False, output=output[:OUTPUT_KEPT]))
    finally:
        sandbox.close()
    return checks


# Tools every step knows how to use: no skill is prepared for them.
COMMON_TOOLS = {"python", "python3", "pip", "pip3", "make", "gcc", "g++", "cc", "clang", "bash", "sh", "git",
                "perl", "node", "npm", "java", "sed", "awk", "grep", "find", "ls", "cat", "curl", "wget", "tar",
                "true", "env", "which", "command"}


def needed_tools(run: ExecutionRun, library: list) -> list[str]:
    """The special tools the task needs: those its checks call (the lead's and the steps'), and
    those of a skill in the library that the request or the plan names."""
    found = [retrospect.program(command) for command in run.plan.requires] + programs(run)
    text = "\n".join([run.prompt, run.plan.summary, *(f"{part.title} {part.goal} {part.provides}"
                                                     for part in run.plan.parts)])
    found += [tool for skill in library for tool in skill.tools if skills.names_tool(text, tool)]
    return [tool for tool in dict.fromkeys(item for item in found if item) if tool not in COMMON_TOOLS]


def tool_groups(tools: list[str], library: list) -> list[tuple[str, list[str], Optional[object]]]:
    """One skill to prepare per general skill that covers some of ``tools`` (named after it), and
    one per tool no general skill covers: (name, tools, general skill or None)."""
    groups, left = [], list(tools)
    for skill in library:
        if skill.machine:
            continue
        covered = [tool for tool in left if tool in skill.tools]
        if covered:
            groups.append((skill.name, list(dict.fromkeys(covered + skill.tools)), skill))
            left = [tool for tool in left if tool not in covered]
    return groups + [(tool, [tool], None) for tool in left]


def _with_tools(text: str, tools: list[str]) -> str:
    """A SKILL.md whose front matter lists at least ``tools``."""
    skill = skills.parse(text)
    listed = list(dict.fromkeys([*(skill.tools if skill else []), *tools]))
    head, _, rest = text.partition("\n---")
    lines = [line for line in head.splitlines() if not line.strip().startswith("tools:")]
    return "\n".join([*lines, f"tools: [{', '.join(listed)}]"]) + "\n---" + rest


def skill_prompt(run: ExecutionRun, name: str, tools: list[str], general, checks: list[PreflightCheck]) -> str:
    """Ask for this machine's skill for ``tools``, before the task's steps need them."""
    verified = [check for check in checks if check.ok and check.command and any(
        skills.names_tool(check.command, tool) for tool in tools)]
    return "\n".join([
        (f"Before the steps of a task start, write this machine's skill for {', '.join(tools)}: how a step "
         "uses them here, so that no step has to find it out again. You work in a scratch folder of the "
         "task's working directory, in the same sandbox as the steps (only the working directory and /tmp "
         "writable, the environment set up as for every step). Do not do the task itself."),
        f"The task: {run.plan.summary or run.prompt[:600]}",
        "",
        ("Try, on a tiny example of your own, each thing the task's steps will do with these tools (for "
         "a simulator: compile, run with a seed, collect and report coverage, check assertions). Keep "
         "only what you ran successfully, with the exact commands and options, what they print when they "
         "work, the errors you met and what they mean, where reports land. Be brief: commands and facts."),
        *(["", "Commands already checked here:"] + [f"- `{check.command[:300]}`\n  {check.output.strip()[:300]}"
                                                   for check in verified] if verified else []),
        *(["", f"The general skill ({general.name}); correct what does not hold here:", general.body[:6000]]
          if general else []),
        "",
        ("Reply with only the SKILL.md, no fences: a front matter block, then Markdown:\n"
         f"---\nname: {name}\ndescription: <one line: what it covers, on this machine>\n"
         f"tools: [{', '.join(tools)}]\n---\n# ...\n"
         "No secrets, no licence keys, no user names; paths and environment scripts are fine."),
    ])


def failures(checks: list[PreflightCheck], blocking: bool = True) -> list[PreflightCheck]:
    return [check for check in checks if not check.ok and check.blocking == blocking]


def describe(checks: list[PreflightCheck]) -> str:
    return "\n".join(f"- {check.name}{f'：`{check.command}`' if check.command and check.command != '(内置)' else ''}"
                     f"\n  {check.output.strip()[:300] or '(没有输出)'}" for check in checks)


def repair_prompt(run: ExecutionRun, failed: list[PreflightCheck], workspace: Path, tools: Path) -> str:
    """Ask a diagnoser to repair what the checks before the first step found missing."""
    return "\n".join([
        ("Before the first step of a multi-model task runs, these checks failed in the steps' sandbox "
         "(a bubblewrap sandbox: the system read-only, the home folder hidden, only the working directory "
         "writable, the environment cleared" + (", the internet allowed" if run.allow_network else ", no network")
         + "). Repair the environment so they pass. Do not do the task's work and do not change its files."),
        f"Task: {run.prompt[:1500]}",
        f"Working directory of the task: {workspace}",
        "",
        "# Failed checks",
        describe(failed),
        "",
        "# How to repair",
        (f"Make repairs visible to every step through the shared tools folder {tools}: executables or "
         f"symlinks in {tools}/bin (first on PATH), Python packages with `pip install --target {tools}/python`, "
         f"environment variables and local TCP ports a sandboxed command must reach in {tools}/sandbox.json "
         '(`{"env": {"NAME": "value"}, "host_ports": [26585]}`; keep what is there). Your commands run on the '
         "host as the user, with network access, without root. A check of Super Partner's own sandbox (its "
         "output or exit code) that fails is a defect of Super Partner itself: say so and do not try to fix it."),
        "Then run each failed check again the way a step will, and report the result.",
        "",
        ("Answer in Chinese Markdown: what was missing, what you changed (paths), the checks you ran and "
         "their results. End with `STATUS: FIXED` if the checks now pass, otherwise `STATUS: BLOCKED` and "
         "`USER_ACTION: <what the user must do>`."),
    ])


class Preflighting:
    """The check before the first step, mixed into Executor."""

    async def _preflight(self, run_id: str) -> bool:
        """Check, repair once, check again; whether the steps may start. A task the user went on
        with after a failed check is not stopped again."""
        run = self.db.get_run(run_id)
        if not settings.preflight or not run or (run.preflight and run.preflight.passed):
            return True
        if not any(node.status == NodeStatus.PENDING for node in run.plan.nodes) and not run.plan.parts:
            return True
        overruled = bool(run.preflight and run.preflight.paused)
        self.emit(run_id, "run.preflight_started")
        workspace = Path(run.project_dir) if run.project_dir else settings.workspace_root / run.folder_id
        workspace.mkdir(parents=True, exist_ok=True)
        checks = await sandbox_checks(run, workspace) + await self._model_checks(run)
        repaired = bool(run.preflight and run.preflight.repaired)
        if failures(checks) and not repaired and not overruled:
            repaired = True
            if await self._preflight_repair(run_id, failures(checks), workspace):
                run = self.db.get_run(run_id)
                checks = await sandbox_checks(run, workspace) + await self._model_checks(run)
        failed = failures(checks)
        prepared = [] if failed and not overruled else await self._prepare_skills(run_id, workspace, checks)
        run = self.db.get_run(run_id)
        run.preflight = Preflight(passed=not failed, checks=checks, repaired=repaired,
                                  paused=bool(failed) and not overruled, skills=prepared)
        if failed and not overruled:
            run.status = RunStatus.PAUSED
            run.error = ("执行前检查没有通过，任务还没开始执行，没有花在步骤上：\n" + describe(failed)
                         + "\n装好或修好后点“继续”会重新检查；确定不需要这些，也可以直接点“继续”，任务会照常执行。")
        self._save(run)
        self.emit(run_id, "run.preflight", passed=not failed, overruled=bool(failed) and overruled,
                  failed=[check.command or check.name for check in failed],
                  warnings=[check.command or check.name for check in failures(checks, blocking=False)])
        if failed and not overruled:
            self.emit(run_id, "approval.required", reason="preflight_failed")
            return False
        return True

    async def _prepare_skills(self, run_id: str, workspace: Path, checks: list[PreflightCheck]) -> list[str]:
        """This machine's skill for each tool the task needs: kept from an earlier task, or written
        now by a model trying the tool in a step's sandbox. Returns the skills' names; one that
        cannot be prepared is left out (the steps then find the tool out themselves)."""
        if not settings.preflight_skills:
            return []
        run = self.db.get_run(run_id)
        library = skills.library()
        wanted = await self._available(run, workspace, needed_tools(run, library))
        ready, prepared, failed = [], [], []
        for group in tool_groups(wanted, library):
            name, tools, general = group
            if mine := [skill for skill in skills.for_tools(tools, library) if skill.machine]:
                ready += [skill.name for skill in mine]
                continue
            try:
                text = await asyncio.wait_for(self._write_skill(run, workspace, name, tools, general, checks),
                                              settings.skill_preparation_timeout)
                skill = skills.parse(text)
                if not skill or not skill.body:
                    raise ValueError("the reply was not a SKILL.md")
                skills.save_machine(name, _with_tools(text, tools))
                prepared.append(name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed.append(f"{name}: {str(exc)[:200] or type(exc).__name__}")
        self.emit(run_id, "run.skills_prepared", prepared=prepared, reused=ready, failed=failed)
        return list(dict.fromkeys(ready + prepared))

    async def _available(self, run: ExecutionRun, workspace: Path, tools: list[str]) -> list[str]:
        """The tools a step's sandbox can run (``command -v``)."""
        if not tools or not agent_tools.sandbox_available():
            return []
        sandbox = agent_tools.Workspace(workspace, network=run.allow_network)
        try:
            output, _ok = await sandbox.run("Bash", {"command": "; ".join(
                f"command -v {shlex.quote(tool)} >/dev/null && echo {shlex.quote(tool)}" for tool in tools)})
        finally:
            sandbox.close()
        found = set(output.split())
        return [tool for tool in tools if tool in found]

    async def _write_skill(self, run: ExecutionRun, workspace: Path, name: str, tools: list[str],
                           general: Optional[skills.Skill], checks: list[PreflightCheck]) -> str:
        """Have a model that works through tools try ``tools`` in a scratch folder of the task's
        workspace (the steps' sandbox) and write what holds here as a SKILL.md."""
        node = TaskNode(id="skill", title=f"Skill: {name}", instructions="", capability="coding",
                        difficulty="normal", needs_workspace=True)
        route_node(node, list(self.model_lookup.values()), self._configured(run))
        scratch = workspace / f".skill-{name}"
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            result = await self._complete(run, None, node.model_id, skill_prompt(run, name, tools, general, checks),
                                          scratch, act=True, effort="medium")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        return result.text

    async def _model_checks(self, run: ExecutionRun) -> list[PreflightCheck]:
        """A tiny request to each API model the plan's steps are routed to (and the lead's, if
        it is one); a model that does not answer is a warning, since another takes its place."""
        wanted = {node.model_id for node in run.plan.nodes if node.model_id}
        if run.supervisor and run.supervisor.model_id:
            wanted.add(run.supervisor.model_id)
        checks = []
        for model_id in sorted(wanted):
            if model_id.startswith(("cli:", "mock:")):
                continue
            try:
                await asyncio.wait_for(self._complete(run, None, model_id, "Reply with just: OK", None, effort="low"),
                                       settings.preflight_command_timeout)
                checks.append(PreflightCheck(name="模型", command=model_id, blocking=False))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                checks.append(PreflightCheck(name="模型", command=model_id, ok=False, blocking=False,
                                             output=str(exc)[:OUTPUT_KEPT] or type(exc).__name__))
        return checks

    async def _preflight_repair(self, run_id: str, failed: list[PreflightCheck], workspace: Path) -> bool:
        """One environment repair for the failed checks, by the diagnosers; whether it ran."""
        run = self.db.get_run(run_id)
        diagnosis_id = self.begin_diagnosis(run, "preflight")
        if diagnosis_id is None:
            return False
        self._save(run)
        diagnosis = next(item for item in run.diagnoses if item.id == diagnosis_id)
        if diagnosis.status == "failed":
            return False
        self.emit(run_id, "run.diagnosis_started", diagnosis_id=diagnosis_id, trigger="preflight", model_id=diagnosis.model_id)
        report: Optional[str] = None
        error: Optional[str] = None
        cost = 0.0
        try:
            async with self.repair_lock:
                tools = environment.prepare()
                result, _model, cost = await self._diagnose_with(
                    run, diagnosis, None, repair_prompt(run, failed, workspace, tools), workspace, repair=True)
                report = result.text
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = str(exc)[:500] or type(exc).__name__
        run = self.db.get_run(run_id)
        record = next(item for item in run.diagnoses if item.id == diagnosis_id)
        record.status, record.error, record.cost_usd = ("failed", error, cost) if error else ("done", None, cost)
        record.finished_at = datetime.now(timezone.utc)
        if report is not None:
            record.report, record.advice, record.verdict = parse_diagnosis(report)
        self._save(run)
        self.emit(run_id, "run.diagnosed", diagnosis_id=diagnosis_id, status=record.status,
                  model_id=record.model_id, verdict=record.verdict)
        return error is None

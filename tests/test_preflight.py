"""Before a task's first step, what it needs is checked in a real step sandbox, so a missing tool
or a broken sandbox is found before any work is paid for (preflight.py)."""
import json

import pytest
from super_partner import agent, outline, preflight
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import ExecutionRun, NodeStatus, RunStatus, TaskNode, TaskPlan
from super_partner.providers import ProviderResult
from super_partner.quota import ModelUnavailable
from test_turns import model

needs_bwrap = pytest.mark.skipif(not agent.sandbox_available(), reason="bubblewrap is not installed")
MODELS = {item.id: item for item in [model("x:flash", 90), model("y:gone", 90), model("cli:claude", 96, "cli")]}
MISSING = "command -v no-such-tool-for-preflight"


@pytest.fixture(autouse=True)
def preflight_on(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "preflight", True)
    monkeypatch.setattr(settings, "review_nodes", False)
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")


class Machine:
    """Models for the steps, the repair and the checks' tiny requests; y:gone never answers."""

    def __init__(self):
        self.steps, self.repairs, self.pings = [], [], []

    def configured_models(self, ids):
        return set(ids)

    async def complete(self, model_id, prompt, workspace=None, **kwargs):
        if model_id == "y:gone":
            raise ModelUnavailable(model_id, None, "y:gone 服务暂时不可用")
        if prompt == "Reply with just: OK":
            self.pings.append(model_id)
            return ProviderResult("OK", 1, 1, model_id)
        if "these checks failed" in prompt:
            self.repairs.append(prompt)
            return ProviderResult("找不到这个工具，需要用户安装。\nSTATUS: BLOCKED\nUSER_ACTION: 安装它", 0, 0, model_id)
        self.steps.append(prompt)
        return ProviderResult("done", 1, 1, model_id)


def task(database, requires=(), model_id="x:flash") -> ExecutionRun:
    node = TaskNode(id="n", title="编译", instructions="编译它", capability="coding", model_id=model_id)
    run = ExecutionRun(id="r", prompt="写一个模块并编译", status=RunStatus.QUEUED, budget_usd=1,
                       plan=TaskPlan(summary="s", nodes=[node], requires=list(requires)))
    database.save_run(run)
    return run


def test_the_tools_checked_are_the_leads_and_those_the_checks_call():
    node = TaskNode(id="n", title="t", instructions="i", capability="coding", files=["sim/run.sh"],
                    check="cd sim && iverilog -g2012 -o a tb.v && ./run.sh && make test")
    run = ExecutionRun(id="r", prompt="p", status=RunStatus.QUEUED, budget_usd=1, plan=TaskPlan(summary="s", nodes=[node], requires=["vcs -ID", "make --version"]))
    # The lead's commands block; a program a check calls only warns (the task may build it), and
    # the task's own script and what the lead already named are left out.
    assert preflight.tool_commands(run) == [("vcs -ID", True), ("make --version", True), ("command -v iverilog", False)]


async def test_the_outline_names_the_commands_that_check_the_tools():
    async def say(message):
        long_probe = "python3 -c '" + "x = 1\n" * 1000 + "'"  # cut short, it would not even parse
        multi_line = "python3 -c 'import sys\nprint(sys.version)\n' && echo " + "y" * 400
        return json.dumps({"summary": "FIFO", "requires": ["make --version", "make --version", "", long_probe, multi_line], "parts": [
            {"key": "p1", "title": "RTL", "goal": "g", "provides": "rtl/fifo.v", "size": 2}]})
    plan = await outline.plan_outline(say, "")
    assert plan.requires[0] == "make --version" and len(plan.requires) == 2
    assert plan.requires[1].startswith("python3 -c 'import sys") and plan.requires[1].endswith("y" * 400)


@needs_bwrap
async def test_a_working_sandbox_and_tool_pass_and_a_missing_tool_fails(tmp_path):
    run = ExecutionRun(id="r", prompt="p", status=RunStatus.QUEUED, budget_usd=1, plan=TaskPlan(summary="s", nodes=[], requires=["sh -c 'exit 0'", MISSING]))
    checks = await preflight.sandbox_checks(run, tmp_path)
    assert [check.ok for check in checks] == [True, True, False]
    assert checks[0].name.startswith("步骤沙箱") and "Exit code 1" in checks[2].output


@needs_bwrap
async def test_a_missing_tool_is_repaired_once_then_the_task_waits_before_any_step(tmp_path):
    database = Database(tmp_path / "t.db")
    task(database, requires=[MISSING])
    machine = Machine()
    executor = Executor(database, machine, MODELS)
    await executor.execute("r")
    run = database.get_run("r")
    assert run.status == RunStatus.PAUSED and machine.steps == []
    assert len(machine.repairs) == 1 and MISSING in machine.repairs[0]
    assert MISSING in run.error and run.preflight.passed is False and run.preflight.repaired
    assert [event.data["reason"] for event in database.events("r") if event.kind == "approval.required"] == ["preflight_failed"]
    # The user goes on anyway: checked again, not repaired again, and the steps run.
    run.status = RunStatus.QUEUED
    database.save_run(run)
    await executor.execute("r")
    run = database.get_run("r")
    assert run.status == RunStatus.SUCCEEDED and len(machine.steps) == 1 and len(machine.repairs) == 1
    assert [event.data["overruled"] for event in database.events("r") if event.kind == "run.preflight"] == [False, True]


@needs_bwrap
async def test_a_task_whose_checks_pass_runs_and_an_unanswering_model_is_only_a_warning(tmp_path):
    database = Database(tmp_path / "t.db")
    run = task(database, requires=["sh -c 'exit 0'"])
    run.plan.nodes.append(TaskNode(id="m", title="另一步", instructions="i", capability="coding", model_id="y:gone",
                                   status=NodeStatus.SUCCEEDED))
    database.save_run(run)
    machine = Machine()
    await Executor(database, machine, MODELS).execute("r")
    run = database.get_run("r")
    assert run.status == RunStatus.SUCCEEDED and run.preflight.passed and machine.repairs == []
    assert machine.pings == ["x:flash"]
    [warning] = preflight.failures(run.preflight.checks, blocking=False)
    assert warning.command == "y:gone" and "不可用" in warning.output
    # Checked once: going on with the task later does not check again.
    assert sum(event.kind == "run.preflight" for event in database.events("r")) == 1


async def test_preflight_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "preflight", False)
    database = Database(tmp_path / "t.db")
    task(database, requires=[MISSING])
    machine = Machine()
    await Executor(database, machine, MODELS).execute("r")
    assert database.get_run("r").status == RunStatus.SUCCEEDED and database.get_run("r").preflight is None


def test_the_repair_is_told_what_failed_and_how_steps_run(tmp_path):
    run = ExecutionRun(id="r", prompt="写 FIFO", status=RunStatus.QUEUED, budget_usd=1, allow_network=False, plan=TaskPlan(summary="s", nodes=[]))
    failed = [preflight.PreflightCheck(name="工具", command="vcs -ID", ok=False, output="vcs: command not found")]
    prompt = preflight.repair_prompt(run, failed, tmp_path / "w", tmp_path / "tools")
    assert "vcs -ID" in prompt and "vcs: command not found" in prompt and "no network" in prompt
    assert f"{tmp_path / 'tools'}/bin" in prompt and "STATUS: FIXED" in prompt


def test_every_model_is_told_where_it_runs():
    """A model that does not know it is in a sandbox works around limits it does not see."""
    from super_partner import agent, outline, quality
    from super_partner.models import PlanPart

    step = agent.agent_instructions(root="/w")
    assert "Where you are: a sandbox" in step and "not root" in step and "home folder is hidden" in step
    assert "Commands have no network access" in step
    assert "this task allows the internet" in agent.agent_instructions(root="/w", network=True)
    assert "not in the task's steps' sandbox" in agent.HOST_INSTRUCTIONS
    run = ExecutionRun(id="r", prompt="p", status=RunStatus.QUEUED, budget_usd=1, allow_network=True,
                       plan=TaskPlan(summary="s", nodes=[]))
    for model_id in ("cli:codex", "cli:claude"):
        node = TaskNode(id="n", title="t", instructions="i", capability="coding", model_id=model_id)
        prompt, _ = quality.node_prompt(node, run, 4000)
        assert "Super Partner runs you in your own sandbox" in prompt and "allows the internet" in prompt
        assert "find . -name '*.o' -delete" in prompt  # the same note for every CLI (cli_channels.codex_rules)
    api_step = TaskNode(id="n", title="t", instructions="i", capability="coding", model_id="x:flash")
    assert "Super Partner runs you" not in quality.node_prompt(api_step, run, 4000)[0]
    # The lead splitting a part hears whether the task allows the internet now, not at planning.
    plan = TaskPlan(summary="s", nodes=[], request="写 FIFO\n\n" + outline.INTERNET_NOTE)
    part = PlanPart(id="p", title="RTL")
    assert outline.INTERNET_NOTE not in outline.part_opening(plan, part, internet=False)
    assert "does not allow the internet" in outline.part_opening(plan, part, internet=False)
    assert outline.part_opening(plan, part, internet=True).count(outline.INTERNET_NOTE) == 1

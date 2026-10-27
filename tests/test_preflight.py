"""Before a task's first step, what it needs is checked in a real step sandbox, so a missing tool
or a broken sandbox is found before any work is paid for (preflight.py)."""
import json
import os
from pathlib import Path

import pytest
from super_partner import agent, outline, preflight
from super_partner.cli_channels import CliChannelRegistry, codex_home, codex_package
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import ExecutionRun, NodeStatus, RunStatus, TaskNode, TaskPlan
from super_partner.providers import ProviderResult
from super_partner.quota import ModelUnavailable
from test_turns import model

# Read before a test changes HOME: where Codex is installed and keeps its helper on this machine.
REAL_CODEX_PACKAGE, REAL_CODEX_HOME = codex_package(), codex_home()
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


class CodexSandbox:
    """Codex's own sandbox, where ``missing`` is not found."""

    def __init__(self, missing=(), unusable=False):
        self.missing, self.unusable, self.commands = set(missing), unusable, []

    async def sandbox_run(self, model_id, command, cwd, timeout, network=False):
        self.commands.append(command)
        if self.unusable:
            return "error: unrecognized subcommand 'sandbox'", False
        if command == preflight.CLI_SANDBOX_COMMAND:
            return f"sp-write\n{os.getuid()}", True
        return ("Exit code 1", False) if any(tool in command for tool in self.missing) else ("ok", True)


def task_with(requires, network=False):
    return ExecutionRun(id="r", prompt="p", status=RunStatus.QUEUED, budget_usd=1, allow_network=network,
                        plan=TaskPlan(summary="s", nodes=[], requires=requires))


async def test_the_tools_are_checked_in_codexs_own_sandbox_too(tmp_path):
    cli = CodexSandbox(missing={"vcs"})
    checks = await preflight.cli_checks(cli, task_with(["vcs -ID", "make --version"]), tmp_path, {"cli:codex", "x:flash"})
    assert [(c.command, c.ok, c.blocking) for c in checks] == [
        ("(内置)", True, True), ("vcs -ID", False, True), ("make --version", True, True)]
    assert all(c.name.startswith("Codex 沙箱") for c in checks)
    # Not when Codex cannot run the task's steps.
    assert await preflight.cli_checks(cli, task_with(["vcs -ID"]), tmp_path, {"x:flash"}) == []


async def test_an_old_codex_or_the_internet_is_only_a_warning(tmp_path):
    [old] = await preflight.cli_checks(CodexSandbox(unusable=True), task_with(["vcs -ID"]), tmp_path, {"cli:codex"})
    assert not old.ok and not old.blocking
    checks = await preflight.cli_checks(CodexSandbox(), task_with([], network=True), tmp_path, {"cli:codex"})
    assert not checks[-1].blocking and "互联网" in checks[-1].name


@needs_bwrap
async def test_a_tool_codex_cannot_find_stops_the_task_before_any_step(tmp_path):
    machine = Machine()
    machine.cli = CodexSandbox(missing={"fakesim"})
    database = Database(tmp_path / "t.db")
    executor = Executor(database, machine, MODELS | {"cli:codex": model("cli:codex", 96, "cli")})
    run = task_with(["true fakesim"])
    run.plan.nodes = [TaskNode(id="n", title="t", instructions="i", capability="coding", model_id="x:flash")]
    database.save_run(run)
    assert await executor._preflight("r") is False
    failed = [c for c in database.get_run("r").preflight.checks if not c.ok]
    assert [c.name for c in failed] == ["Codex 沙箱：工具"] and machine.steps == []


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A home with keys and other projects, Codex's own folder in it, and the platform's folders."""
    from super_partner import cli_channels
    home = tmp_path / "home"
    for name in (".ssh/id_rsa", ".codex/auth.json", ".codex/tmp/arg0/x", "code/other/main.c", "notes.txt"):
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        (home / name).write_text("private")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(cli_channels, "codex_package", lambda: None)
    for task in ("this-task", "other-task"):
        (settings.workspace_root / task).mkdir(parents=True)
    (Path(settings.data_dir) / "tools" / "bin").mkdir(parents=True)
    (Path(settings.data_dir) / "super-partner.db").write_text("db")
    return home


def rules_of(args):
    """The inline TOML table of paths (all keys and values are JSON strings) as a dict."""
    [table] = [arg.split("filesystem=", 1)[1] for arg in args if "filesystem=" in arg]
    return json.loads(table.replace('" = "', '": "'))


def test_a_codex_step_reads_only_its_folder_and_what_it_needs(fake_home):
    from super_partner import cli_channels
    cwd = settings.workspace_root / "this-task"
    rules = rules_of(cli_channels.codex_step_permissions(cwd, network=False))
    resolved = {str(Path(path).resolve()) if path.startswith("/") else path: mode for path, mode in rules.items()}
    home, data = fake_home.resolve(), Path(settings.data_dir).resolve()
    denied = {path for path, mode in resolved.items() if mode == "deny"}
    assert {str(home / ".ssh"), str(home / "code"), str(home / "notes.txt"), str(home / ".codex" / "auth.json"),
            str(data / "super-partner.db"), str(settings.workspace_root.resolve() / "other-task")} <= denied
    # Its folder is under /tmp here (pytest's): the rest of /tmp is denied entry by entry instead.
    assert ":slash_tmp" in denied or any(path.startswith("/tmp/") for path in denied)
    assert str(home / ".codex" / "tmp") not in denied and str(data / "tools") not in denied  # Codex's helper, the tools
    assert resolved[str(cwd.resolve())] == "write" and resolved[":tmpdir"] == "write"
    # A lead reads, the tasks' folders included, and nothing is written.
    lead = rules_of(cli_channels.codex_step_permissions(fake_home / ".codex", network=False, lead=True))
    assert "write" not in lead.values() and not any("other-task" in path for path in lead)



def test_a_codex_step_may_write_in_tmp_but_not_read_what_is_already_there(tmp_path, monkeypatch):
    # URG writes its temporary files in /tmp whatever TMPDIR says, and crashes when it cannot.
    from super_partner import cli_channels
    shared = tmp_path / "shared"
    for name in ("other-session/notes", "keys.json"):
        (shared / name).parent.mkdir(parents=True, exist_ok=True)
        (shared / name).write_text("private")
    rules = {}
    cli_channels._shared_tmp(tmp_path / "elsewhere" / "task", rules, shared)
    assert rules == {str(shared.resolve()): "write", str(shared.resolve() / "keys.json"): "deny",
                     str(shared.resolve() / "other-session"): "deny"}
    monkeypatch.setattr(cli_channels, "MAX_TMP_ENTRIES", 1)
    rules = {}
    cli_channels._shared_tmp(tmp_path / "elsewhere" / "task", rules, shared)
    assert rules == {":slash_tmp": "deny"}  # too many to list: all of it, as before


def test_a_codex_steps_own_temporary_folder_is_under_the_hidden_data_folder():
    from super_partner import cli_channels
    with cli_channels.private_tmp() as folder:
        assert Path(settings.data_dir).resolve() / "tmp" in Path(folder).parents
    assert not Path(folder).exists()

async def test_codex_reading_the_platforms_data_is_a_warning(fake_home, monkeypatch):
    from super_partner import cli_channels
    monkeypatch.setattr(settings, "database_path", Path(settings.data_dir) / "super-partner.db")

    class Leaky(CodexSandbox):
        async def sandbox_run(self, model_id, command, cwd, timeout, network=False):
            if "readable:" in command:
                return "readable: " + str(settings.db_path), True
            return await super().sandbox_run(model_id, command, cwd, timeout, network)

    checks = await preflight.cli_checks(Leaky(), task_with([]), settings.workspace_root / "this-task", {"cli:codex"})
    [privacy] = [c for c in checks if "读不到" in c.name]
    assert not privacy.ok and not privacy.blocking
    assert cli_channels.codex_home() == fake_home / ".codex"


@pytest.mark.skipif(not __import__("shutil").which("codex"), reason="Codex is not installed")
async def test_in_codexs_real_sandbox_a_step_cannot_read_keys_or_other_tasks(fake_home, monkeypatch):
    """With the real Codex: the profile is accepted and enforced (bubblewrap)."""
    from super_partner import cli_channels
    monkeypatch.setattr(cli_channels, "codex_package", lambda: REAL_CODEX_PACKAGE)
    # Codex needs its own helper, which it keeps in its real home; this test's home lacks it.
    monkeypatch.setenv("CODEX_HOME", str(REAL_CODEX_HOME))
    cwd = settings.workspace_root / "this-task"
    script = (f"cat {fake_home}/.ssh/id_rsa; cat {settings.workspace_root}/other-task/../other-task 2>&1; "
              f"cat {Path(settings.data_dir)}/super-partner.db; echo mine > own.txt && cat own.txt; "
              "echo t > $TMPDIR/t && cat $TMPDIR/t; echo s > /tmp/sp-step-$$ && cat /tmp/sp-step-$$ && rm /tmp/sp-step-$$")
    output, _ = await CliChannelRegistry().sandbox_run("cli:codex", script, cwd, 60)
    if "unknown" in output or "requires" in output:
        pytest.skip(f"this Codex cannot use permission profiles: {output[:200]}")
    lines = output.splitlines()
    assert "private" not in lines and "db" not in lines  # what the files hold never comes back
    assert "mine" in lines and "t" in lines and "s" in lines  # its own folder, TMPDIR and /tmp work

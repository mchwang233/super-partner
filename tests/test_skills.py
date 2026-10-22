"""Skills for the steps: prepared before the first step for the tools a task needs, kept for the
next task, and given to the steps that use those tools (skills.py, preflight.py)."""
import pytest
from super_partner import agent, environment, preflight, quality, skills
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import ExecutionRun, PlanPart, RunStatus, TaskNode, TaskPlan
from super_partner.providers import ProviderResult
from test_turns import model

needs_bwrap = pytest.mark.skipif(not agent.sandbox_available(), reason="bubblewrap is not installed")
GENERAL = ("---\nname: fake-sim\ndescription: Drive the fake simulator.\ntools: [fakesim, fakerep]\n---\n"
           "# Fake simulator\nCompile with `fakesim -c`.\n")


@pytest.fixture
def library(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "fake-sim").mkdir(parents=True)
    (source / "fake-sim" / "SKILL.md").write_text(GENERAL)
    monkeypatch.setattr(skills, "SOURCE", source)
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    return source


def test_a_skill_is_read_and_matched_by_the_tools_a_step_names(library):
    [general] = skills.library()
    assert (general.name, general.tools, general.machine) == ("fake-sim", ["fakesim", "fakerep"], False)
    assert skills.for_step("Run FAKESIM on the RTL") == [general]
    assert skills.for_step("fakesimulator, /opt/fakesim-2/bin") == []  # other words, a path
    skills.save_machine("fake-sim", GENERAL.replace("# Fake simulator", "# Here: source env.sh first"))
    mine, theirs = skills.for_step("fakesim -c a.sv")
    assert mine.machine and not theirs.machine  # this machine's first
    assert "this machine's are right" in skills.section([mine, theirs])


def test_a_step_is_given_the_skills_for_the_tools_it_uses(library):
    run = ExecutionRun(id="r", prompt="p", status=RunStatus.QUEUED, budget_usd=1, plan=TaskPlan(summary="s", nodes=[]))
    sim = TaskNode(id="a", title="编译", instructions="用 fakesim 编译并运行", capability="coding")
    doc = TaskNode(id="b", title="文档", instructions="写 README", capability="coding")
    assert "Skill: fake-sim" in quality.node_prompt(sim, run, 4000)[0]
    assert "Skill:" not in quality.node_prompt(doc, run, 4000)[0]


def test_the_tools_to_prepare_are_the_special_ones_the_task_names(library):
    run = ExecutionRun(id="r", prompt="用 fakerep 出报告", status=RunStatus.QUEUED, budget_usd=1, plan=TaskPlan(
        summary="s", nodes=[], requires=["make --version", "command -v other-tool"],
        parts=[PlanPart(id="p", title="仿真", goal="fakesim 回归")]))
    tools = preflight.needed_tools(run, skills.library())
    assert tools == ["other-tool", "fakesim", "fakerep"]  # not make: every step knows it
    groups = preflight.tool_groups(tools, skills.library())
    assert [(name, tools) for name, tools, _ in groups] == [("fake-sim", ["fakesim", "fakerep"]), ("other-tool", ["other-tool"])]
    assert "tools: [fakesim, fakerep, x]" in preflight._with_tools(GENERAL, ["x"])


class Machine:
    """A model that writes the skill it is asked for, and does the steps."""

    def __init__(self):
        self.skill_prompts, self.step_prompts = [], []

    def configured_models(self, ids):
        return set(ids)

    async def complete(self, model_id, prompt, workspace=None, **kwargs):
        if "write this machine's skill" in prompt:
            self.skill_prompts.append(prompt)
            return ProviderResult("---\nname: fake-sim\ndescription: fake-sim here\ntools: [fakesim]\n---\n"
                                  "# Here\nRun `fakesim -c a.sv` after `source env.sh`.\n", 1, 1, model_id)
        if prompt != "Reply with just: OK":  # the check that the model answers
            self.step_prompts.append(prompt)
        return ProviderResult("done", 1, 1, model_id)


def task(database, run_id):
    node = TaskNode(id="n", title="仿真", instructions="用 fakesim 编译并运行测试", capability="coding", model_id="x:flash")
    database.save_run(ExecutionRun(id=run_id, prompt="用 fakesim 验证", status=RunStatus.QUEUED, budget_usd=1,
                                   plan=TaskPlan(summary="s", nodes=[node], requires=["fakesim --version"])))


@needs_bwrap
async def test_the_skill_is_prepared_once_and_given_to_the_steps(library, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "preflight", True)
    monkeypatch.setattr(settings, "review_nodes", False)
    tools = environment.prepare()
    for name in ("fakesim", "fakerep"):
        (tools / "bin" / name).write_text("#!/bin/sh\necho fake 1.0\n")
        (tools / "bin" / name).chmod(0o755)
    database = Database(tmp_path / "t.db")
    machine = Machine()
    executor = Executor(database, machine, {item.id: item for item in [model("x:flash", 90)]})
    task(database, "first")
    await executor.execute("first")
    first = database.get_run("first")
    assert first.status == RunStatus.SUCCEEDED and first.preflight.skills == ["fake-sim"]
    [prompt] = machine.skill_prompts
    assert "fakesim --version" in prompt and "Compile with `fakesim -c`" in prompt  # what was checked, the general skill
    saved = skills.machine_dir() / "fake-sim" / "SKILL.md"
    assert "source env.sh" in saved.read_text() and "fakerep" in saved.read_text()
    assert "source env.sh" in machine.step_prompts[0]
    # The next task on this machine uses it as it is.
    task(database, "second")
    await executor.execute("second")
    assert len(machine.skill_prompts) == 1 and database.get_run("second").preflight.skills == ["fake-sim"]
    assert not list((settings.workspace_root).glob("*/.skill-*"))  # the scratch folder is gone

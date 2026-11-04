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


async def test_reuse_first_uses_existing_general_skill_without_generating(library, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "skill_reuse_first", True)
    database = Database(tmp_path / "t.db")
    task(database, "reuse")
    machine = Machine()
    executor = Executor(database, machine, {m.id: m for m in [model("x:flash", 90)]})

    async def available(*args):
        return ["fakesim"]

    monkeypatch.setattr(executor, "_available", available)
    assert await executor._prepare_skills("reuse", tmp_path, []) == ["fake-sim"]
    assert machine.skill_prompts == []
    assert not skills.machine_dir().exists()
    selected = [e for e in database.events("reuse") if e.kind == "run.skill_selected"]
    assert selected[0].data["origin"] == "general"


@pytest.mark.parametrize("network", [False, True])
async def test_search_precedes_bounded_generation_only_when_network_allowed(library, tmp_path, monkeypatch, network):
    from super_partner import skill_discovery
    monkeypatch.setattr(settings, "skill_reuse_first", True)
    monkeypatch.setattr(settings, "skill_generation_max_turns", 4)
    monkeypatch.setattr(skills, "SOURCE", tmp_path / "empty")
    database = Database(tmp_path / "t.db")
    task(database, "missing")
    run = database.get_run("missing")
    run.allow_network = network
    database.save_run(run)
    executor = Executor(database, Machine(), {m.id: m for m in [model("x:flash", 90)]})
    actions = []

    async def available(*args):
        return ["fakesim"]

    async def discover(tools):
        actions.append("search")
        return skill_discovery.Discovery(reason="No match")

    async def complete(*args, **kwargs):
        actions.append("generate")
        assert kwargs["max_turns"] == 4
        return ProviderResult(GENERAL, 1, 1, "x:flash")

    monkeypatch.setattr(executor, "_available", available)
    monkeypatch.setattr(skill_discovery, "discover", discover)
    monkeypatch.setattr(executor, "_complete", complete)
    assert await executor._prepare_skills("missing", tmp_path, []) == ["fakesim"]
    assert actions == (["search", "generate"] if network else ["generate"])


async def test_a_public_skill_is_only_a_reference_for_this_machines_skill(library, tmp_path, monkeypatch):
    from super_partner import skill_discovery
    monkeypatch.setattr(settings, "skill_reuse_first", True)
    monkeypatch.setattr(skills, "SOURCE", tmp_path / "empty")
    database = Database(tmp_path / "t.db")
    task(database, "online")
    run = database.get_run("online")
    run.allow_network = True
    database.save_run(run)
    executor = Executor(database, Machine(), {m.id: m for m in [model("x:flash", 90)]})
    searched, prompts = [], []

    async def available(*args):
        return ["fakesim"]

    async def discover(tools):
        searched.append(tools)
        skill = skills.parse(GENERAL.replace("Compile with", "Public: compile with"))
        skill.source_url = "https://github.com/example/skills/blob/main/SKILL.md"
        skill.source_revision = "b" * 40
        return skill_discovery.Discovery(skill=skill)

    async def complete(run, node, model_id, prompt, *args, **kwargs):
        prompts.append(prompt)
        return ProviderResult("---\nname: fakesim\ndescription: here\ntools: [fakesim]\n---\n# Here\n", 1, 1, model_id)

    monkeypatch.setattr(executor, "_available", available)
    monkeypatch.setattr(skill_discovery, "discover", discover)
    monkeypatch.setattr(executor, "_complete", complete)
    assert await executor._prepare_skills("online", tmp_path, []) == ["fakesim"]
    assert "Public: compile with" in prompts[0] and "not checked on this machine" in prompts[0]
    # The step is given this machine's skill, never the public one.
    assert [s.machine for s in skills.for_step("fakesim -c a.sv")] == [True]
    # The cached public skill is the reference next time, without another search.
    for path in skills.machine_dir().glob("*/SKILL.md"):
        path.unlink()
    assert await executor._prepare_skills("online", tmp_path, []) == ["fakesim"]
    assert len(searched) == 1 and "Public: compile with" in prompts[1]


async def test_generation_deadline_does_not_block_task_start(library, tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setattr(settings, "skill_reuse_first", True)
    monkeypatch.setattr(settings, "skill_preparation_timeout", 0.01)
    monkeypatch.setattr(skills, "SOURCE", tmp_path / "empty")
    database = Database(tmp_path / "t.db")
    task(database, "timeout")
    executor = Executor(database, Machine(), {m.id: m for m in [model("x:flash", 90)]})
    cancelled = []

    async def available(*args):
        return ["fakesim"]

    async def write(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(executor, "_available", available)
    monkeypatch.setattr(executor, "_write_skill", write)
    assert await executor._prepare_skills("timeout", tmp_path, []) == []
    assert cancelled == [True]
    event = [e for e in database.events("timeout") if e.kind == "run.skills_prepared"][-1]
    assert event.data["failed"]


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


LESSON = "---\nname: fake-sim-lesson\ndescription: seeds\ntools: [fakesim]\n---\nPass `+seed=N` or runs repeat.\n"


def test_a_lesson_is_written_into_this_machines_skill_and_taken_out_again(library):
    original = GENERAL.replace("# Fake simulator", "# Here: source env.sh first")
    path = skills.save_machine("fake-sim", original)
    assert skills.add_lesson("l1", "Seeds", LESSON, ["fakesim"]) == [path]
    skills.add_lesson("l2", "Licence", LESSON.replace("Pass `+seed=N` or runs repeat.", "Wait for the licence."),
                      ["fakesim"])
    [mine, _general] = skills.for_step("fakesim -c a.sv")
    assert mine.body.index("Wait for the licence") < mine.body.index("+seed=N") < mine.body.index("source env.sh")
    assert "Pass `+seed=N`" in skills.section([mine])
    skills.remove_lesson("l2")
    skills.remove_lesson("l1")
    assert path.read_text() == original


def test_a_skill_made_of_a_lesson_alone_goes_with_it(library):
    [path] = skills.add_lesson("l1", "Seeds", LESSON, ["fakesim"])
    assert skills.for_step("fakesim")[0].machine and "+seed=N" in path.read_text()
    skills.remove_lesson("l1")
    assert not path.exists() and not [s for s in skills.library() if s.machine]


def test_the_github_reference_skill_reaches_steps_and_employees_that_look_on_github(tmp_path, monkeypatch):
    from super_partner import team
    from super_partner.models import Employee

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")  # the platform's own skills only
    [guide] = [skill for skill in skills.library() if skill.name == "github-reference"]
    assert set(guide.tools) == {"github", "gh"} and not guide.machine
    assert len(guide.body) <= skills.MAX_SKILL_CHARS  # given whole, not cut off
    for rule in ("gh_rank.py", "never run its scripts", "REFERENCE: OWNER/REPO@SHA", "license"):
        assert rule in guide.body
    assert guide in skills.for_step("在 GitHub 上找一个成熟的 JSON 解析库作为参考实现")
    assert guide not in skills.for_step("写 README 并整理目录")

    plan = TaskPlan(summary="s", nodes=[], employees=[Employee(
        id="dev", name="开发", role="实现解析器", model_id="test:m", capabilities=["coding"],
        skills=["github-reference"])])
    team.refresh_skills(plan)
    assert "Skill: github-reference" in plan.employees[0].skill_context

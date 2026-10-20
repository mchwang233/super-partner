"""A project task is planned as small work packages, each naming the files it needs."""
import json

import httpx
import pytest
from super_partner import progress
from super_partner.budget import ProviderError
from super_partner.models import SupervisorSession, TaskNode, TaskPlan
from super_partner.planner import autonomous_plan, scope_problems
from super_partner.progress import expected_turns
from super_partner.providers import ProviderRegistry
from super_partner.quality import node_prompt
from test_progress import READ, reply
from test_supervisor import CONFIGURED, MODELS, Leads

BRIEF = "Project brief ...\napp.js (1447 lines): SPA\nstyle.css (308 lines): styles"


def plan(*nodes):
    """The lead's split of a part into tasks."""
    return json.dumps({"children": [
        {"key": key, "kind": "task", "title": key, "instructions": f"做 {key}", "capability": "coding",
         "needs_workspace": True, "acceptance_criteria": "可检查", "dependencies": deps, **fields}
        for key, deps, fields in nodes]})


OUTLINE = json.dumps({"summary": "s", "parts": [{"key": "p", "title": "全部", "goal": "完成请求"}]})


AREA = plan(("界面", [], {"files": ["easylife/static/"]}), ("检查", ["界面"], {"verifies": ["界面"]}))
PACKAGES = plan(
    ("按钮", [], {"files": ["style.css"], "provides": ".btn-primary 44px", "check": "node --check app.js"}),
    ("设置页", [], {"files": ["app.js"], "reads": ["style.css"]}),
    ("检查", ["按钮", "设置页"], {"verifies": ["按钮", "设置页"], "reads": ["app.js", "style.css"]}))


async def test_the_lead_plans_from_the_brief_and_cuts_broad_steps_into_packages():
    session = SupervisorSession()
    leads = Leads([OUTLINE, AREA, PACKAGES])
    result = await autonomous_plan("把界面打磨好", MODELS, CONFIGURED, leads, session=session, brief=BRIEF)
    assert [node.title for node in result.nodes] == ["按钮", "设置页", "检查"]
    first, _, second = (message for _, _, message in leads.messages)
    assert BRIEF in first and "small work packages" in first
    assert "界面: names folders or patterns (easylife/static/)" in second
    button = result.nodes[0]
    assert (button.files, button.provides, button.check) == (["style.css"], ".btn-primary 44px", "node --check app.js")


async def test_a_lead_that_keeps_planning_broadly_still_gets_its_plan_run():
    leads = Leads([OUTLINE, AREA, AREA, AREA])
    result = await autonomous_plan("把界面打磨好", MODELS, CONFIGURED, leads, session=SupervisorSession(), brief=BRIEF)
    assert [node.title for node in result.nodes] == ["界面", "检查"] and len(leads.messages) == 4


def test_what_makes_a_package_too_broad():
    def step(**fields):
        return TaskNode(**{"id": "n", "title": "T", "instructions": "i", "capability": "coding",
                           "needs_workspace": True, **fields})
    assert scope_problems(TaskPlan(summary="s", nodes=[step(files=["a.py"], reads=["b.py"])])) == []
    [missing] = scope_problems(TaskPlan(summary="s", nodes=[step()]))
    [many] = scope_problems(TaskPlan(summary="s", nodes=[step(files=[f"{i}.py" for i in range(6)])]))
    assert "lists no files" in missing and "changes 6 files" in many
    # A new task's code needs a scope too; verifications and steps that write nothing do not.
    assert "lists no files" in scope_problems(TaskPlan(summary="s", nodes=[step(needs_workspace=False)]))[0]
    assert scope_problems(TaskPlan(summary="s", nodes=[
        step(verifies=["x"]), step(needs_workspace=False, capability="research")])) == []


def test_a_package_hears_what_came_before_in_brief(database):
    run = database.get_run("r")
    before = run.plan.nodes[0]
    before.result, before.provides = "很长的过程说明。" * 1000, "GET /api/admin/mail-settings -> {host, port}"
    step = TaskNode(id="b", title="B", instructions="做设置页", capability="coding", dependencies=["a"], files=["app.js"])
    run.plan.nodes.append(step)
    prompt, _ = node_prompt(step, run, 4000)
    assert "It provides: GET /api/admin/mail-settings -> {host, port}" in prompt
    assert prompt.count("很长的过程说明") < 200  # 1500 characters of it, not the whole result


async def test_a_package_far_past_its_turns_goes_to_the_lead(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    registry = ProviderRegistry()
    registry.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=reply({"content": None, "tool_calls": [READ]}))))
    asked = []

    async def lead(evidence):
        asked.append(evidence)
        return None if len(asked) == 1 else "一直在读同一个目录，这个包太大，应拆开"

    with pytest.raises(ProviderError, match=progress.OVERRUN):
        await registry.complete("deepseek:deepseek-flash", "work", tmp_path, act=True, overrun=(5, lead))
    await registry.client.aclose()
    assert [evidence.split(" ", 1)[0] for evidence in asked] == ["10", "20"]


def test_the_lead_is_asked_only_about_scoped_packages(database):
    step = TaskNode(id="b", title="B", instructions="i", capability="coding", difficulty="hard", files=["a.py"])
    assert expected_turns(step) == 25
    assert expected_turns(step.model_copy(update={"files": []})) is None
    assert expected_turns(step.model_copy(update={"verifies": ["a"]})) is None


async def test_a_new_task_is_cut_into_packages_too():
    broad = plan(("全部", [], {}), ("说明", ["全部"], {"capability": "chinese"})).replace('"needs_workspace": true, ', '')
    leads = Leads([OUTLINE, broad, json.dumps(
        {"children": [
            {"key": "rtl", "title": "RTL", "instructions": "写 FIFO", "capability": "coding", "files": ["async_fifo.v"],
             "provides": "module async_fifo #(DATA_WIDTH, DEPTH)", "acceptance_criteria": "仿真通过"},
            {"key": "tb", "title": "测试", "instructions": "写测试", "capability": "coding",
             "files": ["tb_async_fifo.v", "Makefile"], "acceptance_criteria": "make 能跑"}]})])
    result = await autonomous_plan("写一个异步 FIFO", MODELS, CONFIGURED, leads, session=SupervisorSession())
    assert "全部: lists no files" in leads.messages[2][2]
    assert [node.files for node in result.nodes] == [["async_fifo.v"], ["tb_async_fifo.v", "Makefile"]]

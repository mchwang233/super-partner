"""A project task is planned as small work packages, each naming the files it needs."""
import json

import httpx
import pytest
from super_partner import outline, progress
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


async def split(prompt, replies, brief=None):
    """Plan ``prompt`` as one major part, then split it with ``replies``, as a running task does."""
    leads = Leads([OUTLINE, *replies])
    planned = await autonomous_plan(prompt, MODELS, CONFIGURED, leads, session=SupervisorSession(), brief=brief)
    session = SupervisorSession(model_id="cli:codex")

    async def say(text):
        return (await leads.converse(session, text)).text

    part = planned.parts[0]
    _, nodes = await outline.refine(say, planned, part, outline.part_opening(planned, part))
    return TaskPlan(summary="s", nodes=nodes), [message for _, _, message in leads.messages]


AREA = plan(("界面", [], {"files": ["easylife/static/"]}), ("检查", ["界面"], {"verifies": ["界面"]}))
PACKAGES = plan(
    ("按钮", [], {"files": ["style.css"], "provides": ".btn-primary 44px", "check": "node --check app.js"}),
    ("设置页", [], {"files": ["app.js"], "reads": ["style.css"]}),
    ("检查", ["按钮", "设置页"], {"verifies": ["按钮", "设置页"], "reads": ["app.js", "style.css"]}))


async def test_the_lead_plans_from_the_brief_and_cuts_broad_steps_into_packages():
    result, messages = await split("把界面打磨好", [AREA, PACKAGES], BRIEF)
    assert [node.title for node in result.nodes] == ["按钮", "设置页", "检查"]
    _, first, second = messages
    assert BRIEF in first and "small work packages" in first
    assert "界面: names folders or patterns (easylife/static/)" in second
    button = result.nodes[0]
    assert (button.files, button.provides, button.check) == (["style.css"], ".btn-primary 44px", "node --check app.js")


async def test_a_lead_that_keeps_planning_broadly_still_gets_its_plan_run():
    result, messages = await split("把界面打磨好", [AREA, AREA, AREA], BRIEF)
    assert [node.title for node in result.nodes] == ["界面", "检查"] and len(messages) == 4


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
    result, messages = await split("写一个异步 FIFO", [broad, json.dumps(
        {"children": [
            {"key": "rtl", "title": "RTL", "instructions": "写 FIFO", "capability": "coding", "files": ["async_fifo.v"],
             "provides": "module async_fifo #(DATA_WIDTH, DEPTH)", "acceptance_criteria": "仿真通过"},
            {"key": "tb", "title": "测试", "instructions": "写测试", "capability": "coding",
             "files": ["tb_async_fifo.v", "Makefile"], "acceptance_criteria": "make 能跑"}]})])
    assert "全部: lists no files" in messages[2]
    assert [node.files for node in result.nodes] == [["async_fifo.v"], ["tb_async_fifo.v", "Makefile"]]


def test_artifact_flat_plan_preserves_three_distinct_jobs_and_directory_contract():
    from artifact_helpers import coverage_nodes, raw
    from super_partner.planner import _validate_plan

    planned = _validate_plan({"nodes": [raw(node) for node in coverage_nodes()]})
    vdb, report, audit = planned.nodes
    assert [n.artifact_contract.stage for n in planned.nodes] == ["coverage_vdb", "urg_report", "delivery_audit"]
    assert vdb.artifact_contract.outputs[0].kind == "directory"
    assert report.dependencies == [vdb.id] and audit.dependencies == [report.id]
    assert audit.verifies == [report.id] and scope_problems(planned) == []


@pytest.mark.parametrize("missing", ["inputs", "outputs", "acceptance_commands", "blocked_when", "max_turns", "max_cost_usd"])
def test_artifact_contract_missing_declaration_is_rejected(missing):
    from artifact_helpers import coverage_nodes, raw
    from super_partner.planner import _validate_plan

    nodes = [raw(node) for node in coverage_nodes()]
    del nodes[0]["artifact_contract"][missing]
    with pytest.raises(ValueError, match="artifact_contract"):
        _validate_plan({"nodes": nodes})


def test_artifact_contract_requires_real_producer_dependency_and_separate_audit():
    from artifact_helpers import coverage_nodes, raw
    from super_partner.planner import _validate_plan

    nodes = [raw(node) for node in coverage_nodes()]
    nodes[1]["dependencies"] = []
    with pytest.raises(ValueError, match="producer dependency"):
        _validate_plan({"nodes": nodes})
    nodes = [raw(node) for node in coverage_nodes()]
    nodes[0]["title"] = "VDB 采集、URG 文本报告生成及交付审计"
    with pytest.raises(ValueError, match="separate artifact stages"):
        _validate_plan({"nodes": nodes})


def test_artifact_external_vdb_report_needs_no_collection_node_and_old_records_still_load():
    from artifact_helpers import coverage_nodes, raw
    from super_partner.planner import _validate_plan

    _, report, audit = coverage_nodes()
    report.dependencies = []
    report.artifact_contract.inputs[0].source = "external"
    planned = _validate_plan({"nodes": [raw(report), raw(audit)]})
    assert len(planned.nodes) == 2 and planned.nodes[0].dependencies == []
    old = TaskNode.model_validate({"id": "old", "title": "coverage overview", "instructions": "summarize", "capability": "research"})
    assert old.artifact_contract is None and old.contract_turns == 0
    assert scope_problems(TaskPlan(summary="old", nodes=[old])) == []


async def test_artifact_invalid_flat_plan_cannot_fall_back_to_unbounded_work():
    from artifact_helpers import coverage_nodes, raw
    from super_partner.planner import autonomous_plan
    from super_partner.providers import ProviderResult

    nodes = [raw(node) for node in coverage_nodes()]
    del nodes[0]["artifact_contract"]

    class BadPlanner:
        async def complete(self, model_id, *args, **kwargs):
            return ProviderResult(json.dumps({"nodes": nodes}), 1, 1, model_id)

    with pytest.raises(ValueError, match="Artifact planning blocked"):
        await autonomous_plan("Collect VDB then generate URG report", MODELS, CONFIGURED - {"mock:local"}, BadPlanner())


def test_artifact_explicit_collection_and_report_require_contracts():
    from super_partner.planner import _validate_plan

    with pytest.raises(ValueError, match="artifact_contract"):
        _validate_plan({"nodes": [
            {"key": "vdb", "title": "VDB 采集", "instructions": "Run simulation", "files": ["sim.vdb"]},
            {"key": "report", "title": "URG 文本报告", "instructions": "Generate report", "dependencies": ["vdb"]},
        ]})


@pytest.mark.parametrize("title,files", [
    ("实现 URG 文本解析及内容验收器", ["scripts/parse_report.py", "tests/test_parse_report.py"]),
    ("编写 URG 复现和报告追溯说明", ["docs/reproduce.md"]),
])
def test_report_preparation_is_not_report_production(title, files):
    from super_partner import contracts

    node = TaskNode(id="helper", title=title, instructions="Prepare the helper and test it with fixtures.",
                    capability="coding", files=files, acceptance_criteria="Unit tests pass.")
    assert contracts.stages(node) == set()
    assert contracts.node_problems(node, coverage=True) == []


def test_report_preparation_does_not_erase_explicit_contract_or_real_report_outputs():
    from artifact_helpers import contract_node
    from super_partner import contracts

    node = contract_node("helper", "urg_report", [], "parse_report.py")
    node.title = "实现 URG 文本解析器"
    assert contracts.stages(node) == {"urg_report"}
    assert any("must consume a VDB" in problem for problem in contracts.node_problems(node))
    node.artifact_contract = None
    node.files.append("reports/result.txt")
    assert contracts.stages(node) == {"urg_report"}
    assert contracts.node_problems(node) == [f"{node.title}: artifact_contract is required"]


async def test_artifact_no_planner_does_not_invent_an_unbounded_fallback():
    with pytest.raises(ValueError, match="Artifact planning blocked"):
        await autonomous_plan("Generate URG text report", [], set(), object())

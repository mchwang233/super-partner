"""Team formation precedes planning; delegated work remains in the same durable run."""
import asyncio
import json

import pytest
from super_partner import team
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import (
    CapabilityScore,
    Employee,
    ExecutionRun,
    ModelProfile,
    NodeStatus,
    RunStatus,
    SupervisorSession,
    TaskNode,
    TaskPlan,
)
from super_partner.planner import autonomous_plan
from super_partner.providers import ProviderResult
from super_partner.quality import node_prompt


def models():
    return [ModelProfile(id=f"test:{name}", provider="test", display_name=name, scores=CapabilityScore(**{
        key: 95 for key in CapabilityScore.model_fields}), scores_source="artificial_analysis",
        context_window=200000, max_output_tokens=4096, input_per_million=1, output_per_million=1,
        supports_tools=True) for name in ("build", "verify")]


def employees():
    return [Employee(id="builder", name="开发", role="实现模块并集成", model_id="test:build", capabilities=["coding"]),
            Employee(id="tester", name="验证", role="独立检验", model_id="test:verify", capabilities=["coding", "research"])]


def run_with_team():
    return ExecutionRun(id="team-run", prompt="完成完整模块，保留兼容性", budget_usd=10, status=RunStatus.QUEUED,
                        plan=TaskPlan(summary="module", nodes=[TaskNode(id="parent", title="实现模块", instructions="build",
                            capability="coding", files=["app.py"], employee_id="builder", model_id="test:build")],
                            employees=employees(), team_brief="接口不变；通过独立验证才交付"))


def request(**overrides):
    return {"request_id": "check-parser", "reason": "需要独立研究", "tasks": [{
        "key": "research", "title": "检验解析逻辑", "instructions": "check parser semantics",
        "capability": "research", "difficulty": "normal", "employee_id": "tester",
        "acceptance_criteria": "给出边界情况和证据", "dependencies": [], **overrides}]}


async def test_team_is_decided_before_any_parts_are_planned(monkeypatch):
    monkeypatch.setattr(settings, "employee_teams", True)
    messages = []

    class Planner:
        async def converse(self, session, text, **kwargs):
            messages.append(text)
            payload = ({"team_brief": "保持公开接口并独立验收", "employees": [e.model_dump() for e in employees()]}
                       if len(messages) == 1 else {"summary": "module", "parts": [{"key": "p1", "title": "模块",
                           "employee_id": "builder", "goal": "实现模块", "capability": "coding", "size": 1}]})
            return ProviderResult(json.dumps(payload), 10, 10, session.model_id)

    plan = await autonomous_plan("实现模块", models(), {m.id for m in models()}, Planner(), session=SupervisorSession())
    assert len(messages) == 2
    assert "TEAM_SETUP" in messages[0] and "Do not split" in messages[0]
    assert "builder" in messages[1] and "tester" in messages[1]
    assert [e.model_id for e in plan.employees] == ["test:build", "test:verify"]
    assert plan.parts[0].employee_id == "builder"


def test_employee_context_carries_whole_goal_alignment_and_accepted_own_history():
    run = run_with_team()
    earlier = TaskNode(id="earlier", title="设计", instructions="design", capability="coding", employee_id="builder",
                       status="succeeded", result="已确认接口 version=1", provides="parse(data)")
    run.plan.nodes.append(earlier)
    prompt, _ = node_prompt(run.plan.nodes[0], run, 1000)
    assert run.prompt in prompt and run.plan.team_brief in prompt
    assert "已确认接口 version=1" in prompt and "tester" in prompt
    assert "DELEGATE_TASKS" in prompt


def test_delegation_is_atomic_assigns_other_employee_and_waits_for_delivery():
    run = run_with_team()
    children = team.delegate(run, run.plan.nodes[0], request(), models(), {m.id for m in models()})
    child = children[0]
    assert child.employee_id == "tester" and child.model_id == "test:verify"
    assert child.delegation_parent_id == "parent"
    assert run.plan.nodes[0].dependencies == [child.id]
    assert team.delegate(run, run.plan.nodes[0], request(), models(), {m.id for m in models()}) == children
    assert len(run.plan.nodes) == 2


@pytest.mark.parametrize("override", [{"employee_id": "missing"}, {"employee_id": "builder"},
    {"dependencies": ["parent"]}, {"files": ["../outside.txt"]}, {"capability": "planning"}])
def test_invalid_delegation_never_partially_changes_plan(override):
    run = run_with_team()
    before = run.model_dump_json()
    with pytest.raises(ValueError):
        team.delegate(run, run.plan.nodes[0], request(**override), models(), {m.id for m in models()})
    assert run.model_dump_json() == before


async def test_parent_yields_child_runs_on_its_employee_then_parent_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "review_nodes", False)
    monkeypatch.setattr(settings, "lead_decisions", False)
    db = Database(tmp_path / "team.db")
    db.save_run(run_with_team())
    calls = []

    class Workers:
        def configured_models(self, _):
            return {m.id for m in models()}

        async def complete(self, model_id, prompt, workspace=None, **kwargs):
            calls.append((model_id, prompt))
            text = "DELEGATE_TASKS\n" + json.dumps(request()) if len(calls) == 1 else "子任务证据已确认" if len(calls) == 2 else "完成集成"
            return ProviderResult(text, 100, 100, model_id)

    executor = Executor(db, Workers(), {m.id:m for m in models()})
    await asyncio.wait_for(executor.execute("team-run"), 5)
    run = db.get_run("team-run")
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert [model for model, _ in calls] == ["test:build", "test:verify", "test:build"]
    assert "子任务证据已确认" in calls[2][1]
    assert [a.outcome for a in run.plan.nodes[0].attempts] == ["delegated", "passed"]
    assert run.actual_cost_usd == pytest.approx(0.0006)


def test_old_records_still_load_without_employees():
    assert TaskPlan.model_validate({"summary": "old", "nodes": []}).employees == []


def test_missing_recipient_selects_another_capable_employee():
    run = run_with_team()
    child = team.delegate(run, run.plan.nodes[0], request(employee_id=None), models(), {m.id for m in models()})[0]
    assert child.employee_id == "tester"


def test_same_request_id_cannot_be_reused_with_other_work():
    run = run_with_team()
    team.delegate(run, run.plan.nodes[0], request(), models(), {m.id for m in models()})
    with pytest.raises(ValueError, match="different delegation"):
        team.delegate(run, run.plan.nodes[0], request(instructions="unrelated work"), models(), {m.id for m in models()})
    assert len(run.plan.nodes) == 2


def test_delegation_depth_is_bounded(monkeypatch):
    monkeypatch.setattr(settings, "team_max_delegation_depth", 1)
    run = run_with_team()
    child = team.delegate(run, run.plan.nodes[0], request(), models(), {m.id for m in models()})[0]
    with pytest.raises(ValueError, match="depth limit"):
        team.delegate(run, child, request(employee_id="builder", capability="coding", files=["helper.py"]), models(), {m.id for m in models()})


async def test_cancelled_task_rejects_late_delegation_but_keeps_usage(tmp_path):
    db = Database(tmp_path / "cancel.db")
    run = run_with_team()
    run.status = RunStatus.CANCELLED
    db.save_run(run)
    executor = Executor(db, object(), {m.id:m for m in models()})
    await executor._delegate_result(run.id, "parent", request(), ProviderResult("request", 100, 100, "test:build"), [], [], 0.0002, 0)
    run = db.get_run(run.id)
    assert run.status == RunStatus.CANCELLED and len(run.plan.nodes) == 1
    assert run.plan.nodes[0].attempts[0].outcome == "cancelled"
    assert run.actual_cost_usd == pytest.approx(0.0002)


async def test_restart_resumes_native_parent_history_with_verified_child_result(tmp_path):
    from super_partner.models import NodeAttempt
    db = Database(tmp_path / "resume.db")
    run = run_with_team()
    db.save_run(run)
    first = Executor(db, object(), {m.id:m for m in models()})
    conversation = first._conversation(run, run.plan.nodes[0])
    conversation.model_id = "test:build"
    conversation.state = {"provider": "openai", "history": [{"role": "assistant", "content": "saved-progress"}]}
    conversation.save()
    child = team.delegate(run, run.plan.nodes[0], request(), models(), {m.id for m in models()})[0]
    run.plan.nodes[0].attempts.append(NodeAttempt(model_id="test:build", outcome="delegated"))
    child.status, child.result = NodeStatus.SUCCEEDED, "checked boundary evidence"
    child.attempts.append(NodeAttempt(model_id="test:verify", outcome="passed"))
    db.save_run(run)
    second = Executor(db, object(), {m.id:m for m in models()})
    restored = second._conversation(db.get_run(run.id), db.get_run(run.id).plan.nodes[0])
    assert restored.state["history"][0]["content"] == "saved-progress"
    assert "checked boundary evidence" in restored.followup


@pytest.mark.parametrize("capacity,expected", [(1, ["parent"]), (3, ["parent", "other"])])
async def test_dispatch_serializes_one_employee_and_obeys_provider_capacity(tmp_path, monkeypatch, capacity, expected):
    monkeypatch.setattr(settings, "provider_limits", {"test": {"concurrency": capacity}})
    db = Database(tmp_path / "dispatch.db")
    run = run_with_team()
    run.plan.nodes += [run.plan.nodes[0].model_copy(update={"id": "next"}),
                      run.plan.nodes[0].model_copy(update={"id": "other", "employee_id": "tester", "model_id": "test:verify"})]
    db.save_run(run)
    executor = Executor(db, object(), {m.id:m for m in models()})
    started, release = [], asyncio.Event()
    async def worker(_, node_id):
        started.append(node_id)
        await release.wait()
    monkeypatch.setattr(executor, "_execute_node", worker)
    running = {}
    executor._dispatch(run, running)
    await asyncio.sleep(0)
    assert started == expected
    release.set()
    await asyncio.gather(*running)


async def test_delegated_requests_share_ancestor_cost_and_turn_limits_after_restart(tmp_path):
    from artifact_helpers import contract_node
    from super_partner.budget import StepLimitExceeded
    db = Database(tmp_path / "budget.db")
    run = run_with_team()
    parent = contract_node("parent", stage=None, output="app.py", employee_id="builder")
    parent.model_id = "test:build"
    parent.artifact_contract.max_turns = 2
    run.plan.nodes = [parent]
    child = team.delegate(run, parent, request(), models(), {m.id for m in models()})[0]
    db.save_run(run)
    async def call(budget):
        await budget.begin_turn()
        await budget.record("test:verify", 100, 100)
        return ProviderResult("done", 100, 100, "test:verify")
    for _ in range(2):
        executor = Executor(db, object(), {m.id:m for m in models()})
        await executor._charged(run, child, "test:verify", call, work=True)
    with pytest.raises(StepLimitExceeded):
        await executor._charged(run, child, "test:verify", call, work=True)
    saved = db.get_run(run.id)
    assert saved.actual_cost_usd == pytest.approx(0.0004)
    assert saved.plan.nodes[0].contract_cost_usd == pytest.approx(0.0004)
    assert saved.plan.nodes[0].contract_turns == 2


def test_refinement_keeps_employee_ids_and_inherits_module_owner():
    from super_partner.outline import Tree, siblings
    tree = Tree()
    owner = tree.add(siblings([{"key":"p", "title":"module", "employee_id":"builder"}], False, parts_only=True), None)[0]
    tree.add(siblings([{"key":"n", "instructions":"implement", "capability":"coding", "files":["a.py"]},
                       {"key":"v", "instructions":"verify", "capability":"research", "employee_id":"tester"}], True), owner.id)
    assert [n.employee_id for n in tree.plan("module").nodes] == ["builder", "tester"]


def test_named_skill_is_carried_even_when_task_does_not_mention_tool(monkeypatch):
    from super_partner.skills import Skill
    run = run_with_team()
    run.plan.employees[0].skills = ["special-guide"]
    team.refresh_skills(run.plan, [Skill(name="special-guide", description="guide", tools=["special"], body="verified invocation procedure")])
    prompt, _ = node_prompt(run.plan.nodes[0], run, 1000)
    assert "verified invocation procedure" in prompt


def test_replanning_preserves_employee_assignment_and_explicit_model():
    from super_partner.replan import change_plan
    run = run_with_team()
    change_plan(run, {"add": [{"key": "new", "title": "research", "instructions":"investigate",
                "employee_id":"tester", "capability":"research", "acceptance_criteria":"evidence"}]}, models(), {m.id for m in models()})
    assert run.plan.nodes[-1].employee_id == "tester" and run.plan.nodes[-1].model_id == "test:verify"


async def test_failed_child_blocks_parent_from_claiming_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "review_nodes", False)
    monkeypatch.setattr(settings, "lead_decisions", False)
    db = Database(tmp_path / "child-failure.db")
    db.save_run(run_with_team())
    calls = []
    class Workers:
        def configured_models(self, _):
            return {m.id for m in models()}
        async def complete(self, model_id, prompt, workspace=None, **kwargs):
            calls.append(model_id)
            return ProviderResult("DELEGATE_TASKS\n" + json.dumps(request()) if len(calls) == 1 else "BLOCKED: missing evidence", 100, 100, model_id)
    executor = Executor(db, Workers(), {m.id:m for m in models()})
    await asyncio.wait_for(executor.execute("team-run"), 5)
    saved = db.get_run("team-run")
    assert saved.status == RunStatus.INCOMPLETE
    assert calls == ["test:build", "test:verify"]
    assert saved.plan.nodes[0].status != "succeeded"


async def test_already_finished_node_cannot_publish_a_second_child_graph(tmp_path):
    db = Database(tmp_path / "late.db")
    run = run_with_team()
    run.status = RunStatus.RUNNING
    run.plan.nodes[0].status = NodeStatus.SUCCEEDED
    db.save_run(run)
    executor = Executor(db, object(), {m.id:m for m in models()})
    await executor._delegate_result(run.id, "parent", request(), ProviderResult("request", 0, 0, "test:build"), [], [], 0, 0)
    restored = db.get_run(run.id)
    assert len(restored.plan.nodes) == 1
    assert restored.plan.nodes[0].status == NodeStatus.SUCCEEDED

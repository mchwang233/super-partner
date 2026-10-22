"""The initial task list is executable as-is; there is no later model refinement."""
import asyncio
import json

import pytest
from artifact_helpers import coverage_nodes, raw
from super_partner.config import settings
from super_partner.contracts import ContractError
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import ExecutionRun, RunStatus, SupervisorSession, TaskPlan
from super_partner.planner import autonomous_plan
from super_partner.providers import ProviderResult
from test_planning_latency import CONFIGURED, MODELS, ROSTER

TASKS = {"summary": "交付模块", "folder": "small-module", "requires": ["python --version"], "nodes": [
    {"key": "build", "title": "实现模块", "instructions": "完成模块和本任务自测", "capability": "coding",
     "employee_id": "builder", "acceptance_criteria": "功能通过自测", "provides": "public interface"},
    {"key": "check", "title": "验收模块", "instructions": "根据依赖产物进行验收", "capability": "coding",
     "employee_id": "builder", "dependencies": ["build"], "verifies": ["build"],
     "acceptance_criteria": "列出验收证据"}]}


@pytest.fixture(autouse=True)
def direct_policy(monkeypatch):
    monkeypatch.setattr(settings, "employee_teams", True)
    monkeypatch.setitem(settings.__dict__, "direct_task_planning", True)


class Planner:
    def __init__(self, payload=None):
        self.payload = payload if payload is not None else TASKS
        self.messages = []

    async def converse(self, session, text, **kwargs):
        self.messages.append(text)
        return ProviderResult(json.dumps(ROSTER if "TEAM_SETUP" in text else self.payload), 10, 10, session.model_id)


async def planned(provider=None, prompt="实现模块"):
    return await autonomous_plan(prompt, MODELS, CONFIGURED, provider or Planner(), session=SupervisorSession())


async def test_team_then_tasks_contains_dependencies_without_refinement():
    provider = Planner()
    plan = await planned(provider)
    assert len(provider.messages) == 2
    assert "DIRECT_TASK_PLAN" in provider.messages[1]
    assert "10-15 tool turns" not in provider.messages[1]
    assert not plan.parts and len(plan.nodes) == 2
    assert plan.nodes[1].dependencies == plan.nodes[1].verifies == [plan.nodes[0].id]
    assert all(n.employee_id == "builder" for n in plan.nodes)
    assert plan.requires == ["python --version"] and plan.folder == "small-module"
    assert "不再细化" in plan.planning_note
    assert TaskPlan.model_validate_json(plan.model_dump_json()) == plan


async def test_one_complete_task_is_enough_without_forced_splitting():
    plan = await planned(Planner({**TASKS, "nodes": TASKS["nodes"][:1]}))
    assert len(plan.nodes) == 1 and not plan.parts


async def test_verification_of_transitive_dependencies_needs_no_model_correction():
    payload = {**TASKS, "nodes": [TASKS["nodes"][0], {**TASKS["nodes"][0], "key": "report", "dependencies": ["build"]},
        {**TASKS["nodes"][1], "dependencies": ["report"], "verifies": ["build", "report"]}]}
    provider = Planner(payload)
    plan = await planned(provider)
    assert len(provider.messages) == 2  # team + tasks, no regeneration of a valid dependency graph
    first, second, audit = plan.nodes
    assert set(audit.dependencies) == set(audit.verifies) == {first.id, second.id}
    assert payload["nodes"][-1]["dependencies"] == ["report"]  # raw input is not rewritten


@pytest.mark.parametrize("verified", ["missing", "independent"])
async def test_verification_cannot_add_unknown_or_unrelated_work(verified):
    payload = {**TASKS, "nodes": [{**TASKS["nodes"][0], "key": "independent"}, TASKS["nodes"][0],
        {**TASKS["nodes"][1], "verifies": [verified]}]}
    with pytest.raises(ContractError):
        await planned(Planner(payload))


@pytest.mark.parametrize("change", [{"dependencies": ["missing"]}, {"employee_id": "missing"},
                                  {"dependencies": ["build"]}, {"acceptance_criteria": ""}])
async def test_invalid_task_graph_or_assignment_is_not_executed(change):
    payload = {**TASKS, "nodes": [{**TASKS["nodes"][0], **change}]}
    with pytest.raises(ContractError):
        await planned(Planner(payload))


async def test_direct_plan_preserves_artifact_handoffs_and_limits():
    payload = {"summary": "coverage", "nodes": [{**raw(n), "employee_id": "builder"} for n in coverage_nodes()]}
    plan = await planned(Planner(payload), "Collect VDB, produce URG report, audit the delivery")
    assert [n.artifact_contract.stage for n in plan.nodes] == ["coverage_vdb", "urg_report", "delivery_audit"]
    assert plan.nodes[-1].verifies == [plan.nodes[1].id]
    assert all(n.artifact_contract.max_turns == 3 for n in plan.nodes)
    with pytest.raises(ContractError):
        await planned(Planner(), "Collect VDB and produce URG report")


async def test_direct_tasks_dispatch_in_dependency_order_without_refiner(tmp_path, monkeypatch):
    plan = await planned()
    for node in plan.nodes:
        node.model_id = "test:first"
    run = ExecutionRun(id="direct", prompt="实现模块", status=RunStatus.RUNNING, plan=plan, budget_usd=10)
    database = Database(tmp_path / "task.db")
    executor = Executor(database, Planner(), {m.id: m for m in MODELS})
    started = []

    async def execute(run_id, node_id):
        started.append(node_id)

    async def refine(*args):
        pytest.fail("direct tasks must never invoke a refinement model")

    monkeypatch.setattr(executor, "_execute_node", execute)
    monkeypatch.setattr(executor, "_split_part", refine)
    running = {}
    executor._dispatch(run, running)
    await asyncio.gather(*running)
    assert started == [plan.nodes[0].id]
    plan.nodes[0].status = "succeeded"
    running = {}
    executor._dispatch(run, running)
    await asyncio.gather(*running)
    assert started == [n.id for n in plan.nodes]
    database.connection.close()


async def test_api_keeps_employee_model_at_creation_and_confirmation(task_api, monkeypatch):
    api, client = task_api
    catalog = [m.model_copy(update={"input_per_million": 10 if i == 0 else 1,
                                    "output_per_million": 10 if i == 0 else 1}) for i, m in enumerate(MODELS)]

    async def make_plan(*args, **kwargs):
        return await planned()

    monkeypatch.setattr(api, "autonomous_plan", make_plan)
    monkeypatch.setattr(api, "models", {m.id: m for m in catalog})
    monkeypatch.setattr(api.providers, "configured_models", lambda ids: CONFIGURED)
    monkeypatch.setattr(api.executor, "start", lambda run_id: None)
    response = await client.post("/api/tasks", json={"prompt": "实现模块", "use_cli": False, "budget_usd": 10},
                                 headers={"origin": "http://localhost:5173"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert all(n["model_id"] == "test:first" for n in body["plan"]["nodes"])
    # An unavailable employee must be reassigned explicitly, not silently replaced at confirm.
    monkeypatch.setattr(api.providers, "configured_models", lambda ids: {"test:second"})
    rejected = await client.post(f"/api/tasks/{body['id']}/confirm", json={},
                                 headers={"origin": "http://localhost:5173"})
    assert rejected.status_code == 422, rejected.text
    assert api.db.get_run(body["id"]).status == RunStatus.AWAITING_CONFIRMATION

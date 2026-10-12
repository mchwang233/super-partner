from pathlib import Path

import pytest
from super_partner.catalog import builtin_models
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import ExecutionRun, NodeStatus, RunStatus, TaskNode, TaskPlan


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8000", "192.168.1.5:8000"])
async def test_rebinding_hosts_are_refused(task_api, host):
    _, client = task_api
    response = await client.get("/api/tasks", headers={"host": host})
    assert response.status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost", "LOCALHOST:8000", "[::1]:8000"])
async def test_local_hosts_are_served(task_api, host):
    _, client = task_api
    assert (await client.get("/api/tasks", headers={"host": host})).status_code == 200


async def test_cross_site_writes_are_refused_but_console_and_cli_writes_pass(task_api):
    _, client = task_api
    url = "/api/tasks/missing/cancel"
    assert (await client.post(url, headers={"origin": "https://evil.example"})).status_code == 403
    assert (await client.post(url, headers={"origin": "null"})).status_code == 403
    assert (await client.post(url, headers={"origin": "http://localhost:5173"})).status_code == 404
    assert (await client.post(url)).status_code == 404


async def test_startup_pauses_interrupted_runs_instead_of_resuming(task_api, monkeypatch):
    api, _ = task_api
    monkeypatch.setattr(api.catalog_sync, "start", lambda: None)
    for run_id, status in [("running", RunStatus.RUNNING), ("queued", RunStatus.QUEUED)]:
        api.db.save_run(ExecutionRun(
            id=run_id, prompt="demo", status=status, budget_usd=1,
            plan=TaskPlan(summary="demo", nodes=[]),
        ))
    async with api.lifespan(api.app):
        for run_id in ("running", "queued"):
            assert api.db.get_run(run_id).status == RunStatus.PAUSED
            assert run_id not in api.executor.tasks
            events = api.db.events(run_id)
            assert [(event.kind, event.data) for event in events] == [
                ("approval.required", {"reason": "interrupted"})
            ]


def fallback_executor(tmp_path: Path, **overrides):
    catalog = {model.id: model for model in builtin_models()}
    for model_id, enabled in overrides.items():
        catalog[model_id].enabled = enabled
    return Executor(Database(tmp_path / "test.db"), None, catalog)


def cli_run(budget: float, actual: float = 0.0, other_running_cost: float = 0.0):
    node = TaskNode(id="cli", title="CLI", instructions="do", capability="coding",
                    model_id="cli:claude")
    other = TaskNode(id="api", title="API", instructions="do", capability="coding",
                     model_id="anthropic:claude-sonnet-5", status=NodeStatus.RUNNING,
                     estimated_cost_usd=other_running_cost)
    return ExecutionRun(id="run", prompt="demo", status=RunStatus.RUNNING, budget_usd=budget,
                        actual_cost_usd=actual, plan=TaskPlan(summary="demo", nodes=[node, other]))


def test_fallback_needs_the_api_model_enabled(tmp_path):
    run = cli_run(budget=100)
    node = run.plan.nodes[0]
    assert fallback_executor(tmp_path)._fallback_allowed(run, node)
    disabled = fallback_executor(tmp_path, **{"anthropic:claude-sonnet-5": False})
    assert not disabled._fallback_allowed(run, node)


def test_fallback_must_fit_the_budget_left_by_spent_and_running_nodes(tmp_path):
    executor = fallback_executor(tmp_path)
    assert not executor._fallback_allowed(cli_run(budget=1e-9), cli_run(1e-9).plan.nodes[0])
    assert not executor._fallback_allowed(cli_run(budget=1, actual=1), cli_run(1).plan.nodes[0])
    crowded = cli_run(budget=1, other_running_cost=1)
    assert not executor._fallback_allowed(crowded, crowded.plan.nodes[0])


def test_non_cli_nodes_are_unaffected(tmp_path):
    run = cli_run(budget=0.0001)
    assert fallback_executor(tmp_path)._fallback_allowed(run, run.plan.nodes[1])


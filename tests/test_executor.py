from pathlib import Path

import pytest
from super_partner.catalog import builtin_models
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import ExecutionRun, RunStatus
from super_partner.planner import heuristic_plan
from super_partner.providers import ProviderRegistry
from super_partner.routing import estimate, route_node


@pytest.mark.asyncio
async def test_mock_execution_completes(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    catalog = builtin_models()
    plan = heuristic_plan("Research and implement a small feature")
    for node in plan.nodes:
        route_node(node, catalog, {"mock:local"})
    plan.estimate = estimate(plan.nodes, 1)
    run = ExecutionRun(id="demo", prompt="demo", status=RunStatus.QUEUED, plan=plan, budget_usd=1)
    database.save_run(run)
    providers = ProviderRegistry()
    executor = Executor(database, providers, {model.id: model for model in catalog})
    await executor.execute(run.id)
    result = database.get_run(run.id)
    await providers.client.aclose()
    assert result is not None
    assert result.status == RunStatus.SUCCEEDED
    assert result.final_result
    assert all(node.result for node in result.plan.nodes)

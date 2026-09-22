from pathlib import Path

from super_partner.catalog import builtin_models
from super_partner.db import Database
from super_partner.models import ExecutionRun, RunStatus
from super_partner.planner import autonomous_plan, heuristic_plan
from super_partner.providers import ProviderRegistry, ProviderResult
from super_partner.routing import estimate, route_node


def test_planner_builds_valid_dependency_graph():
    plan = heuristic_plan("Research the options and implement the code")
    seen = set()
    for node in plan.nodes:
        assert set(node.dependencies) <= seen
        seen.add(node.id)
    assert plan.nodes[-1].capability == "structured_output"


def test_router_chooses_cheapest_eligible_model():
    models = builtin_models()
    node = heuristic_plan("Analyze this task").nodes[0]
    route_node(node, models, {model.id for model in models})
    assert node.model_id == "cli:codex"
    assert node.estimated_cost_usd == 0


def test_router_prefers_cli_over_cheaper_api():
    models = builtin_models()
    node = heuristic_plan("Implement this feature").nodes[0]
    route_node(node, models, {"cli:claude", "mock:local"})
    assert node.model_id == "cli:claude"


def test_router_can_exclude_all_cli_channels():
    models = builtin_models()
    node = heuristic_plan("Implement this feature").nodes[0]
    route_node(node, models, {"openai:gpt-5.6-luna", "mock:local"})
    assert node.model_id == "openai:gpt-5.6-luna"


def test_router_never_selects_disabled_model():
    models = builtin_models()
    next(model for model in models if model.id == "cli:codex").enabled = False
    node = heuristic_plan("Implement this feature").nodes[0]
    route_node(node, models, {"cli:codex", "cli:claude"})
    assert node.model_id == "cli:claude"


def test_estimate_includes_reserves():
    nodes = heuristic_plan("Analyze this task").nodes
    for node in nodes:
        node.estimated_cost_usd = 0.01
    quote = estimate(nodes, 2)
    assert quote.high_usd > quote.base_usd
    assert quote.hard_limit_usd == 2


def test_estimate_includes_planning_cost():
    quote = estimate([], 2, planning_cost=.25)
    assert quote.base_usd == .25


async def test_autonomous_planner_builds_model_generated_dag():
    class FakeProviders:
        async def complete(self, *_args, **_kwargs):
            return ProviderResult(
                text='{"summary":"Build it","nodes":['
                '{"key":"n1","title":"Inspect","instructions":"Inspect inputs",'
                '"capability":"planning","dependencies":[],"estimated_input_tokens":900,'
                '"estimated_output_tokens":400,"quality_threshold":0.7},'
                '{"key":"n2","title":"Implement","instructions":"Implement and test",'
                '"capability":"coding","dependencies":["n1"],"estimated_input_tokens":2400,'
                '"estimated_output_tokens":1600,"quality_threshold":0.8}]}',
                input_tokens=100, output_tokens=100, used_model_id="openai:gpt-5.6-sol",
            )

    catalog = builtin_models()
    plan = await autonomous_plan(
        "Build it", catalog, {"openai:gpt-5.6-sol"}, FakeProviders(),
    )
    assert plan.planning_method == "model"
    assert plan.planner_model_id == "openai:gpt-5.6-sol"
    assert plan.nodes[1].dependencies == [plan.nodes[0].id]
    assert plan.planning_cost_usd > 0


def test_database_round_trip(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    plan = heuristic_plan("Analyze this task")
    run = ExecutionRun(id="run-1", prompt="Analyze this task", status=RunStatus.AWAITING_CONFIRMATION, plan=plan, budget_usd=1)
    database.save_run(run)
    loaded = database.get_run("run-1")
    assert loaded is not None
    assert loaded.prompt == run.prompt
    assert database.list_runs()[0].id == "run-1"


def test_new_chinese_providers_are_routable(monkeypatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "test")
    monkeypatch.setenv("MINIMAX_API_KEY", "test")
    monkeypatch.setenv("MOONSHOT_API_KEY", "test")
    registry = ProviderRegistry()
    model_ids = [model.id for model in builtin_models()]
    configured = registry.configured_models(model_ids)
    assert {"glm:glm-5.2", "minimax:MiniMax-M2.7", "kimi:kimi-k3"} <= configured

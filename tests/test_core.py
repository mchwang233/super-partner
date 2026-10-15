from pathlib import Path

from conftest import scored_models
from super_partner.catalog import builtin_models
from super_partner.cli_channels import CLI_SPECS
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


def test_router_chooses_cheapest_eligible_api_model():
    models = scored_models()
    node = heuristic_plan("Analyze this task").nodes[0]
    configured = {model.id for model in models if model.provider != "mock"}
    route_node(node, models, configured)
    api = [m for m in models if m.channel != "cli" and m.id in configured]
    assert node.model_id == min(api, key=lambda m: m.input_per_million * 1500 + m.output_per_million * 800).id
    assert node.routing_note.startswith("子任务优先用 API 模型")


def test_subtasks_use_a_cli_only_when_no_api_model_qualifies(monkeypatch):
    from super_partner.config import settings
    models = scored_models()
    luna = next(model for model in models if model.id == "openai:gpt-5.6-luna")
    node = heuristic_plan("Implement this feature").nodes[0]
    route_node(node, models, {"cli:claude", luna.id})
    assert node.model_id == luna.id
    luna.scores = luna.scores.model_copy(update={node.capability: node.quality_threshold - 5})
    route_node(node, models, {"cli:claude", luna.id})
    assert node.model_id == "cli:claude"
    assert "API 模型最高只有" in node.routing_note and "改用会员 CLI" in node.routing_note
    # With the policy off, the free membership CLI comes first again.
    monkeypatch.setattr(settings, "subtasks_prefer_api", False)
    luna.scores = luna.scores.model_copy(update={node.capability: 95})
    route_node(node, models, {"cli:claude", luna.id})
    assert node.model_id == "cli:claude"


def test_router_can_exclude_all_cli_channels():
    models = scored_models()
    node = heuristic_plan("Implement this feature").nodes[0]
    route_node(node, models, {"openai:gpt-5.6-luna", "mock:local"})
    assert node.model_id == "openai:gpt-5.6-luna"


def test_router_never_selects_disabled_model():
    models = scored_models()
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

    catalog = scored_models()
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


def test_database_prunes_retired_models(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    catalog = builtin_models()
    retired = catalog[0].model_copy(update={"id": "anthropic:retired"})
    database.save_models([*catalog, retired])
    database.delete_models_except(model.id for model in catalog)
    assert {model.id for model in database.list_models()} == {model.id for model in catalog}


def test_cli_fallbacks_reference_catalog_models():
    catalog_ids = {model.id for model in builtin_models()}
    assert {spec.fallback_model_id for spec in CLI_SPECS.values()} <= catalog_ids


def _priced(model_id: str, priority: int, price: float, coding: float):
    from super_partner.models import CapabilityScore, ModelProfile
    return ModelProfile(
        id=model_id, provider="x", display_name=model_id, context_window=200_000,
        input_per_million=price, output_per_million=price, execution_priority=priority,
        scores=CapabilityScore(coding=coding, reliability=90),
    )


def test_difficulty_decides_between_the_cheapest_and_the_strongest_model():
    from super_partner.models import TaskNode
    models = [_priced("cheap", 100, .1, 82), _priced("strong", 100, 5, 97), _priced("cli", 0, 0, 91)]
    configured = {model.id for model in models}

    def routed(difficulty: str) -> str:
        node = TaskNode(id="n", title="t", instructions="i", capability="coding",
                        difficulty=difficulty, quality_threshold={"easy": 70, "normal": 80, "hard": 90}[difficulty])
        return route_node(node, models, configured).model_id

    # Easy and normal: membership CLI first, then the cheapest that clears the threshold.
    assert routed("easy") == routed("normal") == "cli"
    without_cli = [model for model in models if model.id != "cli"]
    node = TaskNode(id="n", title="t", instructions="i", capability="coding", quality_threshold=80)
    assert route_node(node, without_cli, configured).model_id == "cheap"
    # Hard: the highest score wins even though it costs more than the free CLI.
    assert routed("hard") == "strong"


def test_planner_difficulty_sets_the_threshold_and_old_plans_derive_it():
    from super_partner.models import TaskNode
    from super_partner.planner import _validate_plan
    plan = _validate_plan({"nodes": [
        {"key": "a", "instructions": "x", "difficulty": "hard"},
        {"key": "b", "instructions": "y", "dependencies": ["a"], "difficulty": "Easy"},
        {"key": "c", "instructions": "z", "quality_threshold": .8},
    ]})
    assert [(n.difficulty, n.quality_threshold) for n in plan.nodes] == [("hard", 90), ("easy", 70), ("normal", 80)]
    saved = TaskNode(id="n", title="t", instructions="i", capability="coding", quality_threshold=.92)
    assert (saved.difficulty, saved.quality_threshold) == ("hard", 92)


def test_reviews_also_go_to_api_models_first():
    from super_partner.routing import pick_reviewer
    models = scored_models()
    configured = {"cli:claude", "openai:gpt-5.6-luna"}
    assert pick_reviewer(models, configured, exclude="openai:gpt-6", tokens=1000).id == "openai:gpt-5.6-luna"
    assert pick_reviewer(models, {"cli:claude"}, exclude=None, tokens=1000).id == "cli:claude"

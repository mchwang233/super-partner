"""A task's running steps are spread over API providers, so one provider's outage or rate limit
does not stop them all (Executor._spread, routing.spread_to)."""
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import (
    CapabilityScore,
    ExecutionRun,
    ModelProfile,
    NodeStatus,
    RunStatus,
    TaskNode,
    TaskPlan,
)
from super_partner.routing import spread_to


def api(model_id: str, price: float, coding: float = 90) -> ModelProfile:
    return ModelProfile(id=model_id, provider=model_id.split(":")[0], display_name=model_id, context_window=1_000_000,
                        input_per_million=price, output_per_million=price * 4, cached_input_per_million=price / 10,
                        scores=CapabilityScore(coding=coding, planning=85, reliability=90))


CHEAP, NEAR, DEAR, WEAK = api("deepseek:flash", 0.15), api("minimax:m3", 0.3), api("glm:glm", 1.2), api("kimi:k", 0.2, 60)
MODELS = {model.id: model for model in (CHEAP, NEAR, DEAR, WEAK)}


class Providers:
    def configured_models(self, ids):
        return set(ids)


def step(node_id: str, status=NodeStatus.PENDING, difficulty="normal") -> TaskNode:
    return TaskNode(id=node_id, title=node_id, instructions="i", capability="coding", model_id=CHEAP.id,
                    status=status, difficulty=difficulty, quality_threshold=80)


def started(tmp_path, nodes):
    database = Database(tmp_path / "t.db")
    database.save_run(ExecutionRun(id="r", prompt="p", status=RunStatus.RUNNING, budget_usd=5, use_cli=False,
                                   plan=TaskPlan(summary="s", nodes=nodes)))
    return database, Executor(database, Providers(), MODELS)


def test_spreading_is_off_unless_set():
    assert settings.max_steps_per_provider == 0


def test_a_step_past_the_providers_share_goes_to_another_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "max_steps_per_provider", 2)
    database, executor = started(tmp_path, [step("a", NodeStatus.RUNNING), step("b", NodeStatus.RUNNING), step("c")])
    run = database.get_run("r")
    node = run.plan.nodes[2]
    executor._spread(run, node)
    # The cheapest other provider that clears the threshold; the weak one does not, the dear one costs too much.
    assert node.model_id == "minimax:m3"
    event = [e for e in database.events("r") if e.kind == "node.model_switched"][-1]
    assert event.data["reason"] == "spread_across_providers" and event.data["model_id"] == "deepseek:flash"


def test_a_step_stays_when_its_provider_has_room_or_no_other_fits(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "max_steps_per_provider", 2)
    database, executor = started(tmp_path, [step("a", NodeStatus.RUNNING), step("b")])
    run = database.get_run("r")
    executor._spread(run, run.plan.nodes[1])
    assert run.plan.nodes[1].model_id == CHEAP.id  # one running on it: room for another
    # Past the share, but the only other able model costs more than spread_cost_factor times as much.
    busy = {"deepseek"}
    assert spread_to(step("c"), [CHEAP, DEAR, WEAK], set(MODELS), busy) is None
    monkeypatch.setattr(settings, "spread_cost_factor", 20.0)
    assert spread_to(step("c"), [CHEAP, DEAR, WEAK], set(MODELS), busy).id == "glm:glm"
    # Off when the setting is 0.
    monkeypatch.setattr(settings, "max_steps_per_provider", 0)
    database, executor = started(tmp_path / "off", [step("a", NodeStatus.RUNNING), step("b", NodeStatus.RUNNING), step("c")])
    run = database.get_run("r")
    executor._spread(run, run.plan.nodes[2])
    assert run.plan.nodes[2].model_id == CHEAP.id

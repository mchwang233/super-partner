"""A step the rest of the task waits for gets a fast enough model, and the lead hears when it runs
far too long (routing.close_to_best, Review._critical_check)."""
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import (
    CapabilityScore,
    ExecutionRun,
    ModelProfile,
    NodeStatus,
    RunStatus,
    SupervisorSession,
    TaskNode,
    TaskPlan,
)
from super_partner.routing import route_node


def api(model_id: str, coding: float, price: float) -> ModelProfile:
    return ModelProfile(id=model_id, provider=model_id.split(":")[0], display_name=model_id, context_window=1_000_000,
                        input_per_million=price, output_per_million=price * 4, cached_input_per_million=price / 10,
                        scores=CapabilityScore(coding=coding, planning=90, reliability=90))


def hard() -> TaskNode:
    return TaskNode(id="n", title="RTL", instructions="i", capability="coding", difficulty="hard", quality_threshold=90)


def test_a_hard_step_takes_a_cheaper_model_a_point_or_two_below_the_best(monkeypatch):
    flash, glm, kimi = api("deepseek:flash", 92.3, 0.15), api("glm:glm", 93.9, 1.2), api("kimi:k", 96.5, 3.0)
    ids = {flash.id, glm.id, kimi.id}
    # 93.9 against 92.3: DeepSeek Flash, many times cheaper, is close enough.
    assert route_node(hard(), [flash, glm], ids).model_id == "deepseek:flash"
    # A model far above the rest is still worth its price.
    assert route_node(hard(), [flash, glm, kimi], ids).model_id == "kimi:k"
    monkeypatch.setattr(settings, "hard_score_margin", 1.0)
    assert route_node(hard(), [flash, glm], ids).model_id == "glm:glm"


class Lead:
    def configured_models(self, ids):
        return set(ids)

    async def converse(self, *args, **kwargs):
        raise AssertionError("not used")


def waiting_task(tmp_path, blocked=True):
    database = Database(tmp_path / "t.db")
    first = TaskNode(id="rtl", title="实现 RTL", instructions="i", capability="coding", difficulty="hard",
                     status=NodeStatus.RUNNING, model_id="glm:glm")
    after = TaskNode(id="sva", title="SVA 断言", instructions="i", capability="coding",
                     dependencies=["rtl"] if blocked else [])
    database.save_run(ExecutionRun(id="r", prompt="p", status=RunStatus.RUNNING, budget_usd=5, plan=TaskPlan(
        summary="s", nodes=[first, after]), supervisor=SupervisorSession(model_id="cli:codex", cwd=str(tmp_path))))
    executor = Executor(database, Lead(), {})
    return database, executor, first


async def test_a_step_others_wait_for_is_put_to_the_lead_when_far_past_its_time(tmp_path, monkeypatch):
    database, executor, node = waiting_task(tmp_path)
    asked = []

    async def ask_lead(run_id, message):
        asked.append(message)
        return "还在调试自己的临时测试平台，拖住了后面所有步骤\nVERDICT: STOP: 太慢，换更快的模型"
    monkeypatch.setattr(executor, "ask_lead", ask_lead)
    expected = 25 * settings.minutes_per_turn  # a hard step's turns
    count = [0]
    # Not yet far past its time: nothing asked.
    assert await executor._critical_check("r", node, "glm:glm", expected * 1.5, "t17 Edit tb.sv", count) is None
    reason = await executor._critical_check("r", node, "glm:glm", expected * 2.1, "t17 Edit tb.sv", count)
    assert reason.startswith("这一步挡着后面的步骤") and "换更快的模型" in reason
    [message] = asked
    assert "SVA 断言" in message and "glm:glm" in message and "switch_model" in message
    [event] = [e for e in database.events("r") if e.kind == "node.critical_overrun_check"]
    assert event.data["stop"] and event.data["waiting"] == 1


async def test_a_step_nothing_waits_for_runs_as_long_as_it_needs(tmp_path, monkeypatch):
    _database, executor, node = waiting_task(tmp_path, blocked=False)

    async def ask_lead(run_id, message):
        raise AssertionError("the lead is not asked")
    monkeypatch.setattr(executor, "ask_lead", ask_lead)
    assert await executor._critical_check("r", node, "glm:glm", 500, "evidence", [0]) is None

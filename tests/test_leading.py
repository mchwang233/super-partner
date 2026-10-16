"""A failed attempt goes to the task's lead, which decides what to do; that is then done."""
import json

import pytest
from super_partner import quality
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import (
    DiagnosisMessage,
    ExecutionRun,
    NodeStatus,
    RunStatus,
    SupervisorSession,
    TaskNode,
    TaskPlan,
)
from super_partner.providers import ProviderResult
from test_turns import model

MODELS = {m.id: m for m in [model("x:api", 90), model("cli:claude", 96, "cli")]}


def decision(action, **fields):
    return json.dumps({"action": action, "reason": f"理由：{action}", **fields}, ensure_ascii=False)


class Team:
    """Workers fail while ``fails`` lasts; the lead answers from ``decisions`` in order."""

    def __init__(self, decisions, fails=1, review=None):
        self.decisions, self.fails, self.review = list(decisions), fails, review
        self.work, self.lead, self.diagnoses = [], [], []

    def configured_models(self, ids):
        return set(ids)

    async def converse(self, session, prompt, spend_ok=None, timeout=600):
        self.lead.append(prompt)
        session.session_id = session.session_id or "lead-1"
        return ProviderResult(self.decisions.pop(0) if self.decisions else "不知道", 0, 0, session.model_id)

    async def complete(self, model_id, prompt, workspace=None, **kwargs):
        if kwargs.get("repair"):
            self.diagnoses.append(prompt)
            return ProviderResult("## 修复内容\n装好了缺的工具\nSTATUS: FIXED", 0, 0, model_id)
        if prompt.startswith(quality.REVIEW_MARKER):
            return ProviderResult(json.dumps(self.review), 0, 0, model_id)
        self.work.append((model_id, prompt))
        if len(self.work) <= self.fails:
            raise RuntimeError("tests failed: 3 errors")
        return ProviderResult("做完了", 0, 0, model_id)


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "review_nodes", False)
    monkeypatch.setattr(settings, "auto_diagnose", True)
    monkeypatch.setattr(settings, "lead_decisions", True)
    database = Database(tmp_path / "test.db")
    node = TaskNode(id="a", title="实现", instructions="写代码", capability="coding", model_id="x:api")
    database.save_run(ExecutionRun(
        id="r", prompt="写一个工具", status=RunStatus.QUEUED, budget_usd=1,
        supervisor=SupervisorSession(model_id="cli:claude", session_id="lead-1", cwd=str(tmp_path / "lead")),
        plan=TaskPlan(summary="s", nodes=[node])))
    return database


async def run(database, team):
    executor = Executor(database, team, MODELS)
    await executor.execute("r")
    return executor, database.get_run("r")


async def test_the_lead_decides_to_retry_with_instructions(database):
    team = Team([decision("retry", instructions="先运行测试看清哪三个失败")])
    _, result = await run(database, team)
    assert result.status == RunStatus.SUCCEEDED and len(team.work) == 2
    assert "负责人要求：先运行测试看清哪三个失败" in team.work[1][1]
    # It was told what failed and could choose among the models for the step.
    assert "tests failed: 3 errors" in team.lead[0] and "cli:claude" in team.lead[0]
    [made] = result.decisions
    assert (made.action, made.node_id, made.lead_model_id) == ("retry", "a", "cli:claude")
    assert not result.diagnoses  # no separate diagnosis any more


async def test_the_lead_hands_the_step_to_another_model(database):
    team = Team([decision("switch_model", model_id="cli:claude", instructions="换个更强的模型")])
    _, result = await run(database, team)
    assert [work[0] for work in team.work] == ["x:api", "cli:claude"] and result.status == RunStatus.SUCCEEDED


async def test_the_lead_can_accept_work_a_check_rejected(database, monkeypatch):
    monkeypatch.setattr(settings, "review_nodes", True)
    team = Team([decision("accept")], fails=0, review={"passed": False, "reason": "要求再加一个 --verbose 选项"})
    _, result = await run(database, team)
    node = result.plan.nodes[0]
    assert result.status == RunStatus.SUCCEEDED and node.result == "做完了" and node.review == "负责人接受：理由：accept"
    assert len(team.work) == 1


async def test_the_lead_ends_a_step_that_cannot_be_done_here(database):
    team = Team([decision("unable", user_action="把摄像头接入这台机器")])
    _, result = await run(database, team)
    node = result.plan.nodes[0]
    assert result.status == RunStatus.INCOMPLETE and node.status == NodeStatus.UNABLE
    assert "需要你处理：把摄像头接入这台机器" in node.unable_reason and len(team.work) == 1


async def test_the_lead_asks_the_user_and_acts_on_the_answer(database):
    team = Team([decision("ask_user", question="用 SQLite 还是 JSON 存数据？", options=["SQLite", "JSON"]),
                 decision("retry", instructions="用 SQLite")])
    executor, result = await run(database, team)
    question = result.diagnoses[-1]
    assert result.status == RunStatus.PAUSED and result.waiting_diagnosis_id == question.id
    assert (question.trigger, question.choices) == ("lead", ["SQLite", "JSON"])

    question.messages.append(DiagnosisMessage(text="", choice="SQLite"))
    result.waiting_diagnosis_id = None
    database.save_run(result)
    await executor._lead_answer("r", question.id)
    await executor.tasks["r"]
    final = database.get_run("r")
    assert final.status == RunStatus.SUCCEEDED and "Their answer: SQLite" in team.lead[1]
    assert [item.event for item in final.decisions] == ["attempt_failed", "user_answer"]


async def test_the_lead_sends_a_repair_session_to_fix_the_environment(database):
    team = Team([decision("repair_environment", instructions="装上 iverilog")])
    _, result = await run(database, team)
    assert result.status == RunStatus.SUCCEEDED and len(team.diagnoses) == 1
    assert "装上 iverilog" in team.diagnoses[0]


async def test_without_a_valid_decision_the_usual_diagnosis_takes_over(database):
    team = Team(["我觉得还行", "嗯"])
    _, result = await run(database, team)
    assert [item.action for item in result.decisions] == ["fallback"]
    assert len(team.lead) == 2 and len(team.diagnoses) == 1 and result.status == RunStatus.SUCCEEDED


async def test_past_the_decision_limit_the_task_pauses_for_the_user(database, monkeypatch):
    monkeypatch.setattr(settings, "max_node_decisions", 2)
    team = Team([decision("retry", instructions="再试")] * 5, fails=10)
    _, result = await run(database, team)
    assert result.status == RunStatus.PAUSED and "已决策 2 次" in result.error
    assert len(result.decisions) == 2 and len(team.work) == 3

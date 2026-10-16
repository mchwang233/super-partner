"""A failed attempt goes to the task's lead, which decides what to do; that is then done."""
import json

import pytest
from super_partner import quality
from super_partner.config import settings
from super_partner.executor import Executor
from super_partner.models import (
    DiagnosisMessage,
    NodeStatus,
    RunStatus,
    TaskNode,
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
    assert len(team.diagnoses) == 1 and result.status == RunStatus.SUCCEEDED
    # Asked twice about the failure; then, with every step done, asked to check the whole result.
    assert "EVENT: an attempt failed" in team.lead[0] and "EVENT: every step has finished" in team.lead[2]


async def test_past_the_decision_limit_the_task_pauses_for_the_user(database, monkeypatch):
    monkeypatch.setattr(settings, "max_node_decisions", 2)
    team = Team([decision("retry", instructions="再试")] * 5, fails=10)
    _, result = await run(database, team)
    assert result.status == RunStatus.PAUSED and "已决策 2 次" in result.error
    assert len(result.decisions) == 2 and len(team.work) == 3


class Checked(Team):
    """A build that always finishes, and a verification answering from ``verdicts`` in order."""

    def __init__(self, decisions, verdicts):
        super().__init__(decisions, fails=0)
        self.verdicts = list(verdicts)
        self.verifications = []

    async def complete(self, model_id, prompt, workspace=None, **kwargs):
        if "You are verifying" in prompt:
            self.verifications.append(prompt)
            return ProviderResult(self.verdicts.pop(0) if self.verdicts else "VERDICT: PASS", 0, 0, model_id)
        return await super().complete(model_id, prompt, workspace, **kwargs)


@pytest.fixture
def checked(database):

    current = database.get_run("r")
    current.plan.nodes.append(TaskNode(id="v", title="验证", instructions="跑测试", capability="coding",
                                       model_id="x:api", dependencies=["a"], verifies=["a"]))
    database.save_run(current)
    return database


async def test_the_lead_sends_work_back_with_what_to_change(checked, monkeypatch):
    # Rework rounds no longer cap it: the lead's decision limits do.
    monkeypatch.setattr(settings, "max_rework_rounds", 0)
    team = Checked([decision("rework", targets=["a"], instructions="补上空文件的处理")],
                   ["VERDICT: FAIL: 空文件会崩溃"])
    _, result = await run(checked, team)
    assert result.status == RunStatus.SUCCEEDED and result.rework_rounds == 1
    assert len(team.work) == 2 and "负责人要求：补上空文件的处理" in team.work[1][1]
    assert "空文件会崩溃" in team.lead[0] and '"targets"' in team.lead[0]
    assert [item.action for item in result.decisions] == ["rework"]


async def test_the_lead_overrules_a_verification_that_asks_for_too_much(checked):
    team = Checked([decision("accept")], ["VERDICT: FAIL: 还应该支持 Windows 路径"])
    _, result = await run(checked, team)
    verifier = result.plan.nodes[1]
    assert result.status == RunStatus.SUCCEEDED and len(team.work) == 1 and result.rework_rounds == 0
    assert verifier.review.startswith("负责人接受")


async def test_the_lead_reruns_a_verification_that_got_it_wrong(checked):
    team = Checked([decision("retry", instructions="先装依赖再跑测试")], ["VERDICT: FAIL: 测试找不到模块"])
    _, result = await run(checked, team)
    assert result.status == RunStatus.SUCCEEDED and len(team.work) == 1 and len(team.verifications) == 2
    assert "负责人要求：先装依赖再跑测试" in team.verifications[1]


async def test_without_a_decision_a_failed_verification_sends_the_work_back_as_before(checked):
    team = Checked(["随便", decision("rework", targets=["nope"])], ["VERDICT: FAIL: 空文件会崩溃"])
    _, result = await run(checked, team)
    assert [item.action for item in result.decisions] == ["fallback"]
    assert result.status == RunStatus.SUCCEEDED and result.rework_rounds == 1 and len(team.work) == 2


async def test_the_user_talks_with_the_lead_in_its_conversation(database):
    team = Team(["空文件的处理我会在下一次决策时要求补上。"])
    executor = Executor(database, team, MODELS)
    assert executor.start_lead_chat("r", "记得要处理空文件")
    # One message at a time: the lead is still answering.
    assert not executor.start_lead_chat("r", "还有别的吗")
    pending = database.get_run("r").lead_messages
    assert [(m.role, m.status) for m in pending] == [("user", "done"), ("lead", "pending")]
    await executor.lead_tasks["r"]
    asked, answered = database.get_run("r").lead_messages
    assert asked.text == "记得要处理空文件" and answered.status == "done" and "空文件" in answered.text
    assert "MESSAGE from the user" in team.lead[0] and "记得要处理空文件" in team.lead[0]

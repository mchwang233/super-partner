import asyncio

import pytest
from super_partner.budget import BudgetExceeded
from super_partner.config import settings
from super_partner.diagnosis import interaction, parse, repair_prompt
from super_partner.executor import Executor
from super_partner.models import Diagnosis, DiagnosisMessage, NodeStatus, RunStatus
from super_partner.providers import ProviderResult
from test_diagnosis import MODELS, ORIGIN, Providers, one_node_run

QUESTION = ('需要确认是否必须支持旧版接口。\nQUESTION: 旧版接口是否需要继续兼容？\n'
            'OPTIONS: ["保留兼容", "允许升级接口"]\nSTATUS: NEEDS_INPUT')


def test_question_and_choices_are_structured_and_removed_from_report():
    assert interaction(QUESTION) == ("旧版接口是否需要继续兼容？", ["保留兼容", "允许升级接口"])
    assert parse(QUESTION) == ("需要确认是否必须支持旧版接口。", None, "needs_input")
    assert interaction('QUESTION: 你的要求？\nOPTIONS: [invalid]') == ("你的要求？", [])
    assert interaction('QUESTION: 要哪种？\nOPTIONS: ["甲", "甲", 1, "乙"]')[1] == ["甲", "乙"]


class InteractiveProviders(Providers):
    def __init__(self):
        super().__init__()
        self.repaired = False
        self.work_calls = 0
        self.reply_prompts = []
        self.step_prompts = []

    async def complete(self, model_id, prompt, workspace=None, **kwargs):
        if kwargs.get("repair"):
            if "User's reply:" in prompt:
                self.reply_prompts.append(prompt)
                self.repaired = True
                return ProviderResult("已按你的兼容性要求修复。\nSTATUS: FIXED", 0, 0, model_id)
            return ProviderResult(QUESTION, 0, 0, model_id)
        self.work_calls += 1
        self.step_prompts.append(prompt)
        if not self.repaired:
            raise RuntimeError("legacy interface missing")
        return ProviderResult("checked", 0, 0, model_id)


async def test_user_choice_and_text_continue_the_actual_repair_and_task(task_api, monkeypatch):
    api, client = task_api
    monkeypatch.setattr(settings, "auto_diagnose", True)
    monkeypatch.setattr(settings, "review_nodes", False)
    providers = InteractiveProviders()
    executor = Executor(api.db, providers, MODELS)
    monkeypatch.setattr(api, "executor", executor)
    one_node_run(api.db)
    await executor.execute("r")
    run = api.db.get_run("r")
    diagnosis = run.diagnoses[-1]
    assert run.status == RunStatus.PAUSED
    assert run.waiting_diagnosis_id == diagnosis.id
    assert run.plan.nodes[0].status == NodeStatus.PENDING
    assert providers.work_calls == 1  # No guessing and no automatic retry before an answer.
    assert (await client.post("/api/tasks/r/confirm", json={}, headers=ORIGIN)).status_code == 409

    response = await client.post(f"/api/tasks/r/diagnoses/{diagnosis.id}/reply", headers=ORIGIN,
                                 json={"choice": "保留兼容", "text": "已有三个项目依赖旧接口，请不要改调用方。"})
    assert response.status_code == 200
    await asyncio.gather(*list(executor.diagnosing.values()))
    await asyncio.gather(*list(executor.tasks.values()))
    completed = api.db.get_run("r")
    assert completed.status == RunStatus.SUCCEEDED
    assert completed.waiting_diagnosis_id is None
    assert providers.work_calls == 2
    assert "保留兼容" in providers.reply_prompts[0]
    assert "请不要改调用方" in providers.reply_prompts[0]
    assert "请不要改调用方" in providers.step_prompts[-1]
    assert completed.diagnoses[0].messages[0].choice == "保留兼容"
    assert completed.diagnoses[0].question == diagnosis.question
    assert len(completed.diagnoses) == 2


@pytest.mark.parametrize("out_of_budget", [False, True])
async def test_answer_to_a_general_diagnosis_resumes_or_leaves_budget_controls_available(
        task_api, monkeypatch, out_of_budget):
    api, client = task_api
    monkeypatch.setattr(settings, "review_nodes", False)

    class GeneralDiagnosis(Providers):
        async def complete(self, model_id, prompt, workspace=None, **kwargs):
            if prompt.startswith("You are diagnosing"):
                if "User's reply:" in prompt:
                    if out_of_budget:
                        raise BudgetExceeded("剩余预算不足")
                    return ProviderResult("已明确兼容性要求，可以继续。\nSTATUS: RETRY", 0, 0, model_id)
                return ProviderResult(QUESTION, 0, 0, model_id)
            assert "保留兼容" in prompt
            return ProviderResult("checked", 0, 0, model_id)

    executor = Executor(api.db, GeneralDiagnosis(), MODELS)
    monkeypatch.setattr(api, "executor", executor)
    one_node_run(api.db)
    response = await client.post("/api/tasks/r/diagnose", headers=ORIGIN)
    assert response.status_code == 200
    await asyncio.gather(*list(executor.diagnosing.values()))
    run = api.db.get_run("r")
    assert run.status == RunStatus.PAUSED
    assert run.diagnoses[-1].node_id is None
    identifier = run.waiting_diagnosis_id
    assert identifier

    response = await client.post(f"/api/tasks/r/diagnoses/{identifier}/reply", headers=ORIGIN,
                                 json={"choice": "保留兼容"})
    assert response.status_code == 200
    await asyncio.gather(*list(executor.diagnosing.values()))
    await asyncio.gather(*list(executor.tasks.values()))
    completed = api.db.get_run("r")
    assert completed.waiting_diagnosis_id is None
    assert completed.status == (RunStatus.PAUSED if out_of_budget else RunStatus.SUCCEEDED)
    if out_of_budget:
        assert "预算" in completed.diagnoses[-1].error


@pytest.mark.parametrize("body,status", [
    ({"text": "只修复平台，不改项目配置"}, 200),
    ({"choice": "保留兼容"}, 200),
    ({"text": "  "}, 422),
    ({"choice": "不存在的方案"}, 422),
])
async def test_reply_validation_and_persistence(task_api, monkeypatch, body, status):
    api, client = task_api
    one_node_run(api.db)
    run = api.db.get_run("r")
    run.status = RunStatus.PAUSED
    run.diagnoses = [Diagnosis(id="d", trigger="manual", node_id="n", status="done",
                               question="是否保持兼容？", choices=["保留兼容", "允许升级接口"])]
    api.db.save_run(run)
    monkeypatch.setattr(api.executor, "model_lookup", MODELS)
    monkeypatch.setattr(api.executor.providers, "configured_models", lambda ids: set(ids))
    started = []
    monkeypatch.setattr(api.executor, "start_diagnosis", lambda *args: started.append(args))
    response = await client.post("/api/tasks/r/diagnoses/d/reply", json=body, headers=ORIGIN)
    assert response.status_code == status
    stored = api.db.get_run("r")
    assert len(stored.diagnoses[0].messages) == (1 if status == 200 else 0)
    assert len(started) == (1 if status == 200 else 0)
    assert (await client.post("/api/tasks/r/diagnoses/missing/reply", json={"text": "x"}, headers=ORIGIN)).status_code == 404


async def test_busy_diagnosis_does_not_accept_or_lose_a_reply(task_api, monkeypatch):
    api, client = task_api
    one_node_run(api.db)
    run = api.db.get_run("r")
    run.diagnoses = [Diagnosis(id="d", trigger="manual", status="done")]
    api.db.save_run(run)
    busy = asyncio.get_running_loop().create_future()
    monkeypatch.setitem(api.executor.diagnosing, "r", busy)
    try:
        response = await client.post("/api/tasks/r/diagnoses/d/reply", json={"text": "补充内容"}, headers=ORIGIN)
        assert response.status_code == 409
        assert api.db.get_run("r").diagnoses[0].messages == []
    finally:
        busy.cancel()


def test_reply_to_an_older_report_is_still_in_the_next_prompt(tmp_path):
    from super_partner.db import Database

    db = Database(tmp_path / "test.db")
    one_node_run(db)
    run = db.get_run("r")
    run.diagnoses = [Diagnosis(id=str(i), trigger="manual", status="done") for i in range(8)]
    run.diagnoses[0].messages = [DiagnosisMessage(text="纠正最早的判断：保留旧接口。")]
    prompt = repair_prompt(run, run.plan.nodes[0], "workspace", "tools", [], [])
    assert "纠正最早的判断：保留旧接口。" in prompt

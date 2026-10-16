import asyncio

from super_partner.config import settings
from super_partner.diagnosis import NETWORK_CHOICE, repair_prompt
from super_partner.executor import Executor
from super_partner.models import RunStatus
from super_partner.providers import ProviderResult
from test_diagnosis import MODELS, ORIGIN, Providers, one_node_run
from test_followup import finished_task, planning

ASK = (f"这一步要调用外部天气接口，本任务不允许联网。\nQUESTION: 是否允许本任务联网？\n"
       f'OPTIONS: ["{NETWORK_CHOICE}", "改用本地样例数据"]\nSTATUS: NEEDS_INPUT')


class NetworkProviders(Providers):
    """The step only works with the network; the diagnosis asks for it."""

    def __init__(self):
        super().__init__()
        self.work = []
        self.repair_prompts = []

    async def complete(self, model_id, prompt, workspace=None, **kwargs):
        if kwargs.get("repair"):
            assert "network" not in kwargs  # Repairs have the host's network anyway.
            self.repair_prompts.append(prompt)
            return ProviderResult("已允许联网。\nSTATUS: FIXED" if "User's reply:" in prompt else ASK, 0, 0, model_id)
        self.work.append(kwargs.get("network", False))
        if not kwargs.get("network"):
            raise RuntimeError("curl: (6) Could not resolve host: api.weather.example")
        return ProviderResult("fetched", 0, 0, model_id)


async def test_a_diagnosis_can_ask_to_allow_the_network_and_the_answer_turns_it_on(task_api, monkeypatch):
    api, client = task_api
    monkeypatch.setattr(settings, "auto_diagnose", True)
    monkeypatch.setattr(settings, "review_nodes", False)
    providers = NetworkProviders()
    executor = Executor(api.db, providers, MODELS)
    monkeypatch.setattr(api, "executor", executor)
    one_node_run(api.db)
    await executor.execute("r")
    run = api.db.get_run("r")
    assert run.status == RunStatus.PAUSED and not run.allow_network
    assert NETWORK_CHOICE in run.diagnoses[-1].choices and NETWORK_CHOICE in providers.repair_prompts[0]

    response = await client.post(f"/api/tasks/r/diagnoses/{run.diagnoses[-1].id}/reply", headers=ORIGIN,
                                 json={"choice": NETWORK_CHOICE})
    assert response.status_code == 200 and response.json()["allow_network"] is True
    await asyncio.gather(*list(executor.diagnosing.values()))
    await asyncio.gather(*list(executor.tasks.values()))
    done = api.db.get_run("r")
    assert done.status == RunStatus.SUCCEEDED and done.allow_network
    assert providers.work == [False, True]


async def test_network_access_is_chosen_per_task_and_carried_into_a_change(task_api, monkeypatch):
    api, client = task_api
    parent = finished_task(api)
    planning(api, monkeypatch, [])
    assert not parent.allow_network
    response = await client.put(f"/api/tasks/{parent.id}/network", json={"allow": True}, headers=ORIGIN)
    assert response.status_code == 200 and api.db.get_run(parent.id).allow_network
    assert "allows the internet" in repair_prompt(api.db.get_run(parent.id), parent.plan.nodes[0], "w", "t", [], [])
    assert "does not allow the internet" in repair_prompt(parent, parent.plan.nodes[0], "w", "t", [], [])

    child = (await client.post(f"/api/tasks/{parent.id}/followup", json={"prompt": "抓取天气"}, headers=ORIGIN)).json()
    assert child["allow_network"] is True
    child = (await client.post(f"/api/tasks/{parent.id}/followup", json={"prompt": "离线版本", "allow_network": False},
                               headers=ORIGIN)).json()
    assert child["allow_network"] is False
    assert child["use_cli"] is False  # The earlier task's setting.
    child = (await client.post(f"/api/tasks/{parent.id}/followup", json={"prompt": "可以用 CLI", "use_cli": True},
                               headers=ORIGIN)).json()
    assert child["use_cli"] is True and child["allow_network"] is True
    created = (await client.post("/api/tasks", json={"prompt": "写一个网页", "allow_network": True}, headers=ORIGIN)).json()
    assert created["allow_network"] is True
    assert (await client.put("/api/tasks/missing/network", json={"allow": True}, headers=ORIGIN)).status_code == 404

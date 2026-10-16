from super_partner import quality
from super_partner.config import settings
from super_partner.models import (
    CapabilityScore,
    ExecutionRun,
    ModelProfile,
    RunStatus,
    TaskNode,
    TaskPlan,
)

ORIGIN = {"origin": "http://localhost:5173"}
AGENT = ModelProfile(id="x:agent", provider="x", display_name="Agent", channel="api", context_window=1_000_000,
                     input_per_million=1, output_per_million=1, execution_priority=100,
                     scores=CapabilityScore(coding=90, planning=90, reliability=90))


def finished_task(api, status=RunStatus.SUCCEEDED) -> ExecutionRun:
    run = ExecutionRun(id="parent", prompt="写一个待办事项网页", status=status, budget_usd=3, use_cli=False,
                       final_result="完成了 index.html 和 app.js，支持增删待办。",
                       plan=TaskPlan(summary="待办网页", nodes=[TaskNode(id="a", title="页面", instructions="i", capability="coding")]))
    folder = settings.workspace_root / run.id
    (folder / "static").mkdir(parents=True)
    (folder / "static" / "app.js").write_text("// todo app\n")
    (folder / "index.html").write_text("<!doctype html>\n")
    api.db.save_run(run)
    return run


def planning(api, monkeypatch, prompts):
    async def plan(prompt, *_args, **_kwargs):
        prompts.append(prompt)
        return TaskPlan(summary="加提醒", nodes=[TaskNode(id="b", title="提醒", instructions="add reminders", capability="coding")])

    monkeypatch.setattr(api, "autonomous_plan", plan)
    monkeypatch.setattr(api, "models", {AGENT.id: AGENT})
    monkeypatch.setattr(api.providers, "configured_models", lambda _ids: {AGENT.id})


async def test_a_requirement_change_builds_on_the_finished_task_in_its_folder(task_api, monkeypatch):
    api, client = task_api
    parent = finished_task(api)
    prompts = []
    planning(api, monkeypatch, prompts)

    response = await client.post(f"/api/tasks/{parent.id}/followup", json={"prompt": "增加到期提醒"}, headers=ORIGIN)
    assert response.status_code == 201, response.text
    child = api.db.get_run(response.json()["id"])
    assert (child.prompt, child.parent_run_id, child.workspace_id, child.budget_usd) == ("增加到期提醒", "parent", "parent", 3)
    assert child.status == RunStatus.AWAITING_CONFIRMATION
    # The planner knows what exists and what is new.
    [prompt] = prompts
    assert "写一个待办事项网页" in prompt and "index.html" in prompt and "static/app.js" in prompt
    assert "完成了 index.html 和 app.js" in prompt and prompt.rstrip().endswith("新需求：\n增加到期提醒")
    # Its steps work in the same folder, can change files, and are told so.
    assert settings.workspace_root / child.folder_id == settings.workspace_root / parent.id
    assert all(node.needs_workspace and node.model_id == "x:agent" for node in child.plan.nodes)
    step_prompt, _ = quality.node_prompt(child.plan.nodes[0], child, 4000)
    assert "this task changes an earlier task" in step_prompt and "写一个待办事项网页" in step_prompt

    # A change of the change stays in the first folder and keeps the earlier background.
    child.status = RunStatus.SUCCEEDED
    api.db.save_run(child)
    response = await client.post(f"/api/tasks/{child.id}/followup", json={"prompt": "提醒改成邮件"}, headers=ORIGIN)
    grandchild = api.db.get_run(response.json()["id"])
    assert grandchild.workspace_id == "parent" and "更早的背景" in grandchild.background
    assert "增加到期提醒" in grandchild.background


async def test_a_task_still_running_cannot_be_changed(task_api, monkeypatch):
    api, client = task_api
    parent = finished_task(api, RunStatus.RUNNING)
    planning(api, monkeypatch, [])
    response = await client.post(f"/api/tasks/{parent.id}/followup", json={"prompt": "增加到期提醒"}, headers=ORIGIN)
    assert response.status_code == 409

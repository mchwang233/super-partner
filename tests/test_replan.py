"""The lead changes the plan of a running task, and checks the whole result before it ends."""
import json

import pytest
from super_partner.config import settings
from super_partner.models import ExecutionRun, NodeStatus, RunStatus, TaskNode, TaskPlan
from super_partner.replan import PlanError, change_plan
from test_leading import MODELS, Team, decision, run

CONFIGURED = set(MODELS)


def plan(*nodes):
    return ExecutionRun(id="r", prompt="p", status=RunStatus.RUNNING, budget_usd=1, plan=TaskPlan(summary="s", nodes=list(nodes)))


def step(node_id, status=NodeStatus.PENDING, deps=()):
    return TaskNode(id=node_id, title=node_id.upper(), instructions="do", capability="coding",
                    model_id="x:api", status=status, dependencies=list(deps))


def test_a_missing_prerequisite_is_added_before_a_waiting_step():
    current = plan(step("a", NodeStatus.SUCCEEDED), step("b", deps=["a"]))
    changes = change_plan(current, {
        "add": [{"key": "db", "title": "建表", "instructions": "建好表", "dependencies": ["a"], "difficulty": "easy"}],
        "update": [{"id": "b", "dependencies": ["a", "db"], "instructions": "用新表"}],
    }, list(MODELS.values()), CONFIGURED)
    added = current.plan.nodes[-1]
    assert changes == {"added": ["建表"], "removed": [], "updated": ["B"]}
    assert added.dependencies == ["a"] and added.model_id and added.difficulty == "easy"
    assert current.plan.nodes[1].dependencies == ["a", added.id] and current.plan.nodes[1].instructions == "用新表"


def test_finished_work_stays_and_the_plan_stays_acyclic():
    current = plan(step("a", NodeStatus.SUCCEEDED), step("b", deps=["a"]), step("c", deps=["b"]))
    with pytest.raises(PlanError, match="finished or running"):
        change_plan(current, {"remove": ["a"]}, list(MODELS.values()), CONFIGURED)
    with pytest.raises(PlanError, match="cycle"):
        change_plan(current, {"update": [{"id": "b", "dependencies": ["c"]}]}, list(MODELS.values()), CONFIGURED)
    with pytest.raises(PlanError, match="unknown or removed"):
        change_plan(current, {"add": [{"key": "x", "instructions": "i", "dependencies": ["zzz"]}]},
                    list(MODELS.values()), CONFIGURED)
    # Removing a step that has not run drops it from what depended on it.
    change_plan(current, {"remove": ["b"]}, list(MODELS.values()), CONFIGURED)
    assert [node.id for node in current.plan.nodes] == ["a", "c"] and current.plan.nodes[1].dependencies == []


SPLIT = {"remove": ["a"], "add": [
    {"key": "one", "title": "上半部分", "instructions": "先做解析"},
    {"key": "two", "title": "下半部分", "instructions": "再做输出", "dependencies": ["one"]},
]}


async def test_the_lead_splits_a_step_that_is_too_big(database):
    team = Team([decision("replan", plan=SPLIT)])
    _, result = await run(database, team)
    assert result.status == RunStatus.SUCCEEDED
    assert [node.title for node in result.plan.nodes] == ["上半部分", "下半部分"]
    assert [work[1].split("\n", 1)[0] for work in team.work[1:]] == ["Task: 先做解析", "Task: 再做输出"]
    assert any(event.kind == "run.plan_changed" for event in database.events("r"))


async def test_a_plan_change_that_cannot_be_made_is_sent_back_with_why(database):
    team = Team([decision("replan", plan={"add": [{"key": "x", "instructions": "i", "dependencies": ["nope"]}]}),
                 decision("retry", instructions="再试一次")])
    _, result = await run(database, team)
    assert "cannot be made: step x dependencies: unknown or removed step nope" in team.lead[1]
    assert result.decisions[0].action == "retry" and result.status == RunStatus.SUCCEEDED


async def test_the_lead_confirms_the_whole_result_before_the_task_ends(database):
    team = Team([json.dumps({"action": "done", "reason": "需求都做到了"})], fails=0)
    _, result = await run(database, team)
    assert result.status == RunStatus.SUCCEEDED
    assert "EVENT: every step has finished" in team.lead[0] and "写一个工具" in team.lead[0]
    assert [(item.event, item.action) for item in result.decisions] == [("all_done", "done")]


async def test_the_lead_sends_finished_work_back_when_the_whole_falls_short(database):
    team = Team([decision("rework", targets=["a"], instructions="README 里补上安装步骤"), decision("done")], fails=0)
    _, result = await run(database, team)
    assert result.status == RunStatus.SUCCEEDED and len(team.work) == 2
    assert "负责人检查整体结果后要求返工：README 里补上安装步骤" in team.work[1][1]


async def test_the_lead_adds_what_is_missing_at_the_end(database):
    added = {"add": [{"key": "docs", "title": "写说明", "instructions": "写使用说明", "dependencies": ["a"]}]}
    team = Team([decision("replan", plan=added), decision("done")], fails=0)
    _, result = await run(database, team)
    assert result.status == RunStatus.SUCCEEDED and [node.title for node in result.plan.nodes] == ["实现", "写说明"]
    assert [work[1].split("\n", 1)[0] for work in team.work] == ["Task: 写代码", "Task: 写使用说明"]


async def test_without_a_lead_a_finished_task_ends_as_before(database, monkeypatch):
    monkeypatch.setattr(settings, "lead_decisions", False)
    team = Team([], fails=0)
    _, result = await run(database, team)
    assert result.status == RunStatus.SUCCEEDED and team.lead == []

"""Finished tasks are looked back on, so a problem that keeps coming back is seen as one
(retrospect.py)."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from super_partner import retrospect
from super_partner.catalog import builtin_models
from super_partner.config import settings
from super_partner.db import Database
from super_partner.diagnosis import repair_prompt
from super_partner.executor import Executor
from super_partner.models import (
    Diagnosis,
    Event,
    ExecutionRun,
    NodeAttempt,
    NodeStatus,
    RunStatus,
    TaskNode,
    TaskPlan,
    ToolCall,
)
from super_partner.providers import ProviderResult


def finished(run_id: str, nodes, days_ago: int = 0, **fields) -> ExecutionRun:
    created = datetime.now(timezone.utc) - timedelta(days=days_ago)
    fields = {"status": RunStatus.SUCCEEDED, **fields}
    return ExecutionRun(id=run_id, prompt="写一个 FIFO", budget_usd=2,
                        plan=TaskPlan(summary="s", nodes=nodes), created_at=created, updated_at=created, **fields)


def vcs_step(node_id="n", turns=40, cost=0.3) -> TaskNode:
    return TaskNode(
        id=node_id, title="冒烟测试", instructions="i", capability="coding", model_id="deepseek:flash",
        status=NodeStatus.SUCCEEDED, estimated_cost_usd=0.01, difficulty="normal",
        attempts=[NodeAttempt(model_id="glm:glm", outcome="unavailable", reason="glm request failed: 5 分钟没有回复"),
                  NodeAttempt(model_id="deepseek:flash", outcome="passed", cost_usd=cost, turns=turns, tool_calls=[
                      ToolCall(turn=1, tool="Bash", detail="source /eda/.edarc && which vcs", ok=False),
                      ToolCall(turn=2, tool="Bash", detail="vcs -sverilog tb.sv", ok=False)])],
    )


def test_a_finished_task_is_summed_up_with_its_problems():
    run = finished("r", [vcs_step()], diagnoses=[
        Diagnosis(id="d", trigger="auto", node_id="n", status="done", verdict="fixed", report="## 根因\n沙箱里找不到 vcs 命令"),
    ])
    events = [Event(run_id="r", kind="approval.required", data={"reason": "diagnosis_unresolved", "node_id": "n"}),
              Event(run_id="r", kind="approval.required", data={"reason": "user_paused"}),
              Event(run_id="r", kind="node.progress_check", data={"model_id": "deepseek:flash", "stop": True, "node_id": "n",
                                                                  "reason": "反复运行同一条命令"})]
    body = retrospect.review(run, events)
    [step] = body["steps"]
    assert (step["estimated_usd"], step["actual_usd"], step["turns"], step["expected_turns"]) == (0.01, 0.3, 40, 15)
    names = {(problem["kind"], problem["key"]) for problem in body["problems"]}
    assert names == {("estimate_low", "deepseek:flash"), ("turns_over", "deepseek:flash"),
                     ("model_unavailable", "glm:glm"), ("environment", "vcs"),
                     ("paused", "diagnosis_unresolved"), ("stuck", "deepseek:flash")}
    environment = next(problem for problem in body["problems"] if problem["kind"] == "environment")
    assert environment["repaired"] and environment["label"] == "沙箱里找不到 vcs 命令"


def test_the_program_a_failing_command_was_about():
    assert retrospect.program("source /eda/.edarc && which vcs && vcs -ID") == "vcs"
    assert retrospect.program("cd /work; PATH=/x:$PATH /opt/bin/iverilog -g2012 a.v") == "iverilog"
    assert retrospect.program("echo hi") is None


def test_a_repair_names_the_area_it_touched():
    assert retrospect.repaired_areas(["backend/super_partner/agent.py", "tests/test_agent.py", "docs/ARCHITECTURE.md",
                                      "bin/ssh", "sandbox.json"]) == [
        ("platform", "agent"), ("environment", "ssh"), ("environment", "sandbox")]


def test_recurring_problems_does_not_generate_retrospectives_for_cancelled_tasks(tmp_path):
    db = Database(tmp_path / "t.db")
    db.save_run(finished("cancelled", [vcs_step()], status=RunStatus.CANCELLED))
    db.save_run(finished("natural", [vcs_step()]))
    retrospect.recurring(db)
    assert db.retrospective("cancelled") is None
    assert db.retrospective("natural") is not None


def test_a_problem_that_comes_back_after_a_repair_is_marked(tmp_path):
    db = Database(tmp_path / "t.db")
    for run_id, days in (("first", 3), ("second", 1), ("third", 0)):
        db.save_run(finished(run_id, [vcs_step()], days_ago=days))
    # The sandbox was repaired in the first task and again in the second: it did not hold.
    for run_id in ("first", "second"):
        db.save_shared_repair({"id": f"rep-{run_id}", "run_id": run_id, "scope": "super_partner", "status": "activated",
                               "files": ["backend/super_partner/agent.py"], "report": "根本原因：沙箱没有加载 EDA 环境",
                               "created_at": datetime.now(timezone.utc).isoformat()})
    groups = {(group["kind"], group["key"]): group for group in retrospect.recurring(db)}
    sandbox = groups["platform", "agent"]
    assert sandbox["runs"] == ["first", "second"] and sandbox["repaired_in"] == ["first", "second"]
    assert sandbox["recurred_after_repair"] is True
    assert len(groups["estimate_low", "deepseek:flash"]["runs"]) == 3
    assert groups["estimate_low", "deepseek:flash"]["recurred_after_repair"] is False
    # Kept, even when its task is deleted: later tasks learn from it.
    assert db.retrospective("first")["run_id"] == "first"
    db.delete_run("first")
    assert db.retrospective("first")["run_id"] == "first"


def test_a_diagnosis_is_told_about_the_same_problem_in_earlier_tasks(tmp_path):
    db = Database(tmp_path / "t.db")
    earlier = finished("earlier", [vcs_step()], days_ago=2, diagnoses=[
        Diagnosis(id="d1", trigger="auto", node_id="n", status="done", verdict="fixed",
                  report="根本原因：沙箱里没有加载 .edarc，所以找不到 vcs")])
    db.save_run(earlier)
    now = finished("now", [vcs_step()], status=RunStatus.RUNNING)
    lines = retrospect.similar(db, now, now.plan.nodes[0])
    [line] = [line for line in lines if line.startswith("- environment:vcs")]
    assert "1 earlier task" in line and "沙箱里没有加载 .edarc" in line
    prompt = repair_prompt(now, now.plan.nodes[0], "/w", "/tools", [], [], history=lines)
    assert "The same kind of problem in earlier tasks" in prompt and "沙箱里没有加载 .edarc" in prompt
    assert "The same kind of problem" not in repair_prompt(now, now.plan.nodes[0], "/w", "/tools", [], [])


@pytest.mark.asyncio
async def test_a_task_is_looked_back_on_when_it_ends(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "review_nodes", False)  # only the end matters here
    db = Database(tmp_path / "t.db")
    db.save_run(ExecutionRun(id="r", prompt="p", status=RunStatus.QUEUED, budget_usd=1, plan=TaskPlan(summary="s", nodes=[
        TaskNode(id="n", title="n", instructions="i", capability="planning", model_id="mock:local")])))

    class Done:
        async def complete(self, model_id, prompt, workspace=None, **kwargs):
            return ProviderResult("ok", 1, 1, model_id)

    await Executor(db, Done(), {model.id: model for model in builtin_models()}).execute("r")
    assert db.get_run("r").status == RunStatus.SUCCEEDED
    assert db.retrospective("r")["steps"][0]["id"] == "n"


async def test_the_api_lists_recurring_problems_and_a_tasks_look_back(task_api):
    api, client = task_api
    for run_id, days in (("a", 2), ("b", 1)):
        api.db.save_run(finished(run_id, [vcs_step()], days_ago=days))
    recurring = (await client.get("/api/retrospectives/recurring")).json()
    assert ("estimate_low", "deepseek:flash") in {(group["kind"], group["key"]) for group in recurring}
    assert all(len(group["runs"]) >= 2 for group in recurring)
    body = (await client.get("/api/tasks/a/retrospective")).json()
    assert body["run_id"] == "a" and body["steps"][0]["turns"] == 40
    assert (await client.get("/api/tasks/missing/retrospective")).status_code == 404


def test_a_program_is_found_inside_bash_minus_c_and_after_cd():
    assert retrospect.program("bash -lc 'set -euo pipefail; source /eda/.edarc >/dev/null 2>&1; vcs -sverilog -full64 a.sv'") == "vcs"
    assert retrospect.program("cd /work && ./run.sh") == "run.sh"


def platform_step(node_id="p") -> TaskNode:
    return TaskNode(id=node_id, title="仿真", instructions="i", capability="coding", model_id="deepseek:flash",
                    status=NodeStatus.SUCCEEDED, attempts=[NodeAttempt(model_id="deepseek:flash", outcome="passed")])


class Namer:
    """A cheap model that names problems: the EDA ones alike, whatever the records call them."""

    def __init__(self, reply=None):
        self.prompts, self.reply = [], reply

    async def __call__(self, model_id, prompt):
        self.prompts.append((model_id, prompt))
        if self.reply is not None:
            return ProviderResult(self.reply, 10, 5, model_id)
        lines = [line for line in prompt.split("Problems:\n", 1)[1].split("\n\n", 1)[0].splitlines()]
        names = ["新思 EDA 环境未加载" if "vcs" in line or "platform:agent" in line or ".edarc" in line else "其他"
                 for line in lines]
        return ProviderResult(json.dumps({"names": names}, ensure_ascii=False), 10, 5, model_id)


def cheap_models():
    from conftest import scored_models
    models = scored_models()
    return models, {m.id for m in models}


async def test_one_cause_under_two_names_is_counted_as_one(tmp_path):
    db = Database(tmp_path / "t.db")
    models, configured = cheap_models()
    namer = Namer()
    # The first task: VCS not found (environment:vcs). The second: the sandbox repaired (platform:agent).
    db.save_run(finished("first", [vcs_step()], days_ago=2, diagnoses=[
        Diagnosis(id="d1", trigger="auto", node_id="n", status="done", verdict="fixed", report="沙箱里没有加载 .edarc")]))
    db.save_run(finished("second", [platform_step()], days_ago=1))
    retrospect.record(db, "first")
    db.save_retrospective("second", retrospect.review(db.get_run("second"), [], [
        {"id": "j", "files": ["backend/super_partner/agent.py"], "report": "步骤沙箱加载 .edarc", "node_id": "p"}]))
    for run_id in ("first", "second"):
        await retrospect.name_problems(db, namer, models, configured, run_id)
    assert "新思 EDA 环境未加载" in namer.prompts[1][1]  # the names given so far are offered again
    [eda] = [group for group in retrospect.topics(db) if group["topic"] == "新思 EDA 环境未加载"]
    assert eda["runs"] == ["first", "second"] and {"environment:vcs", "platform:agent"} <= set(eda["members"])
    assert eda["recurred_after_repair"]
    # A third task's diagnosis of a failing VCS step hears of the sandbox repair too.
    now = finished("now", [vcs_step()], status=RunStatus.RUNNING)
    [line] = [line for line in retrospect.similar(db, now, now.plan.nodes[0]) if line.startswith("- 「新思 EDA")]
    assert "platform:agent" in line and "2 earlier task" in line
    # Recorded again, the problems keep their names.
    retrospect.record(db, "first")
    assert all(p.get("topic") for p in db.retrospective("first")["problems"])
    assert db.retrospective("first")["topics_cost_usd"] >= 0


@pytest.mark.parametrize("reply", ["not json", '{"names": ["只有一个"]}'])
async def test_a_bad_naming_reply_leaves_the_retrospective_as_it_was(tmp_path, reply):
    db = Database(tmp_path / "t.db")
    models, configured = cheap_models()
    db.save_run(finished("r", [vcs_step()]))
    before = retrospect.record(db, "r")
    assert len(before["problems"]) > 1
    await retrospect.name_problems(db, Namer(reply), models, configured, "r")
    assert db.retrospective("r") == before and retrospect.topics(db) == []


async def test_a_tasks_problems_are_named_when_it_ends(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setattr(settings, "problem_naming", True)
    db = Database(tmp_path / "t.db")
    models, configured = cheap_models()
    db.save_run(finished("r", [vcs_step()], status=RunStatus.RUNNING))

    class Cheap:
        def __init__(self):
            self.calls = []

        def configured_models(self, ids):
            return configured

        async def complete(self, model_id, prompt, *args, **kwargs):
            self.calls.append((model_id, kwargs))
            count = len(prompt.split("Problems:\n", 1)[1].split("\n\n", 1)[0].splitlines())
            return ProviderResult(json.dumps({"names": ["步骤费用估算偏低"] * count}), 10, 5, model_id)

    providers = Cheap()
    executor = Executor(db, providers, {m.id: m for m in models})
    run = db.get_run("r")
    run.status = RunStatus.SUCCEEDED
    db.save_run(run)
    executor.emit("r", "run.succeeded")
    await asyncio.gather(*executor.naming_tasks.values())
    [(model_id, kwargs)] = providers.calls
    assert kwargs["effort"] == "low" and not model_id.startswith(("cli:", "mock:"))
    assert {p["topic"] for p in db.retrospective("r")["problems"]} == {"步骤费用估算偏低"}


async def test_the_api_lists_problems_by_what_they_are_about(task_api):
    api, client = task_api
    api.db.save_run(finished("a", [vcs_step()]))
    body = retrospect.record(api.db, "a")
    for problem in body["problems"]:
        problem["topic"] = "新思 EDA 环境未加载"
    api.db.save_retrospective("a", body)
    [group] = (await client.get("/api/retrospectives/topics")).json()
    assert group["topic"] == "新思 EDA 环境未加载" and group["runs"] == ["a"] and "estimate_low:deepseek:flash" in group["members"]

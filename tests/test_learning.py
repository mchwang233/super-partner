"""Evidence -> validated lesson -> later application -> comparable observations."""
import asyncio
import json
from pathlib import Path

import pytest
from super_partner import learning, retrospect
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import (
    CapabilityScore,
    EngineerJob,
    ExecutionRun,
    Improvement,
    LearningReview,
    NodeAttempt,
    NodeStatus,
    RunStatus,
    TaskNode,
    TaskPlan,
)
from super_partner.providers import ProviderResult
from test_turns import model


def finished(db, run_id="first", cost=0.2, turns=40, model_id="x:writer"):
    node = TaskNode(id="n", title="compile", instructions="Use fakecompiler to compile a fixture", capability="coding",
                    model_id=model_id, status=NodeStatus.SUCCEEDED, estimated_cost_usd=0.01,
                    attempts=[NodeAttempt(model_id=model_id, outcome="passed", turns=turns, cost_usd=cost)])
    run = ExecutionRun(id=run_id, prompt="Use fakecompiler", plan=TaskPlan(summary="s", nodes=[node]),
                       status=RunStatus.SUCCEEDED, budget_usd=1, actual_cost_usd=cost)
    db.save_run(run)
    retrospect.record(db, run.id)
    return run


def proposal(kind="rule", **extra):
    return {"kind": kind, "title": "Keep the compile check small", "rationale": "Step n spent 40 turns on setup",
            "content": "Run a small compile check before the full check; keep both acceptance checks.",
            "tools": ["fakecompiler"], "capability": "coding", "source_nodes": ["n"],
            "problem_kind": "turns_over", "problem_key": "x:writer", **extra}


class Models:
    def __init__(self, items=None, approved=True):
        self.items = items if items is not None else [proposal()]
        self.approved, self.prompts = approved, []

    def configured_models(self, ids):
        return set(ids)

    async def complete(self, model_id, prompt, workspace=None, **kwargs):
        self.prompts.append((model_id, prompt, workspace, kwargs))
        if "learning:analyze" in prompt:
            text = json.dumps({"conclusion": "Repeated setup cost too many turns; reuse a checked command.",
                               "improvements": self.items})
        elif "learning:validate" in prompt:
            text = json.dumps({"passed": self.approved, "reason": "The evidence supports the bounded advice."})
        else:
            text = "completed"
        return ProviderResult(text, 10, 10, model_id)


@pytest.fixture
def setup_learning(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "review_nodes", False)
    db = Database(tmp_path / "test.db")
    db.save_learning_settings({"enabled": True, "budget_usd": 0.1, "auto_platform": False})
    models = Models()
    executor = Executor(db, models, {m.id: m for m in [model("x:writer", 90), model("y:reviewer", 92)]})
    yield db, models, executor
    db.connection.close()


async def learn(db, executor):
    executor.start_learning("first")
    await executor.learning_tasks["first"]
    return db.learning_review("first"), db.improvements("first")


@pytest.mark.parametrize("channel", ["api", "cli"])
async def test_learning_uses_the_strongest_model_for_analysis_and_fresh_review(setup_learning, monkeypatch, channel):
    db, models, executor = setup_learning
    finished(db)
    monkeypatch.setattr(settings, "subtasks_prefer_api", True)
    strongest = model("best:review", 99, channel).model_copy(update={
        "scores": CapabilityScore(**{key: 99 for key in CapabilityScore.model_fields}),
        "input_per_million": 100, "output_per_million": 100,
    })
    cheap = model("cheap:summary", 80).model_copy(update={"input_per_million": 0, "output_per_million": 0})
    weak_cli = model("cli:weak", 70, "cli")
    executor.model_lookup = {m.id: m for m in [cheap, weak_cli, strongest]}
    review, [item] = await learn(db, executor)
    assert review.status == "done" and review.model_id == strongest.id
    assert item.validation["model_id"] == strongest.id and item.status == "active"
    assert [call[0] for call in models.prompts] == [strongest.id, strongest.id]
    assert models.prompts[0][2] != models.prompts[1][2]  # Independent, empty conversations.
    assert all(call[3]["effort"] == settings.diagnosis_reasoning_effort for call in models.prompts)


async def test_learning_prefers_a_different_equally_strong_validator(setup_learning):
    db, _, executor = setup_learning
    finished(db)
    first = model("a:best", 99)
    peer = first.model_copy(update={"id": "b:peer"})
    weak = model("cheap:summary", 70).model_copy(update={"input_per_million": 0, "output_per_million": 0})
    executor.model_lookup = {m.id: m for m in [weak, peer, first]}
    review, [item] = await learn(db, executor)
    assert review.model_id == first.id
    assert item.validation["model_id"] == peer.id


@pytest.mark.parametrize("unavailable", ["disabled", "unconfigured", "context", "cli_disallowed"])
async def test_learning_only_uses_eligible_models(setup_learning, monkeypatch, unavailable):
    db, models, executor = setup_learning
    run = finished(db)
    best = model("cli:best", 99, "cli")
    allowed = model("allowed:review", 80)
    if unavailable == "disabled":
        best.enabled = False
    elif unavailable == "unconfigured":
        monkeypatch.setattr(models, "configured_models", lambda ids: {allowed.id})
    elif unavailable == "context":
        best.context_window = 100
    else:
        run.use_cli = False
        db.save_run(run)
    executor.model_lookup = {m.id: m for m in [best, allowed]}
    review, _ = await learn(db, executor)
    assert review.status == "done" and review.model_id == allowed.id
    assert {call[0] for call in models.prompts} == {allowed.id}


async def test_finished_event_creates_a_review_and_a_verified_rule(setup_learning):
    db, models, executor = setup_learning
    original = finished(db)
    executor.emit("first", "run.succeeded")
    await executor.learning_tasks["first"]
    review = db.learning_review("first")
    [item] = db.improvements("first")
    assert review.status == "done" and review.conclusion
    assert item.status == "active" and item.validation["model_id"] == "y:reviewer"
    assert review.cost_usd > 0 and db.get_run("first").actual_cost_usd == original.actual_cost_usd
    assert db.get_run("first").status == RunStatus.SUCCEEDED
    assert all(not kwargs["act"] and not kwargs["network"] for _, _, _, kwargs in models.prompts)
    executor.emit("first", "run.succeeded")
    await executor.learning_tasks["first"]
    assert len(models.prompts) == 2 and len(db.improvements()) == 1


async def test_only_matching_work_receives_a_snapshot_and_is_measured(setup_learning):
    db, models, executor = setup_learning
    finished(db)
    _, [item] = await learn(db, executor)
    run = finished(db, "second", cost=0.05, turns=10)
    run.improvement_uses = learning.selected(db, run.prompt)
    assert item.title in learning.instruction(run.improvement_uses)
    run.status = RunStatus.QUEUED
    run.plan.nodes[0].status, run.plan.nodes[0].attempts = NodeStatus.PENDING, []
    db.save_run(run)
    await executor.execute(run.id)
    await executor.learning_tasks[run.id]
    result = db.get_run(run.id)
    assert result.improvement_uses[0]["node_ids"] == ["n"]
    assert any(item.content in prompt and "Task:" in prompt for _, prompt, _, _ in models.prompts)
    [effect] = db.improvement_effects(item.id)
    assert effect["comparable"] and effect["delta"]["cost_per_step"] < 0
    learning.observe(db, result)
    assert len(db.improvement_effects(item.id)) == 1
    item.content, item.status = "changed", "disabled"
    db.save_improvement(item)
    assert "changed" not in learning.instruction(result.improvement_uses)
    assert learning.selected(db, "fakecompiler") == []
    unrelated = result.plan.nodes[0].model_copy(update={"instructions": "write a poem", "title": "poem", "capability": "writing"})
    assert learning.instruction(result.improvement_uses, unrelated) == ""


async def test_a_different_model_does_not_produce_a_false_effect_claim(setup_learning):
    db, _, executor = setup_learning
    finished(db)
    _, [item] = await learn(db, executor)
    run = finished(db, "second", model_id="y:reviewer")
    run.improvement_uses = learning.selected(db, run.prompt)
    run.improvement_uses[0]["node_ids"] = ["n"]
    learning.observe(db, run)
    [effect] = db.improvement_effects(item.id)
    assert not effect["comparable"] and effect["delta"] == {}


@pytest.mark.parametrize("approved", [False, True])
async def test_skill_requires_a_real_check_and_independent_review(setup_learning, monkeypatch, approved):
    db, models, executor = setup_learning
    finished(db)
    content = "---\nname: compile-small\ndescription: compile check\ntools: [fakecompiler]\n---\nCompile a small fixture first."
    models.items = [proposal("skill", content=content, check="fakecompiler fixture")]
    models.approved = approved
    checked = []

    async def check(item):
        checked.append(item.check)
        return {"passed": True, "output": "fixture passed", "command": item.check}

    monkeypatch.setattr(learning, "check_skill", check)
    _, [item] = await learn(db, executor)
    assert checked == ["fakecompiler fixture"]
    assert item.status == ("active" if approved else "failed")
    artifact = Path(settings.data_dir) / "learned-skills" / item.id / "SKILL.md"
    assert artifact.exists() == approved
    if approved:
        assert artifact.read_text() == content


async def test_failing_skill_check_is_never_published(setup_learning, monkeypatch):
    db, models, executor = setup_learning
    finished(db)
    models.items = [proposal("skill", content="---\nname: c\ntools: [fakecompiler]\n---\nCompile a fixture.", check="false")]

    async def fail(item):
        return {"passed": False, "output": "bad command"}

    monkeypatch.setattr(learning, "check_skill", fail)
    _, [item] = await learn(db, executor)
    assert item.status == "failed" and learning.selected(db, "fakecompiler") == []
    assert len(models.prompts) == 1


@pytest.mark.parametrize("change", [{"source_nodes": ["invented"]}, {"tools": [], "capability": None},
                                    {"problem_key": "invented"}, {"tools": ["../unsafe"]}])
def test_invented_evidence_and_unbounded_scope_are_rejected(setup_learning, change):
    db, _, _ = setup_learning
    run = finished(db)
    with pytest.raises(ValueError):
        learning.parse_analysis(json.dumps({"conclusion": "c", "improvements": [proposal(**change)]}), run)


async def test_invalid_model_output_does_not_change_the_task_or_publish(setup_learning):
    db, models, executor = setup_learning
    finished(db)
    models.items = [proposal(source_nodes=["invented"])]
    review, items = await learn(db, executor)
    assert review.status == "blocked" and items == []
    assert db.get_run("first").status == RunStatus.SUCCEEDED


async def test_budget_exhaustion_is_recorded_without_reopening_finished_work(setup_learning, monkeypatch):
    db, models, executor = setup_learning
    finished(db)

    async def exceeds(model_id, prompt, workspace=None, **kwargs):
        await kwargs["spend_ok"].reserve(model_id, {"text": "large" * 1000000})
        raise AssertionError("the reservation must reject the call")

    monkeypatch.setattr(models, "complete", exceeds)
    review, items = await learn(db, executor)
    assert review.status == "blocked" and not items
    assert db.get_run("first").status == RunStatus.SUCCEEDED


async def test_restart_resumes_validation_without_regenerating_items(setup_learning):
    db, models, executor = setup_learning
    run = finished(db)
    conclusion, [item] = learning.parse_analysis(json.dumps({"conclusion": "c", "improvements": [proposal()]}), run)
    item.status = "validating"
    db.save_improvement(item)
    db.save_learning_review(LearningReview(run_id=run.id, revision=learning.revision(run), status="validating",
                                           conclusion=conclusion, model_id="x:writer"))
    executor.restore_learning()
    await executor.learning_tasks[run.id]
    assert len(db.improvements()) == 1 and db.improvement(item.id).status == "active"
    assert len(models.prompts) == 1 and "learning:validate" in models.prompts[0][1]


async def test_platform_changes_use_the_existing_engineer_and_wait_for_actual_activation(setup_learning, monkeypatch):
    db, models, executor = setup_learning
    finished(db)
    models.items = [proposal("platform", content="Improve the task splitting rule and add a regression test.")]
    requested = []

    def engineer(request, **kwargs):
        requested.append(kwargs)
        job = EngineerJob(request=request, **{k: v for k, v in kwargs.items() if k in EngineerJob.model_fields})
        db.save_engineer_job(job)
        return job

    monkeypatch.setattr(executor, "engineer_request", engineer)
    review, [item] = await learn(db, executor)
    assert item.status == "engineering" and requested[0]["origin"] == "retrospective"
    assert not requested[0]["auto_apply"] and requested[0]["budget_usd"] + review.cost_usd <= review.budget_usd
    assert learning.selected(db, "fakecompiler") == []
    job = db.get_engineer_job(item.engineer_job_id)
    job.status, job.checks = "applied", ["backend tests"]
    db.save_engineer_job(job)
    executor.sync_improvements()
    assert db.improvement(item.id).status == "active"


async def test_a_duplicate_lesson_is_not_republished_by_later_reviews(setup_learning):
    db, _, executor = setup_learning
    finished(db)
    await learn(db, executor)
    finished(db, "another")
    executor.start_learning("another")
    await executor.learning_tasks["another"]
    assert db.learning_review("another").status == "done"
    assert len(db.improvements()) == 1


async def test_lessons_are_frozen_before_the_first_step(setup_learning):
    db, _, executor = setup_learning
    finished(db)
    _, [item] = await learn(db, executor)
    run = finished(db, "second")
    learning.attach(db, run)
    another = item.model_copy(update={"id": "another", "title": "new advice"})
    db.save_improvement(another)
    learning.attach(db, run)
    assert [use["id"] for use in run.improvement_uses] == [item.id]


@pytest.mark.skipif(not learning.agent.sandbox_available(), reason="bubblewrap is not installed")
async def test_skill_validation_really_executes_in_an_empty_sandbox(setup_learning):
    db, _, _ = setup_learning
    run = finished(db)
    item = Improvement(run_id=run.id, revision=learning.revision(run), **proposal(
        "skill", check="printf 'sample' > fixture; test \"$(cat fixture)\" = sample; printf 'checked'"))
    assert (await learning.check_skill(item))["passed"] is True
    item.check = "exit 7"
    assert (await learning.check_skill(item))["passed"] is False


async def test_disabled_automation_has_no_model_cost(setup_learning):
    db, models, executor = setup_learning
    finished(db)
    db.save_learning_settings({"enabled": False})
    executor.emit("first", "run.succeeded")
    assert db.learning_review("first") is None and models.prompts == []
    assert db.retrospective("first")


async def test_api_can_start_show_and_disable_a_lesson(task_api, monkeypatch):
    api, client = task_api
    finished(api.db)
    models = Models()
    executor = Executor(api.db, models, {m.id: m for m in [model("x:writer", 90), model("y:reviewer", 92)]})
    monkeypatch.setattr(api, "executor", executor)
    response = await client.put("/api/improvements/settings", json={"enabled": True, "budget_usd": 0.1})
    assert response.status_code == 200
    assert (await client.post("/api/tasks/first/learning")).status_code == 202
    await executor.learning_tasks["first"]
    body = (await client.get("/api/tasks/first/learning")).json()
    assert body["review"]["status"] == "done" and body["retrospective"]["run_id"] == "first"
    [item] = body["improvements"]
    assert (await client.post(f"/api/improvements/{item['id']}/disable")).json()["status"] == "disabled"
    assert (await client.get("/api/tasks/missing/learning")).status_code == 404


def test_delete_removes_learning_records_and_observations(setup_learning):
    db, _, _ = setup_learning
    run = finished(db)
    item = Improvement(run_id=run.id, revision=learning.revision(run), **proposal())
    db.save_improvement(item)
    db.save_learning_review(LearningReview(run_id=run.id, revision=learning.revision(run)))
    db.save_improvement_effect(item.id, "later", {"run_id": "later"})
    db.delete_run(run.id)
    assert db.improvement(item.id) is None and db.learning_review(run.id) is None
    assert db.improvement_effects(item.id) == []


@pytest.mark.parametrize("status", [RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.INCOMPLETE])
async def test_natural_end_still_automatically_learns(setup_learning, status):
    db, models, executor = setup_learning
    run = finished(db)
    run.status = status
    db.save_run(run)
    executor.emit(run.id, f"run.{status.value}")
    await executor.learning_tasks[run.id]
    assert db.learning_review(run.id).status == "done"
    assert db.learning_review(run.id).automatic is True
    assert len(models.prompts) == 2


@pytest.mark.parametrize("active", [False, True])
async def test_user_cancel_never_starts_automatic_retrospective(task_api, monkeypatch, active):
    api, client = task_api
    started = asyncio.Event()

    class Waiting(Models):
        async def complete(self, model_id, prompt, workspace=None, **kwargs):
            if "learning:" in prompt:
                return await super().complete(model_id, prompt, workspace, **kwargs)
            started.set()
            await asyncio.Event().wait()

    models = Waiting()
    executor = Executor(api.db, models, {m.id: m for m in [model("x:writer", 90), model("y:reviewer", 92)]})
    monkeypatch.setattr(api, "executor", executor)
    api.db.save_learning_settings({"enabled": True})
    api.db.save_run(ExecutionRun(id="stop", prompt="p", status=RunStatus.QUEUED, budget_usd=1,
                                 plan=TaskPlan(summary="s", nodes=[TaskNode(
                                     id="n", title="work", instructions="work", capability="coding", model_id="x:writer")])))
    try:
        if active:
            executor.start("stop")
            await asyncio.wait_for(started.wait(), timeout=2)
        assert (await client.post("/api/tasks/stop/cancel")).status_code == 200
        if active:
            await asyncio.gather(executor.tasks["stop"], return_exceptions=True)
        executor.emit("stop", "run.cancelled")
        executor.finish_learning("stop")
        await asyncio.sleep(0)
        assert "stop" not in executor.learning_tasks
        assert api.db.learning_review("stop") is None and api.db.retrospective("stop") is None
        assert api.db.improvements("stop") == [] and not models.prompts
        body = (await client.get("/api/tasks/stop/learning")).json()
        assert body["automatic_eligible"] is False and body["retrospective"] is None
        assert body["automatic_skip_reason"] == "任务已手动取消，不自动复盘。"
        assert api.db.get_run("stop").status == RunStatus.CANCELLED
    finally:
        await executor.shutdown()


@pytest.mark.parametrize("status", ["queued", "analyzing", "validating", "blocked"])
async def test_restart_skips_old_cancelled_automatic_reviews(setup_learning, status):
    db, models, executor = setup_learning
    run = finished(db)
    run.status = RunStatus.CANCELLED
    db.save_run(run)
    # An old record has no automatic field. It must not become a manual request on restart.
    review = LearningReview.model_validate({"run_id": run.id, "revision": learning.revision(run),
                                           "status": status, "error": "old failure"})
    db.save_learning_review(review)
    item = Improvement(run_id=run.id, revision=review.revision, status="validating", **proposal())
    db.save_improvement(item)
    executor.restore_learning()
    assert run.id not in executor.learning_tasks and not models.prompts
    assert db.learning_review(run.id).status == "skipped"
    assert db.learning_review(run.id).error is None
    assert db.improvement(item.id).status == "disabled"


async def test_manual_review_of_cancelled_task_is_explicit_and_survives_restart(setup_learning):
    db, models, executor = setup_learning
    run = finished(db)
    run.status = RunStatus.CANCELLED
    db.save_run(run)
    assert executor.start_learning(run.id) is None
    assert not models.prompts and db.learning_review(run.id) is None
    executor.start_learning(run.id, manual=True)
    await executor.learning_tasks[run.id]
    review = db.learning_review(run.id)
    assert review.status == "done" and review.automatic is False
    [item] = db.improvements(run.id)
    item.status, review.status = "validating", "validating"
    db.save_improvement(item)
    db.save_learning_review(review)
    models.prompts.clear()
    db.save_learning_settings({"enabled": False})
    executor.restore_learning()
    await executor.learning_tasks[run.id]
    assert db.learning_review(run.id).status == "done"
    assert len(models.prompts) == 1 and "learning:validate" in models.prompts[0][1]


@pytest.mark.parametrize("phase", ["analyzing", "validating"])
async def test_cancellation_stops_learning_and_late_reply_cannot_publish(setup_learning, monkeypatch, phase):
    db, models, executor = setup_learning
    run = finished(db)
    if phase == "validating":
        review = LearningReview(run_id=run.id, revision=learning.revision(run), conclusion="existing conclusion")
        db.save_learning_review(review)
        db.save_improvement(Improvement(run_id=run.id, revision=review.revision, **proposal()))
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    complete = models.complete

    async def late(*args, **kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
        # A channel may return a last reply during cancellation cleanup.
        return await complete(*args, **kwargs)

    monkeypatch.setattr(models, "complete", late)
    executor.start_learning(run.id)
    await asyncio.wait_for(started.wait(), timeout=2)
    run.status = RunStatus.CANCELLED
    db.save_run(run)
    executor.cancel(run.id)
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(executor.learning_tasks[run.id], return_exceptions=True)
    assert cancelled.is_set()
    assert db.learning_review(run.id).status == "skipped"
    assert all(item.status == "disabled" for item in db.improvements(run.id))
    assert len(models.prompts) == 1


async def test_skipping_cancelled_work_preserves_completed_reviews(setup_learning):
    db, _, executor = setup_learning
    run = finished(db)
    review, [item] = await learn(db, executor)
    run.status = RunStatus.CANCELLED
    db.save_run(run)
    executor.restore_learning()
    assert db.learning_review(run.id).conclusion == review.conclusion
    assert db.learning_review(run.id).status == "done"
    assert db.improvement(item.id).status == "active"

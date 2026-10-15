import json
from pathlib import Path

import pytest
from super_partner import quality
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
from super_partner.planner import _validate_plan
from super_partner.providers import ProviderResult


def model(model_id: str, coding: float, reliability: float = 80, price: float = 1) -> ModelProfile:
    return ModelProfile(
        id=model_id, provider="x", display_name=model_id, context_window=1_000_000,
        input_per_million=price, output_per_million=price,
        scores=CapabilityScore(coding=coding, reliability=reliability),
    )


MODELS = {m.id: m for m in [model("x:cheap", 82, price=.1), model("x:strong", 97, price=5), model("x:judge", 85, reliability=99)]}


class Script:
    """Fake providers: ``work(model_id, prompt)`` answers tasks, ``review(prompt)`` answers reviews."""

    def __init__(self, work, review=lambda prompt: {"passed": True, "reason": "ok"}):
        self.work, self.review, self.calls = work, review, []

    def configured_models(self, ids):
        return set(ids)

    async def complete(self, model_id, prompt, workspace=None, **_kwargs):
        self.calls.append((model_id, prompt))
        if quality.REVIEW_MARKER in prompt:
            reply = self.review(prompt)
            text = reply if isinstance(reply, str) else json.dumps(reply)
        else:
            text = self.work(model_id, prompt)
        return ProviderResult(text=text, input_tokens=100, output_tokens=100, used_model_id=model_id)


def node(node_id: str, deps=(), **fields) -> TaskNode:
    return TaskNode(id=node_id, title=f"Task {node_id}", instructions=f"do {node_id}", capability="coding",
                    dependencies=list(deps), model_id="x:cheap", **fields)


async def run_plan(tmp_path: Path, nodes: list[TaskNode], providers: Script) -> ExecutionRun:
    database = Database(tmp_path / "test.db")
    run = ExecutionRun(id="run", prompt="demo", status=RunStatus.QUEUED, budget_usd=100,
                       use_cli=False, plan=TaskPlan(summary="demo", nodes=nodes))
    database.save_run(run)
    await Executor(database, providers, MODELS).execute("run")
    return database.get_run("run")


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "review_nodes", True)


async def test_rejected_work_is_retried_on_the_strongest_model_with_the_reviewers_findings(tmp_path):
    reviews = iter([{"passed": False, "reason": "Only described the fix.", "fix": "Edit login.py"},
                    {"passed": True, "reason": "Fixed."}])
    providers = Script(lambda model_id, prompt: f"{model_id} did it", lambda prompt: next(reviews))

    run = await run_plan(tmp_path, [node("a", acceptance_criteria="login.py is changed")], providers)

    a = run.plan.nodes[0]
    assert run.status == RunStatus.SUCCEEDED and a.status == NodeStatus.SUCCEEDED
    assert [(t.model_id, t.outcome) for t in a.attempts] == [("x:cheap", "rejected"), ("x:strong", "passed")]
    assert all(t.reviewer_model_id == "x:judge" for t in a.attempts)
    work = [prompt for model_id, prompt in providers.calls if quality.REVIEW_MARKER not in prompt]
    assert "login.py is changed" in work[0]
    assert "Only described the fix." in work[1] and "Edit login.py" in work[1]
    assert a.result == "x:strong did it" and a.review == "Fixed." and a.feedback is None
    # Review calls are charged too.
    assert run.actual_cost_usd == pytest.approx(sum(t.cost_usd for t in a.attempts)) and run.actual_cost_usd > 0


async def test_a_node_fails_after_its_attempts_run_out(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "max_node_attempts", 2)
    # Without a diagnosis (off in tests) the run fails; with one it pauses (test_diagnosis.py).

    def broken(model_id, prompt):
        raise RuntimeError("model unavailable")

    run = await run_plan(tmp_path, [node("a"), node("b", ["a"])], Script(broken))

    a, b = run.plan.nodes
    assert run.status == RunStatus.FAILED
    assert a.status == NodeStatus.FAILED and [t.outcome for t in a.attempts] == ["error", "error"]
    assert a.result == "model unavailable" and b.status == NodeStatus.PENDING


async def test_failed_verification_sends_the_work_back_with_its_findings(tmp_path):
    verdicts = iter(["Looks wrong.\nVERDICT: FAIL: the tests still fail", "All good.\nVERDICT: PASS"])
    attempts = {"impl": 0}

    def work(model_id, prompt):
        if "You are verifying" in prompt:
            return next(verdicts)
        attempts["impl"] += 1
        return f"implementation v{attempts['impl']}"

    providers = Script(work)
    nodes = [node("impl"), node("check", ["impl"], verifies=["impl"]), node("report", ["check"])]
    run = await run_plan(tmp_path, nodes, providers)

    impl, check, report = run.plan.nodes
    assert run.status == RunStatus.SUCCEEDED and run.rework_rounds == 1
    assert impl.result == "implementation v2"
    assert [t.outcome for t in check.attempts] == ["verify_failed", "passed"]
    second_impl = [p for _, p in providers.calls if p.startswith("Task: do impl")][1]
    assert "the tests still fail" in second_impl and "implementation v1" in second_impl
    # The verifier's own verdict decides; nobody else reviews it.
    assert not any(quality.REVIEW_MARKER in p and "do check" in p for _, p in providers.calls)
    assert report.status == NodeStatus.SUCCEEDED


async def test_rework_stops_after_the_configured_rounds(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "max_rework_rounds", 1)
    providers = Script(lambda model_id, prompt: "VERDICT: FAIL: still broken" if "You are verifying" in prompt else "impl")
    run = await run_plan(tmp_path, [node("impl"), node("check", ["impl"], verifies=["impl"])], providers)
    check = run.plan.nodes[1]
    assert run.status == RunStatus.FAILED and run.rework_rounds == 1
    assert check.status == NodeStatus.FAILED and "still broken" in check.review


async def test_long_results_are_handed_on_as_the_reviewers_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "handoff_max_chars", 500)
    long_text = "x" * 2000

    def review(prompt):
        return {"passed": True, "reason": "ok", "summary": "SHORT SUMMARY"} if '"summary"' in prompt else {"passed": True, "reason": "ok"}

    providers = Script(lambda model_id, prompt: long_text if "do a" in prompt else "done", review)
    run = await run_plan(tmp_path, [node("a"), node("b", ["a"])], providers)

    a, b = run.plan.nodes
    assert a.summary == "SHORT SUMMARY"
    b_prompt = next(p for _, p in providers.calls if p.startswith("Task: do b"))
    assert "SHORT SUMMARY" in b_prompt and long_text not in b_prompt
    assert [(i.node_id, i.form) for i in b.inputs] == [("a", "summary")]


async def test_unreadable_reviews_never_accept_unverified_work(tmp_path):
    providers = Script(lambda model_id, prompt: "no verdict here", lambda prompt: "I think it is fine")
    run = await run_plan(tmp_path, [node("a"), node("check", ["a"], verifies=["a"])], providers)
    a, check = run.plan.nodes
    assert run.status == RunStatus.FAILED
    assert a.review_passed is None and a.status == NodeStatus.FAILED
    assert "无法解析" in a.review and check.status == NodeStatus.PENDING


async def test_missing_verdict_retries_verification_without_accepting_or_reworking_upstream(tmp_path):
    providers = Script(lambda model_id, prompt: "Tests failed; no verdict here")
    run = await run_plan(tmp_path, [node("a"), node("check", ["a"], verifies=["a"])], providers)
    a, check = run.plan.nodes
    assert run.status == RunStatus.FAILED and a.status == NodeStatus.SUCCEEDED
    assert check.review_passed is None and "VERDICT" in check.review
    assert run.rework_rounds == 0


async def test_malformed_reviewer_moves_to_a_backup(tmp_path):
    replies = iter(["not JSON", {"passed": True, "reason": "backup verified the work"}])
    providers = Script(lambda model_id, prompt: "done", lambda prompt: next(replies))
    run = await run_plan(tmp_path, [node("a")], providers)
    reviews = [mid for mid, prompt in providers.calls if quality.REVIEW_MARKER in prompt]
    assert run.status == RunStatus.SUCCEEDED and len(reviews) == 2 and reviews[0] != reviews[1]
    assert len(run.plan.nodes[0].attempts) == 1


def test_changed_files_skip_vcs_and_dependency_folders(tmp_path):
    (tmp_path / "keep.py").write_text("a")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("a")
    before = quality.file_state(tmp_path)
    (tmp_path / "keep.py").write_text("changed!")
    (tmp_path / "new.txt").write_text("n")
    (tmp_path / "node_modules" / "y.js").write_text("a")
    assert quality.changed_files(before, quality.file_state(tmp_path)) == ["keep.py", "new.txt"]


def test_handoff_lists_upstream_files_and_truncates_without_a_summary():
    upstream = node("a", changed_files=["src/app.py", "README.md"])
    upstream.result = "y" * 50
    downstream = node("b", ["a"])
    run = ExecutionRun(id="r", prompt="p", status=RunStatus.RUNNING, budget_usd=1,
                       plan=TaskPlan(summary="s", nodes=[upstream, downstream]))
    prompt, inputs = quality.node_prompt(downstream, run, 10)
    assert "Files it changed: src/app.py, README.md" in prompt and "40 more characters omitted" in prompt
    assert [(i.form, i.changed_files) for i in inputs] == [("truncated", 2)]


def test_planner_keeps_acceptance_criteria_and_only_verifies_its_dependencies():
    plan = _validate_plan({"nodes": [
        {"key": "a", "instructions": "x", "acceptance_criteria": "tests pass"},
        {"key": "b", "instructions": "y"},
        {"key": "c", "instructions": "z", "dependencies": ["a"], "verifies": ["a", "b", "zzz"]},
    ]})
    a, _, c = plan.nodes
    assert a.acceptance_criteria == "tests pass"
    assert c.verifies == [a.id]

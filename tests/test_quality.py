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


async def test_a_rejected_rework_keeps_the_verifications_findings_and_the_reviewer_sees_them(tmp_path):
    verdicts = iter(["VERDICT: FAIL: invalid token must return 403", "VERDICT: PASS"])
    reviews = iter([
        {"passed": True, "reason": "ok"},
        {"passed": False, "reason": "reminder_sent is set without SMTP", "fix": "keep it unsent"},
        {"passed": True, "reason": "ok"},
    ])
    providers = Script(
        lambda model_id, prompt: next(verdicts) if "You are verifying" in prompt else "impl",
        lambda prompt: next(reviews),
    )
    impl = node("impl")
    impl.instructions = "invalid token returns 404"
    run = await run_plan(tmp_path, [impl, node("check", ["impl"], verifies=["impl"])], providers)

    assert run.status == RunStatus.SUCCEEDED and run.rework_rounds == 1
    impl_prompts = [p for _, p in providers.calls if p.startswith("Task: invalid token")]
    assert len(impl_prompts) == 3
    # The retry after the reviewer's rejection still carries the verification's rework.
    assert "must return 403" in impl_prompts[2] and "keep it unsent" in impl_prompts[2]
    reviews_seen = [p for _, p in providers.calls if quality.REVIEW_MARKER in p and "invalid token returns 404" in p]
    assert "must return 403" in reviews_seen[1] and "supersede" in reviews_seen[1]
    assert "must return 403" in reviews_seen[2]


def test_retry_feedback_keeps_only_the_original_rework():
    rework = "验证（Check）发现问题：invalid token must return 403"
    once = quality.retry_feedback(rework, "first rejection")
    twice = quality.retry_feedback(f"{once}\n诊断建议：advice", "second rejection")
    assert once == f"{rework}\n本次验收意见：first rejection"
    assert twice == f"{rework}\n本次验收意见：second rejection"
    assert quality.retry_feedback("an earlier rejection", "new rejection") == "new rejection"
    assert quality.retry_feedback(None, "new rejection") == "new rejection"


def test_review_prompt_shows_rework_and_discounts_sandbox_errors():
    reworked = node("a", feedback="验证（Check）发现问题：invalid token must return 403")
    prompt = quality.review_prompt(reworked, "done", [], "", False)
    assert "invalid token must return 403" in prompt and "supersede" in prompt
    assert "sandbox" in prompt and "socket" in prompt
    assert "Rework requirements" not in quality.review_prompt(node("a"), "done", [], "", False)


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


def test_review_sees_changed_contents_in_a_workspace_without_git(tmp_path):
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "app.js").write_text('go("#/todos/" + todo.id);\n')
    (tmp_path / ".review").mkdir()
    (tmp_path / ".review" / "notes.txt").write_text("reviewer scratch")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\0\0binary")
    changed = ["static/app.js", ".review/notes.txt", "logo.png"]
    evidence = quality.change_evidence(tmp_path, changed)
    prompt = quality.review_prompt(node("a"), "done", changed, evidence, False)
    assert 'go("#/todos/" + todo.id);' in prompt and "static/app.js" in evidence
    assert "reviewer scratch" not in evidence and "PNG" not in evidence


def test_review_evidence_includes_untracked_files_in_a_git_workspace(tmp_path):
    import subprocess
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git[:3], "init", "-q"], check=True)
    (tmp_path / "old.py").write_text("x = 1\n")
    subprocess.run([*git, "add", "old.py"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    (tmp_path / "old.py").write_text("x = 2\n")
    (tmp_path / "new.py").write_text("brand_new = True\n")
    evidence = quality.change_evidence(tmp_path, ["new.py", "old.py"])
    assert "+x = 2" in evidence and "brand_new = True" in evidence


def test_review_evidence_is_clipped(tmp_path):
    (tmp_path / "big.txt").write_text("y" * (quality.MAX_DIFF_CHARS * 2))
    evidence = quality.change_evidence(tmp_path, ["big.txt"])
    assert len(evidence) < quality.MAX_DIFF_CHARS + 200 and "clipped" in evidence


async def test_review_of_a_scratch_task_sees_the_workspace_files(tmp_path):
    """A task without a project folder works in its scratch folder; the reviewer must still see it."""
    def work(model_id, prompt):
        static = settings.workspace_root / "run" / "easylife" / "static"
        static.mkdir(parents=True, exist_ok=True)
        (static / "app.js").write_text("renderTodos(todos);\n")
        return "done"

    providers = Script(work)
    run = await run_plan(tmp_path, [node("a")], providers)
    review = next(p for _, p in providers.calls if quality.REVIEW_MARKER in p)
    assert run.status == RunStatus.SUCCEEDED
    assert "easylife/static/app.js" in review and "renderTodos(todos);" in review


async def test_review_of_an_attempt_that_changed_nothing_sees_the_files_it_names(tmp_path):
    workspace = settings.workspace_root / "run"
    (workspace / "easylife" / "static").mkdir(parents=True)
    (workspace / "easylife" / "static" / "index.html").write_text("<div id=todo-list></div>\n")
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_api.py").write_text("def test_login(): pass\n")
    (workspace / ".env").write_text("SECRET=1\n")
    providers = Script(lambda model_id, prompt: "Checked easylife/static/index.html and .env; all present.")
    await run_plan(tmp_path, [node("a")], providers)
    review = next(p for _, p in providers.calls if quality.REVIEW_MARKER in p)
    assert "Files changed: (none)" in review
    assert "<div id=todo-list></div>" in review
    assert "files in the workspace" in review and "tests/test_api.py" in review
    assert "SECRET=1" not in review


def test_mentioned_files_stay_inside_the_workspace(tmp_path):
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("x")
    (tmp_path / "outside.txt").write_text("x")
    text = f"See src/main.py, {root}/src/main.py, ../outside.txt and missing/file.py."
    assert quality.mentioned_files(root, text) == ["src/main.py"]

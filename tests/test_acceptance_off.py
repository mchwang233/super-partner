"""With step acceptance off (settings.acceptance), a step that finishes has passed: no reviewer, no
declared check, no verification sending work back. A step that says it cannot do its work still
fails, and the lead's final check of the whole result is still made."""
import pytest
from super_partner.catalog import builtin_models
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import ExecutionRun, NodeStatus, RunStatus, TaskNode, TaskPlan
from super_partner.providers import ProviderResult


@pytest.fixture(autouse=True)
def no_acceptance(monkeypatch):
    monkeypatch.setattr(settings, "acceptance", False)
    monkeypatch.setattr(settings, "review_nodes", True)  # even so: acceptance off wins


class Workers:
    """Each step answers ``replies[its title]``; every prompt is kept."""

    def __init__(self, replies):
        self.replies, self.prompts = replies, []

    async def complete(self, model_id, prompt, workspace=None, **_):
        self.prompts.append(prompt)
        title = next((title for title in self.replies if title in prompt), None)
        return ProviderResult(self.replies.get(title, "done"), 10, 10, model_id)


def step(key, **fields):
    return TaskNode(id=key, title=f"Step {key}", instructions=f"do {key}", capability="research",
                    model_id="mock:local", **fields)


async def run(tmp_path, nodes, replies, monkeypatch=None):
    database = Database(tmp_path / "t.db")
    database.save_run(ExecutionRun(id="r", prompt="p", status=RunStatus.QUEUED, budget_usd=5,
                                   plan=TaskPlan(summary="s", nodes=nodes)))
    workers = Workers(replies)
    executor = Executor(database, workers, {m.id: m for m in builtin_models()})
    finals = []

    async def final_review(run_id):
        finals.append(run_id)
        return False

    executor._final_review = final_review
    await executor.execute("r")
    return database.get_run("r"), workers, finals


async def test_a_finished_step_passes_without_a_review_or_its_declared_check(tmp_path):
    result, workers, finals = await run(tmp_path, [step("a", check="false", acceptance_criteria="must be perfect")], {})
    [node] = result.plan.nodes
    assert result.status == RunStatus.SUCCEEDED and node.status == NodeStatus.SUCCEEDED
    assert node.review == "平台未开启验收。" and node.review_passed is None
    assert len(workers.prompts) == 1  # the step itself, no reviewer
    assert finals == ["r"]  # the lead's final check of the whole result is still made


async def test_a_failing_verification_sends_nothing_back(tmp_path):
    nodes = [step("a"), step("v", dependencies=["a"], verifies=["a"])]
    result, workers, _ = await run(tmp_path, nodes, {"do v": "Checked.\nVERDICT: FAIL: a is wrong"})
    assert result.status == RunStatus.SUCCEEDED and result.rework_rounds == 0
    assert len(workers.prompts) == 2


async def test_a_step_that_says_it_cannot_do_its_work_still_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "auto_diagnose", False)
    result, _, _ = await run(tmp_path, [step("a")], {"do a": "UNABLE: vcs is not installed here"})
    assert result.plan.nodes[0].status != NodeStatus.SUCCEEDED and result.status != RunStatus.SUCCEEDED


async def test_the_plan_is_asked_for_without_verification_steps(monkeypatch):
    import json

    from super_partner.models import CapabilityScore, ModelProfile, SupervisorSession
    from super_partner.planner import NO_ACCEPTANCE_NOTE, autonomous_plan
    monkeypatch.setattr(settings, "direct_task_planning", True)
    lead = ModelProfile(id="test:lead", provider="test", display_name="lead", context_window=100000,
                        scores=CapabilityScore(**{key: 95 for key in CapabilityScore.model_fields}),
                        scores_source="artificial_analysis", input_per_million=1, output_per_million=1)
    asked = []

    class Lead:
        async def converse(self, session, text, **kwargs):
            asked.append(text)
            return ProviderResult(json.dumps({"summary": "s", "nodes": [{
                "key": "a", "title": "A", "instructions": "do", "capability": "coding",
                "acceptance_criteria": "ok", "dependencies": []}]}), 1, 1, session.model_id)

    await autonomous_plan("实现模块", [lead], {lead.id}, Lead(), session=SupervisorSession())
    assert NO_ACCEPTANCE_NOTE.strip() in asked[-1]


async def test_a_contract_step_takes_its_inputs_from_a_finished_step_with_no_acceptance_evidence(tmp_path, monkeypatch):
    from artifact_helpers import contract_node, setup_executor

    producer = contract_node("rtl", stage=None, output="rtl.sv")
    consumer = contract_node("sim", stage=None, inputs=[{"path": "rtl.sv", "kind": "file", "source": "dependency"}],
                             output="sim.log", dependencies=["rtl"])
    executor, db, _ = setup_executor(tmp_path, monkeypatch, [producer, consumer])
    monkeypatch.setattr(settings, "acceptance", False)
    await executor.execute("artifacts")
    run = db.get_run("artifacts")
    assert [n.status for n in run.plan.nodes] == [NodeStatus.SUCCEEDED, NodeStatus.SUCCEEDED]
    assert run.plan.nodes[0].artifact_evidence is None or not run.plan.nodes[0].artifact_evidence.accepted

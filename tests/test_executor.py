import asyncio
import os
from pathlib import Path

import httpx
import pytest
from super_partner.catalog import builtin_models
from super_partner.cli_channels import CliChannelRegistry
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import ExecutionRun, NodeStatus, RunStatus, TaskNode, TaskPlan
from super_partner.planner import heuristic_plan
from super_partner.providers import ProviderRegistry, ProviderResult
from super_partner.quota import ModelUnavailable
from super_partner.routing import estimate, route_node


async def test_artifact_pipeline_generates_own_missing_outputs_and_audits_only_accepted_reports(tmp_path, monkeypatch):
    from artifact_helpers import mock_acceptance, setup_executor

    checks = []
    mock_acceptance(monkeypatch, checks)
    executor, db, providers = setup_executor(tmp_path, monkeypatch)
    await executor.execute("artifacts")
    run = db.get_run("artifacts")
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert providers.calls == ["vdb", "report", "audit"]
    assert all(n.artifact_evidence.accepted for n in run.plan.nodes)
    assert [c[0] for c in checks] == ["validate coverage.vdb", "validate report.txt", "validate audit.txt"]
    assert run.actual_cost_usd == pytest.approx(0.006)
    assert all(n.contract_turns == 1 for n in run.plan.nodes)


async def test_artifact_missing_external_input_blocks_without_model_calls_or_downstream_audit(tmp_path, monkeypatch):
    from artifact_helpers import coverage_nodes, setup_executor

    _, report, audit = coverage_nodes()
    report.dependencies = []
    report.artifact_contract.inputs[0].source = "external"
    executor, db, providers = setup_executor(tmp_path, monkeypatch, [report, audit])
    monkeypatch.setattr(settings, "auto_diagnose", True)
    await executor.execute("artifacts")
    run = db.get_run("artifacts")
    assert run.status == RunStatus.INCOMPLETE and providers.calls == []
    assert all(n.status == NodeStatus.BLOCKED for n in run.plan.nodes)
    assert run.plan.nodes[0].attempts[0].outcome == "blocked"
    assert "coverage.vdb" in run.diagnoses[0].report
    assert run.actual_cost_usd == 0


async def test_artifact_cli_cannot_silently_ignore_declared_limits(tmp_path, monkeypatch):
    from artifact_helpers import contract_node, setup_executor

    node = contract_node()
    node.model_id = "cli:codex"
    executor, db, providers = setup_executor(tmp_path, monkeypatch, [node])
    executor.model_lookup[node.model_id] = executor.model_lookup["test:worker"].model_copy(
        update={"id": node.model_id, "channel": "cli"})
    await executor.execute("artifacts")
    node = db.get_run("artifacts").plan.nodes[0]
    assert providers.calls == [] and node.status == NodeStatus.BLOCKED
    assert "通道" in node.review and node.attempts == [] and node.started_at is None
    assert not [e for e in db.events("artifacts") if e.kind == "node.started"]


@pytest.mark.parametrize("declaration", [{}, {"counting_unit": "request", "hard_limit": True},
                                         {"counting_unit": "tool_call", "hard_limit": False}])
async def test_tool_limit_admission_blocks_before_dispatch_with_structured_evidence(tmp_path, monkeypatch, declaration):
    from artifact_helpers import setup_executor

    executor, db, providers = setup_executor(tmp_path, monkeypatch)
    providers.capabilities["test:worker"] = declaration

    async def must_not_start(*args):
        pytest.fail("incompatible work was dispatched")

    monkeypatch.setattr(executor, "_execute_node", must_not_start)
    await executor.execute("artifacts")
    run = db.get_run("artifacts")
    assert run.status == RunStatus.INCOMPLETE
    assert providers.calls == providers.actual_calls == []
    assert all(n.status == NodeStatus.BLOCKED and not n.attempts and n.started_at is None for n in run.plan.nodes)
    events = db.events(run.id)
    assert not any(e.kind == "node.started" for e in events)
    data = next(e.data for e in events if e.kind == "node.blocked")
    assert data["started"] is False and data["code"] == "tool_limit_incompatible"
    assert data["required_capabilities"] == {"counting_unit": "tool_call", "hard_limit": True}
    assert data["channels"][0]["declared_capabilities"] == declaration
    assert data["actual_counts"]["vdb"] == {"requests": 0, "tool_calls": 0, "cost_usd": 0, "limit": 3}
    assert data["affected_dependencies"] == ["audit", "report"]
    assert run.actual_cost_usd == 0


@pytest.mark.parametrize("denial", [None, "unauthorized", "disabled", "budget", "step_budget", "quality", "context"])
async def test_tool_limit_replacement_preserves_authorization_capability_and_budgets(tmp_path, monkeypatch, denial):
    from artifact_helpers import contract_node, mock_acceptance, setup_executor

    node = contract_node()
    node.model_id = "cli:codex"
    executor, db, providers = setup_executor(tmp_path, monkeypatch, [node])
    api = executor.model_lookup["test:worker"]
    executor.model_lookup[node.model_id] = api.model_copy(update={"id": node.model_id, "provider": "cli", "channel": "cli"})
    if denial != "unauthorized":
        providers.authorized.add(api.id)
    if denial == "disabled":
        api.enabled = False
    if denial == "quality":
        api.scores.coding = node.quality_threshold - 1
    if denial == "context":
        api.context_window = 1
    run = db.get_run("artifacts")
    run.use_cli = True
    if denial == "budget":
        run.budget_usd = 0.0001
    if denial == "step_budget":
        run.plan.nodes[0].artifact_contract.max_cost_usd = 0.0001
    before = run.model_copy(deep=True)
    db.save_run(run)
    checks = []
    mock_acceptance(monkeypatch, checks)
    await executor.execute(run.id)
    saved = db.get_run(run.id)
    node = saved.plan.nodes[0]
    assert saved.budget_usd == before.budget_usd and saved.allow_network == before.allow_network
    assert saved.use_cli == before.use_cli and node.artifact_contract == before.plan.nodes[0].artifact_contract
    assert node.acceptance_criteria == before.plan.nodes[0].acceptance_criteria
    if denial:
        assert node.status == NodeStatus.BLOCKED and node.attempts == []
        assert providers.calls == providers.actual_calls == [] and not checks
    else:
        assert saved.status == RunStatus.SUCCEEDED
        assert node.model_id == "test:worker" and [a.model_id for a in node.attempts] == ["test:worker"]
        assert providers.actual_calls == [("vdb", "test:worker")]
        assert node.contract_tool_calls == node.contract_turns == 1
        assert node.artifact_evidence.accepted and checks == [("validate coverage.vdb", False, False)]
        assert any(e.kind == "node.model_switched" and e.data.get("reason") == "tool_limit_compatibility"
                   for e in db.events(run.id))


async def test_tool_limit_admission_rechecks_after_final_channel_changes(tmp_path, monkeypatch):
    from artifact_helpers import contract_node, setup_executor

    executor, db, providers = setup_executor(tmp_path, monkeypatch, [contract_node()])

    def incompatible_after_spreading(run, node):
        providers.capabilities[node.model_id] = {"counting_unit": "request", "hard_limit": False}

    monkeypatch.setattr(executor, "_spread", incompatible_after_spreading)
    await executor.execute("artifacts")
    node = db.get_run("artifacts").plan.nodes[0]
    assert node.status == NodeStatus.BLOCKED and node.attempts == [] and node.started_at is None
    assert providers.calls == providers.actual_calls == []
    assert not any(e.kind == "node.started" for e in db.events("artifacts"))


async def test_compatible_consumer_does_not_start_without_upstream_acceptance(tmp_path, monkeypatch):
    from artifact_helpers import setup_executor

    executor, db, providers = setup_executor(tmp_path, monkeypatch)
    run = db.get_run("artifacts")
    run.plan.nodes[0].status = NodeStatus.SUCCEEDED  # a status alone is not accepted evidence
    db.save_run(run)
    await executor.execute(run.id)
    saved = db.get_run(run.id)
    assert saved.status == RunStatus.INCOMPLETE and providers.calls == []
    assert saved.plan.nodes[0].status == NodeStatus.SUCCEEDED
    assert all(n.status == NodeStatus.BLOCKED and n.attempts == [] and n.started_at is None
               for n in saved.plan.nodes[1:])
    assert not any(e.kind == "node.started" for e in db.events(run.id))


async def test_artifact_worker_blocked_result_preserves_cost_and_log_without_automatic_recovery(tmp_path, monkeypatch):
    from artifact_helpers import contract_node, setup_executor
    from super_partner.providers import ProviderResult

    async def blocked(node, workspace, kwargs):
        kwargs["tool_log"].append({"turn": 1, "tool": "Bash", "detail": "check license", "ok": False, "output": "license missing"})
        return ProviderResult("BLOCKED: license missing", 1000, 1000, "test:worker", turns=1)

    executor, db, providers = setup_executor(tmp_path, monkeypatch, [contract_node()], blocked)
    monkeypatch.setattr(settings, "auto_diagnose", True)
    await executor.execute("artifacts")
    node = db.get_run("artifacts").plan.nodes[0]
    assert providers.calls == ["vdb"] and node.status == NodeStatus.BLOCKED
    assert node.attempts[0].cost_usd == pytest.approx(0.002)
    assert node.attempts[0].tool_calls[0].output == "license missing"


async def test_artifact_blocked_claim_never_runs_downstream_audit(tmp_path, monkeypatch):
    from super_partner.providers import ProviderResult

    class BlockedWorker:
        def __init__(self):
            self.calls = []

        async def complete(self, model_id, prompt, workspace=None, **kwargs):
            self.calls.append(prompt)
            return ProviderResult("BLOCKED: upstream evidence missing", 0, 0, model_id)

    node = TaskNode(id="report", title="Report", instructions="Generate text", capability="research", model_id="mock:local")
    audit = TaskNode(id="audit", title="Audit", instructions="Audit delivery", capability="research", model_id="mock:local", dependencies=["report"])
    db = Database(tmp_path / "blocked.db")
    db.save_run(ExecutionRun(id="blocked", prompt="delivery", status=RunStatus.QUEUED, budget_usd=1,
                             plan=TaskPlan(summary="delivery", nodes=[node, audit])))
    providers = BlockedWorker()
    executor = Executor(db, providers, {model.id: model for model in builtin_models()})
    await executor.execute("blocked")
    assert db.get_run("blocked").status == RunStatus.INCOMPLETE
    assert len(providers.calls) == 1
    assert db.get_run("blocked").plan.nodes[1].status == NodeStatus.BLOCKED


@pytest.fixture(autouse=True)
def execution_only(monkeypatch):
    # Scheduler doubles do not supply reviewers; acceptance is tested in test_quality.py.
    monkeypatch.setattr(settings, "review_nodes", False)


@pytest.mark.asyncio
async def test_mock_execution_completes(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    catalog = builtin_models()
    plan = heuristic_plan("Research and implement a small feature")
    for node in plan.nodes:
        # As the API does, lower thresholds the only model (the demo, at 80) cannot reach.
        node.quality_threshold = min(node.quality_threshold, 80)
        route_node(node, catalog, {"mock:local"})
    plan.estimate = estimate(plan.nodes, 1)
    run = ExecutionRun(id="demo", prompt="demo", status=RunStatus.QUEUED, plan=plan, budget_usd=1)
    database.save_run(run)
    providers = ProviderRegistry()
    executor = Executor(database, providers, {model.id: model for model in catalog})
    await executor.execute(run.id)
    result = database.get_run(run.id)
    await providers.client.aclose()
    assert result is not None
    assert result.status == RunStatus.SUCCEEDED
    assert result.final_result
    assert all(node.result for node in result.plan.nodes)


@pytest.mark.asyncio
async def test_resume_reruns_nodes_interrupted_while_running(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    catalog = builtin_models()
    plan = heuristic_plan("Analyze this task")
    for node in plan.nodes:
        route_node(node, catalog, {"mock:local"})
    for node in plan.nodes[:-1]:
        node.status = NodeStatus.SUCCEEDED
        node.result = "done"
    plan.nodes[-1].status = NodeStatus.RUNNING
    run = ExecutionRun(id="resumed", prompt="demo", status=RunStatus.QUEUED, plan=plan, budget_usd=1)
    database.save_run(run)
    providers = ProviderRegistry()
    executor = Executor(database, providers, {model.id: model for model in catalog})
    await executor.execute(run.id)
    result = database.get_run(run.id)
    await providers.client.aclose()
    assert result is not None
    assert result.status == RunStatus.SUCCEEDED
    assert result.plan.nodes[-1].status == NodeStatus.SUCCEEDED
    assert result.final_result


class SlowProviders:
    def __init__(self, delay: float = 0.05, tokens: tuple[int, int] = (10, 10)):
        self.delay = delay
        self.tokens = tokens
        self.active = 0
        self.peak = 0
        self.calls = 0

    async def complete(self, model_id, prompt, workspace=None, **kwargs):
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.active -= 1
        return ProviderResult(
            text="ok", input_tokens=self.tokens[0], output_tokens=self.tokens[1], used_model_id=model_id,
        )


def wide_run(
    database: Database, width: int, cost: float, budget: float, model_id: str = "mock:local",
) -> ExecutionRun:
    nodes = [
        TaskNode(id=f"n{i}", title=f"Node {i}", instructions="work", capability="planning",
                 model_id=model_id, estimated_cost_usd=cost)
        for i in range(width)
    ]
    run = ExecutionRun(id="wide", prompt="demo", status=RunStatus.QUEUED,
                       plan=TaskPlan(summary="wide", nodes=nodes), budget_usd=budget)
    database.save_run(run)
    return run


@pytest.mark.asyncio
async def test_every_ready_step_starts_at_once_by_default(tmp_path: Path):
    assert settings.max_parallel_nodes == 0
    database = Database(tmp_path / "test.db")
    run = wide_run(database, width=8, cost=0, budget=1)
    providers = SlowProviders(delay=0.1)
    await Executor(database, providers, {model.id: model for model in builtin_models()}).execute(run.id)
    assert database.get_run(run.id).status == RunStatus.SUCCEEDED and providers.peak == 8


@pytest.mark.asyncio
async def test_executor_respects_parallel_limit(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "max_parallel_nodes", 2)
    database = Database(tmp_path / "test.db")
    run = wide_run(database, width=5, cost=0, budget=1)
    providers = SlowProviders()
    executor = Executor(database, providers, {model.id: model for model in builtin_models()})
    await executor.execute(run.id)
    assert database.get_run(run.id).status == RunStatus.SUCCEEDED
    assert providers.calls == 5
    assert providers.peak == 2


@pytest.mark.asyncio
async def test_executor_holds_nodes_that_would_exceed_budget(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    run = wide_run(database, width=3, cost=0.4, budget=1, model_id="openai:gpt-5.6-sol")
    # 20k input + 16k output tokens on GPT-5.6 Sol cost exactly the $0.40 estimate.
    providers = SlowProviders(delay=0, tokens=(20_000, 16_000))
    executor = Executor(database, providers, {model.id: model for model in builtin_models()})
    await executor.execute(run.id)
    result = database.get_run(run.id)
    assert result.status == RunStatus.PAUSED
    assert providers.calls == 2
    assert [node.status for node in result.plan.nodes].count(NodeStatus.PENDING) == 1
    assert database.events(run.id)[-1].kind == "approval.required"


@pytest.mark.asyncio
async def test_cancel_stops_running_nodes(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    run = wide_run(database, width=2, cost=0, budget=1)
    providers = SlowProviders(delay=30)
    executor = Executor(database, providers, {model.id: model for model in builtin_models()})
    executor.start(run.id)
    while providers.active < 2:
        await asyncio.sleep(0.01)
    cancelled = database.get_run(run.id)
    cancelled.status = RunStatus.CANCELLED
    database.save_run(cancelled)
    executor.cancel(run.id)
    with pytest.raises(asyncio.CancelledError):
        await executor.tasks[run.id]
    result = database.get_run(run.id)
    assert providers.active == 0
    assert result.status == RunStatus.CANCELLED
    assert {node.status for node in result.plan.nodes} == {NodeStatus.CANCELLED}


@pytest.mark.asyncio
async def test_a_cancelled_attempt_keeps_its_turns_tool_calls_and_trace(tmp_path: Path, monkeypatch):
    """What an attempt did is kept however it ends, so the task can be looked back on."""
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    database = Database(tmp_path / "test.db")
    run = wide_run(database, width=1, cost=0, budget=1)
    started = asyncio.Event()

    class Working:
        async def complete(self, model_id, prompt, workspace=None, tool_log=None, tool_trace=None, **kwargs):
            tool_log.append({"turn": 3, "tool": "Bash", "detail": "make test", "ok": False, "output": "1 failed"})
            tool_trace.parent.mkdir(parents=True, exist_ok=True)
            tool_trace.write_text('{"turn": 3, "tool": "Bash", "output": "the whole output"}\n')
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError as exc:
                exc.usage = ProviderResult("", 100, 20, model_id, turns=7)
                raise

    executor = Executor(database, Working(), {model.id: model for model in builtin_models()})
    executor.start(run.id)
    await started.wait()
    cancelled = database.get_run(run.id)
    cancelled.status = RunStatus.CANCELLED
    database.save_run(cancelled)
    executor.cancel(run.id)
    with pytest.raises(asyncio.CancelledError):
        await executor.tasks[run.id]
    [attempt] = database.get_run(run.id).plan.nodes[0].attempts
    assert attempt.turns == 7 and [call.detail for call in attempt.tool_calls] == ["make test"]
    assert attempt.trace and "the whole output" in Path(attempt.trace).read_text()
    assert Path(attempt.trace).is_relative_to(tmp_path / "data")


@pytest.mark.asyncio
async def test_cancelled_cli_call_kills_spawned_processes(tmp_path: Path, monkeypatch):
    child_pid_file = tmp_path / "child.pid"
    fake_cli = tmp_path / "codex"
    fake_cli.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {child_pid_file}\nwait\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    task = asyncio.create_task(CliChannelRegistry().complete("cli:codex", "hi", tmp_path))
    while not child_pid_file.exists() or not child_pid_file.read_text().strip():
        await asyncio.sleep(0.01)
    child_pid = int(child_pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(200):
        if not process_alive(child_pid):
            break
        await asyncio.sleep(0.01)
    assert not process_alive(child_pid)


def process_alive(pid: int) -> bool:
    """True unless the process is gone or a zombie awaiting reaping by its new parent."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


async def test_provider_retries_rate_limited_requests(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    responses = [
        httpx.Response(429, headers={"retry-after": "0"}),
        httpx.Response(503),
        httpx.Response(200, json={
            "choices": [{"message": {"content": "done"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }),
    ]
    calls = []

    def handler(request):
        calls.append(request)
        return responses[len(calls) - 1]

    providers = ProviderRegistry()
    providers.retry_base_delay = 0
    providers.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await providers.complete("deepseek:deepseek-chat", "hello")
    await providers.client.aclose()
    assert result.text == "done"
    assert len(calls) == 3


async def test_provider_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    providers = ProviderRegistry()
    providers.retry_base_delay = 0
    providers.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    with pytest.raises(ModelUnavailable, match="500"):
        await providers.complete("deepseek:deepseek-chat", "hello")
    assert providers.quota.exhausted("deepseek:deepseek-chat")
    await providers.client.aclose()


class FixedProviders:
    def __init__(self, used_model_id=None):
        self.used_model_id = used_model_id

    async def complete(self, model_id, prompt, workspace=None, **_):
        return ProviderResult(text=f"out {prompt.split()[1]}", input_tokens=1000,
                              output_tokens=1000, used_model_id=self.used_model_id or model_id)


def graph_run(nodes, budget=1.0):
    return ExecutionRun(id="graph", prompt="demo", status=RunStatus.QUEUED, budget_usd=budget,
                        plan=TaskPlan(summary="graph", nodes=nodes))


async def test_final_result_comes_from_graph_end_nodes_not_list_order(tmp_path: Path):
    nodes = [
        TaskNode(id="report", title="Report", instructions="report", capability="research",
                 model_id="mock:local", dependencies=["data"]),
        TaskNode(id="data", title="Data", instructions="data", capability="research",
                 model_id="mock:local"),
    ]
    database = Database(tmp_path / "test.db")
    database.save_run(graph_run(nodes))
    await Executor(database, FixedProviders(), {m.id: m for m in builtin_models()}).execute("graph")
    assert database.get_run("graph").final_result == "out report"


async def test_parallel_end_nodes_are_all_in_final_result(tmp_path: Path):
    nodes = [
        TaskNode(id=key, title=key.title(), instructions=key, capability="research",
                 model_id="mock:local")
        for key in ("left", "right")
    ]
    database = Database(tmp_path / "test.db")
    database.save_run(graph_run(nodes))
    await Executor(database, FixedProviders(), {m.id: m for m in builtin_models()}).execute("graph")
    assert database.get_run("graph").final_result == "Left:\nout left\n\nRight:\nout right"


async def test_finished_work_is_kept_when_used_model_left_the_catalog(tmp_path: Path):
    node = TaskNode(id="only", title="Only", instructions="only", capability="coding",
                    model_id="cli:codex", estimated_cost_usd=0.25)
    database = Database(tmp_path / "test.db")
    database.save_run(graph_run([node]))
    catalog = {m.id: m for m in builtin_models()}
    await Executor(database, FixedProviders("openai:removed"), catalog).execute("graph")
    result = database.get_run("graph")
    assert result.status == RunStatus.SUCCEEDED
    assert result.plan.nodes[0].result == "out only"
    assert result.actual_cost_usd == 0.25

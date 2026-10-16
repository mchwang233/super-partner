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

from pathlib import Path

import pytest
from conftest import scored_models
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import ExecutionRun, NodeStatus, RunStatus, TaskNode, TaskPlan


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8000", "192.168.1.5:8000"])
async def test_rebinding_hosts_are_refused(task_api, host):
    _, client = task_api
    response = await client.get("/api/tasks", headers={"host": host})
    assert response.status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost", "LOCALHOST:8000", "[::1]:8000"])
async def test_local_hosts_are_served(task_api, host):
    _, client = task_api
    assert (await client.get("/api/tasks", headers={"host": host})).status_code == 200


async def test_cross_site_writes_are_refused_but_console_and_cli_writes_pass(task_api):
    _, client = task_api
    url = "/api/tasks/missing/cancel"
    assert (await client.post(url, headers={"origin": "https://evil.example"})).status_code == 403
    assert (await client.post(url, headers={"origin": "null"})).status_code == 403
    assert (await client.post(url, headers={"origin": "http://localhost:5173"})).status_code == 404
    assert (await client.post(url)).status_code == 404


async def test_startup_pauses_interrupted_runs_instead_of_resuming(task_api, monkeypatch):
    api, _ = task_api
    monkeypatch.setattr(api.catalog_sync, "start", lambda: None)
    for run_id, status in [("running", RunStatus.RUNNING), ("queued", RunStatus.QUEUED)]:
        api.db.save_run(ExecutionRun(
            id=run_id, prompt="demo", status=status, budget_usd=1,
            plan=TaskPlan(summary="demo", nodes=[]),
        ))
    async with api.lifespan(api.app):
        for run_id in ("running", "queued"):
            assert api.db.get_run(run_id).status == RunStatus.PAUSED
            assert run_id not in api.executor.tasks
            events = api.db.events(run_id)
            assert [(event.kind, event.data) for event in events] == [
                ("approval.required", {"reason": "interrupted"})
            ]


def fallback_executor(tmp_path: Path, **overrides):
    catalog = {model.id: model for model in scored_models()}
    for model_id, enabled in overrides.items():
        catalog[model_id].enabled = enabled
    return Executor(Database(tmp_path / "test.db"), None, catalog)


def cli_run(budget: float, actual: float = 0.0, other_running_cost: float = 0.0):
    node = TaskNode(id="cli", title="CLI", instructions="do", capability="coding",
                    model_id="cli:claude")
    other = TaskNode(id="api", title="API", instructions="do", capability="coding",
                     model_id="anthropic:claude-sonnet-5", status=NodeStatus.RUNNING,
                     estimated_cost_usd=other_running_cost)
    return ExecutionRun(id="run", prompt="demo", status=RunStatus.RUNNING, budget_usd=budget,
                        actual_cost_usd=actual, plan=TaskPlan(summary="demo", nodes=[node, other]))


def test_fallback_needs_the_api_model_enabled(tmp_path):
    run = cli_run(budget=100)
    node = run.plan.nodes[0]
    assert fallback_executor(tmp_path)._fallback_allowed(run, node)
    disabled = fallback_executor(tmp_path, **{"anthropic:claude-sonnet-5": False})
    assert not disabled._fallback_allowed(run, node)


def test_fallback_must_fit_the_budget_left_by_spent_and_running_nodes(tmp_path):
    executor = fallback_executor(tmp_path)
    assert not executor._fallback_allowed(cli_run(budget=1e-9), cli_run(1e-9).plan.nodes[0])
    assert not executor._fallback_allowed(cli_run(budget=1, actual=1), cli_run(1).plan.nodes[0])
    crowded = cli_run(budget=1, other_running_cost=1)
    assert not executor._fallback_allowed(crowded, crowded.plan.nodes[0])


def test_non_cli_nodes_are_unaffected(tmp_path):
    run = cli_run(budget=0.0001)
    assert fallback_executor(tmp_path)._fallback_allowed(run, run.plan.nodes[1])

@pytest.mark.parametrize("unsafe", ["../outside.txt", "/tmp/outside.txt", ".env", ".git/config", "secrets.json", ".super-partner/tasks.db"])
def test_artifact_paths_cannot_read_private_or_outside_files(tmp_path, unsafe):
    from super_partner.contracts import ArtifactBlocked, fingerprint
    from super_partner.models import Artifact

    with pytest.raises(ArtifactBlocked):
        fingerprint(tmp_path, Artifact(path=unsafe))


def test_artifact_directory_does_not_follow_symlinks(tmp_path):
    from super_partner.contracts import ArtifactBlocked, fingerprint
    from super_partner.models import Artifact

    root = tmp_path / "work"
    (root / "coverage.vdb").mkdir(parents=True)
    (tmp_path / "outside").write_text("outside")
    (root / "coverage.vdb" / "link").symlink_to(tmp_path / "outside")
    with pytest.raises(ArtifactBlocked, match="符号链接"):
        fingerprint(root, Artifact(path="coverage.vdb", kind="directory"))



def test_artifact_directory_records_links_within_itself_without_following_them(tmp_path):
    # VCS links shared files across the tests of a coverage database.
    from super_partner.contracts import ArtifactBlocked, fingerprint
    from super_partner.models import Artifact

    root = tmp_path / "work"
    common, test = root / "fifo.vdb" / "common", root / "fifo.vdb" / "testdata" / "seed1"
    common.mkdir(parents=True)
    test.mkdir(parents=True)
    (common / "shape.xml").write_text("<shape/>")
    (test / "shape.xml").symlink_to("../../common/shape.xml")
    vdb = Artifact(path="fifo.vdb", kind="directory")
    before = fingerprint(root, vdb)
    (test / "shape.xml").unlink()
    (test / "shape.xml").symlink_to("../../common/other.xml")
    assert fingerprint(root, vdb) != before  # where a link points is part of the evidence
    (test / "escape").symlink_to("../../../")
    with pytest.raises(ArtifactBlocked, match="符号链接"):
        fingerprint(root, vdb)

def test_artifact_host_checks_cannot_read_custom_platform_data_names(tmp_path, monkeypatch):
    from super_partner.config import settings
    from super_partner.contracts import ArtifactBlocked, fingerprint
    from super_partner.models import Artifact

    monkeypatch.setattr(settings, "data_dir", tmp_path / "private-store")
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "all-jobs")
    monkeypatch.setattr(settings, "database_path", tmp_path / "hidden.sqlite")
    for name in ("private-store/evidence.txt", "all-jobs/another-task/report.txt", "hidden.sqlite", "hidden.sqlite-wal"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private-data")
        with pytest.raises(ArtifactBlocked, match="平台"):
            fingerprint(tmp_path, Artifact(path=name))
    own = settings.workspace_root / "this-task"
    own.mkdir()
    (own / "report.txt").write_text("own evidence")
    assert fingerprint(own, Artifact(path="report.txt"))


async def test_artifact_cost_reservation_preserves_task_budget_and_declared_cap():
    import asyncio

    from super_partner.budget import BudgetExceeded, RequestBudget, StepLimitExceeded
    from super_partner.models import ModelProfile

    model = ModelProfile(id="test:model", provider="test", display_name="test", input_per_million=1, output_per_million=1)

    async def charge(cost):
        pass

    # Plenty of task budget cannot buy a request forbidden by the work package's limit.
    budget = RequestBudget({model.id: model}, lambda: 10, {}, charge, asyncio.Event(), step_remaining=lambda: 0.000001)
    with pytest.raises(StepLimitExceeded):
        await budget.reserve(model.id, {"input": "test"})
    # Plenty of step budget cannot bypass the run budget either.
    budget = RequestBudget({model.id: model}, lambda: 0.000001, {}, charge, asyncio.Event(), step_remaining=lambda: 10)
    with pytest.raises(BudgetExceeded):
        await budget.reserve(model.id, {"input": "test"})
    reservations = {object(): 0.999}
    budget = RequestBudget({model.id: model}, lambda: 1, reservations, charge, asyncio.Event(), step_remaining=lambda: 0.002)
    maximum = await budget.reserve(model.id, {"input": "test"}, 4096)
    assert 1 <= maximum < 1000
    assert sum(reservations.values()) <= 1
    budget.release()


async def test_artifact_acceptance_keeps_task_network_and_host_permissions(tmp_path, monkeypatch):
    from artifact_helpers import contract_node, mock_acceptance, setup_executor

    checks = []
    mock_acceptance(monkeypatch, checks)
    executor, _, _ = setup_executor(tmp_path, monkeypatch, [contract_node()])
    await executor.execute("artifacts")
    assert checks == [("validate coverage.vdb", False, False)]


async def test_artifact_simultaneous_progress_checks_share_step_reservations():
    import asyncio

    from super_partner.budget import RequestBudget
    from super_partner.models import ModelProfile

    model = ModelProfile(id="test:model", provider="test", display_name="test", input_per_million=1, output_per_million=1)
    reservations, changed = {}, asyncio.Event()

    async def charge(cost):
        pass

    def budget():
        return RequestBudget({model.id: model}, lambda: 10, reservations, charge, changed,
                             step_remaining=lambda: 0.001, scope="node")

    work, check = budget(), budget()
    await work.reserve(model.id, {"input": "work"}, 1000)
    pending = asyncio.create_task(check.reserve(model.id, {"input": "check"}, 1000))
    await asyncio.sleep(0)
    assert not pending.done()
    work.release()
    await asyncio.wait_for(pending, 1)
    assert sum(reservations.values()) <= 0.001
    check.release()


async def test_artifact_progress_check_cannot_deadlock_waiting_on_its_worker():
    import asyncio

    from super_partner.budget import RequestBudget, StepReservationBusy
    from super_partner.models import ModelProfile

    model = ModelProfile(id="test:model", provider="test", display_name="test", input_per_million=1, output_per_million=1)
    reservations = {}

    async def charge(cost):
        pass

    def budget(wait):
        return RequestBudget({model.id: model}, lambda: 10, reservations, charge, asyncio.Event(),
                             step_remaining=lambda: 0.001, scope="node", wait_for_step=wait)

    worker, checker = budget(True), budget(False)
    await worker.reserve(model.id, {"input": "work"}, 1000)
    with pytest.raises(StepReservationBusy):
        await asyncio.wait_for(checker.reserve(model.id, {"input": "check"}, 1000), 1)
    worker.release()



def test_a_key_file_is_refused_for_holding_a_key_not_for_its_name(tmp_path):
    # VCS writes ucli.key, a log of simulator commands, into every run directory.
    from super_partner.contracts import ArtifactBlocked, fingerprint
    from super_partner.models import Artifact

    run = tmp_path / "artifacts" / "run1"
    run.mkdir(parents=True)
    (run / "sim.log").write_text("PASS")
    (run / "ucli.key").write_text("run\nquit\n")
    folder = Artifact(path="artifacts", kind="directory")
    assert fingerprint(tmp_path, folder)
    (run / "server.key").write_text("-----BEGIN RSA PRIVATE KEY-----\nMIIE\n")
    with pytest.raises(ArtifactBlocked, match="私钥"):
        fingerprint(tmp_path, folder)

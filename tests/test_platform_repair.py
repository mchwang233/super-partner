import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from super_partner import agent
from super_partner import platform_repair as platform
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import Diagnosis, NodeStatus, RunStatus
from super_partner.providers import ProviderResult
from test_diagnosis import MODELS, Providers, one_node_run


@pytest.fixture
def source_checkout(tmp_path, monkeypatch):
    root = tmp_path / "app"
    files = {
        "pyproject.toml": '[project]\nname="super-partner"\nversion="0.1.0"\n',
        "backend/super_partner/api.py": "VALUE = 'broken'\n# unrelated user edit\n",
        "frontend/package.json": "{}\n",
        "tests/test_regression.py": "def test_existing():\n    assert True\n",
        "skills/platform-repair/SKILL.md": "---\nname: platform-repair\n---\n# Repairing Super Partner\n",
        "docs/ARCHITECTURE.md": "# Super Partner architecture map\n",
        "AGENTS.md": "# Super Partner\n",
        ".super-partner/super-partner.db": "keep task data",
        ".env": "keep private configuration",
        "workspaces/kept/file.txt": "keep project",
    }
    for name, contents in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    monkeypatch.setattr(platform, "service_properties", lambda unit: {
        "MainPID": str(os.getpid()), "WorkingDirectory": str(root), "ActiveState": "active",
    })
    return root


def edit_candidate(job):
    source = Path(job["folder"]) / "source"
    path = source / "backend/super_partner/api.py"
    path.write_text(path.read_text().replace("broken", "fixed"))
    (source / "tests/test_new_regression.py").write_text("def test_regression():\n    assert True\n")


async def passing_check(*args, **kwargs):
    return {"returncode": 0, "output": "checks passed"}


async def candidate(tmp_path, source_checkout, monkeypatch):
    manager = platform.PlatformRepairs(source_checkout, tmp_path / "data", "http://localhost:8000")
    monkeypatch.setattr(manager, "_check_command", passing_check)
    job = await manager.prepare("r", "d")
    edit_candidate(job)
    await manager.check(job)
    return manager, job


async def test_source_copy_and_validation_keep_data_and_user_edits_separate(tmp_path, source_checkout, monkeypatch):
    manager, job = await candidate(tmp_path, source_checkout, monkeypatch)
    copied = Path(job["folder"]) / "source"
    assert not (copied / ".env").exists()
    assert not (copied / "workspaces").exists()
    assert not (copied / ".super-partner").exists()
    assert "broken" in (source_checkout / "backend/super_partner/api.py").read_text()
    assert "unrelated user edit" in (copied / "backend/super_partner/api.py").read_text()
    assert len(job["checks"]) == 4
    assert job["status"] == "validated"
    assert manager.pending() is None


async def test_failed_checks_and_missing_regression_cannot_apply_a_patch(tmp_path, source_checkout, monkeypatch):
    manager = platform.PlatformRepairs(source_checkout, tmp_path / "data", "http://localhost:8000")
    job = await manager.prepare("r", "d")
    path = Path(job["folder"]) / "source/backend/super_partner/api.py"
    path.write_text("VALUE = 'fixed'\n")
    with pytest.raises(platform.RepairError, match="回归测试"):
        await manager.check(job)
    edit_candidate(job)

    async def failed(*args, **kwargs):
        return {"returncode": 1, "output": "regression still fails"}

    monkeypatch.setattr(manager, "_check_command", failed)
    with pytest.raises(platform.RepairError, match="regression still fails"):
        await manager.check(job)
    assert "broken" in (source_checkout / "backend/super_partner/api.py").read_text()
    assert manager.pending() is None


async def test_older_repairs_without_network_permission_validate_offline(tmp_path, source_checkout, monkeypatch):
    manager = platform.PlatformRepairs(source_checkout, tmp_path / "data", "http://localhost:8000")
    job = await manager.prepare("r", "d")
    job.pop("allow_network")
    edit_candidate(job)
    permissions = []

    async def check(*args, allow_network):
        permissions.append(allow_network)
        return await passing_check()

    monkeypatch.setattr(manager, "_check_command", check)
    await manager.check(job)
    assert permissions == [False] * 4


@pytest.mark.parametrize("allow_network", [False, True])
@pytest.mark.parametrize("pasta_available", [False, True])
async def test_validation_uses_task_network_and_proxy_policy_without_provider_keys(
    tmp_path, source_checkout, monkeypatch, allow_network, pasta_available,
):
    manager = platform.PlatformRepairs(source_checkout, tmp_path / "data", "http://localhost:8000")
    job = await manager.prepare("r", "d", allow_network=allow_network)
    source = Path(job["folder"]) / "source"
    for name in agent.PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3180")
    monkeypatch.setenv("NO_PROXY", "example.test")
    monkeypatch.setenv("OPENAI_API_KEY", "private-provider-key")
    monkeypatch.setattr(agent, "network_available", lambda: pasta_available)
    captured = {}

    class Process:
        returncode = 0

        async def communicate(self):
            return b"checked\n", None

    async def spawn(*args, **kwargs):
        captured.update(args=args, **kwargs)
        return Process()

    monkeypatch.setattr(platform.asyncio, "create_subprocess_exec", spawn)
    result = await manager._check_command(["true"], source, source, allow_network=allow_network)
    assert result == {"returncode": 0, "output": "checked\n"}
    args, env = captured["args"], captured["env"]
    assert ("--unshare-net" not in args) == allow_network
    assert (args[0] == "pasta") == (allow_network and pasta_available)
    if allow_network and pasta_available:
        assert args[args.index("-T") + 1] == "3180"
        assert "--no-map-gw" in args
    assert env.get("HTTPS_PROXY") == ("http://127.0.0.1:3180" if allow_network else None)
    assert env.get("NO_PROXY") == ("example.test" if allow_network else None)
    assert "OPENAI_API_KEY" not in env
    assert env["SUPER_PARTNER_VALIDATION_ALLOW_NETWORK"] == str(allow_network).lower()
    assert env["SUPER_PARTNER_DATA_DIR"] == str(source / ".validation/data")


@pytest.mark.skipif(not agent.sandbox_available(), reason="bubblewrap is not installed")
@pytest.mark.parametrize("allow_network", [False, True])
async def test_real_validation_sandbox_obeys_network_permission_and_keeps_data_private(
    tmp_path, source_checkout, monkeypatch, request, allow_network,
):
    if allow_network:
        request.getfixturevalue("network_enabled")
        if not agent.network_available():
            pytest.skip("pasta is not installed")
    manager = platform.PlatformRepairs(source_checkout, tmp_path / "data", "http://localhost:8000")
    job = await manager.prepare("r", "d", allow_network=allow_network)
    source = Path(job["folder"]) / "source"

    async def greet(reader, writer):
        writer.write(b"validation-proxy")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    proxy = await asyncio.start_server(greet, "127.0.0.1", 0)
    local = await asyncio.start_server(greet, "127.0.0.1", 0)
    port, other = (server.sockets[0].getsockname()[1] for server in (proxy, local))
    for name in agent.PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("OPENAI_API_KEY", "private-provider-key")
    # A bounded local probe checks route/proxy policy without contacting the internet.
    probe = """
import json, os, socket, sys
from pathlib import Path
def receive(port):
    try:
        with socket.create_connection(('127.0.0.1', int(port)), timeout=2) as connection:
            return connection.recv(100).decode()
    except OSError:
        return None
Path('validation-output.txt').write_text('checked')
print(json.dumps({
    'route': any(line.split()[1:2] == ['00000000'] for line in Path('/proc/net/route').read_text().splitlines()),
    'proxy': receive(sys.argv[1]), 'local': receive(sys.argv[2]),
    'proxy_env': os.environ.get('HTTPS_PROXY'), 'key': os.environ.get('OPENAI_API_KEY'),
    'private_file_visible': Path(sys.argv[3]).exists(),
    'tun': Path('/dev/net/tun').is_char_device(),
}))
"""
    try:
        result = await manager._check_command(
            [sys.executable, "-c", probe, str(port), str(other), str(source_checkout / ".env")],
            source, source, allow_network=allow_network,
        )
        assert result["returncode"] == 0, result["output"]
        observed = json.loads(result["output"])
        assert observed["route"] is allow_network
        assert observed["proxy"] == ("validation-proxy" if allow_network else None)
        assert observed["proxy_env"] == (f"http://127.0.0.1:{port}" if allow_network else None)
        assert observed["local"] is None
        assert observed["key"] is None and not observed["private_file_visible"]
        assert observed["tun"] is allow_network
        assert (source / "validation-output.txt").read_text() == "checked"
    finally:
        proxy.close()
        local.close()
        await proxy.wait_closed()
        await local.wait_closed()


async def test_concurrent_source_changes_are_not_overwritten(tmp_path, source_checkout, monkeypatch):
    manager, job = await candidate(tmp_path, source_checkout, monkeypatch)
    path = source_checkout / "backend/super_partner/api.py"
    path.write_text("another edit arrived\n")
    with pytest.raises(platform.RepairError, match="其他操作修改"):
        await manager.launch(job)
    assert path.read_text() == "another edit arrived\n"


@pytest.mark.parametrize("healthy", [True, False])
async def test_activation_applies_or_rolls_back_without_touching_task_data(tmp_path, source_checkout, monkeypatch, healthy):
    manager, job = await candidate(tmp_path, source_checkout, monkeypatch)
    job["status"] = "scheduled"
    manager.save(job)
    restarts = []
    monkeypatch.setattr(platform.subprocess, "run", lambda args, **kwargs: restarts.append(args))
    checks = []

    def health(*args):
        checks.append(True)
        if not healthy and len(checks) == 1:
            raise platform.RepairError("new API did not start")

    monkeypatch.setattr(platform, "wait_healthy", health)
    platform.activate(Path(job["folder"]) / "job.json")
    saved = json.loads((Path(job["folder"]) / "job.json").read_text())
    assert saved["status"] == ("activated" if healthy else "rolled_back")
    content = (source_checkout / "backend/super_partner/api.py").read_text()
    assert ("fixed" if healthy else "broken") in content
    assert "unrelated user edit" in content
    assert len(restarts) == (1 if healthy else 2)
    assert (source_checkout / ".super-partner/super-partner.db").read_text() == "keep task data"
    assert (source_checkout / "workspaces/kept/file.txt").read_text() == "keep project"
    assert (source_checkout / "tests/test_new_regression.py").exists() == healthy


async def test_candidate_tampering_after_validation_prevents_activation(tmp_path, source_checkout, monkeypatch):
    manager, job = await candidate(tmp_path, source_checkout, monkeypatch)
    job["status"] = "scheduled"
    manager.save(job)
    (Path(job["folder"]) / "source/backend/super_partner/api.py").write_text("unverified change")
    platform.activate(Path(job["folder"]) / "job.json")
    saved = json.loads((Path(job["folder"]) / "job.json").read_text())
    assert saved["status"] == "failed"
    assert "broken" in (source_checkout / "backend/super_partner/api.py").read_text()


@pytest.mark.parametrize("network_before,network_after", [(False, False), (True, True), (False, True), (True, False)])
async def test_platform_diagnosis_edits_copy_and_only_then_schedules_activation(
    tmp_path, source_checkout, monkeypatch, network_before, network_after,
):
    monkeypatch.setattr(settings, "platform_root", source_checkout)
    monkeypatch.setattr(settings, "platform_self_repair", True)
    monkeypatch.setattr(settings, "auto_diagnose", True)
    monkeypatch.setattr(settings, "review_nodes", False)

    class PlatformProvider(Providers):
        async def complete(self, model_id, prompt, workspace=None, **kwargs):
            if prompt.startswith("[[super-partner:platform-repair]]"):
                assert kwargs["repair"] is False
                # Every model that repairs gets the skill and the map, and its copy has them too.
                assert "# Repairing Super Partner" in prompt and "architecture map" in prompt
                assert "name: platform-repair" not in prompt
                assert (workspace / "AGENTS.md").is_file() and (workspace / "skills/platform-repair/SKILL.md").is_file()
                assert workspace != source_checkout
                assert json.loads((workspace.parent / "job.json").read_text())["allow_network"] == network_before
                current = db.get_run("r")
                current.allow_network = network_after
                db.save_run(current)
                edit_candidate({"folder": str(workspace.parent)})
                return ProviderResult("修复调度器并添加回归检查。\nSTATUS: FIXED", 0, 0, model_id)
            if kwargs.get("repair"):
                assert str(source_checkout) in prompt
                assert "LOCATION:" in prompt and "# Repairing Super Partner" in prompt
                return ProviderResult("Super Partner 调度器没有正确处理步骤状态。\nSTATUS: PLATFORM_REPAIR", 0, 0, model_id)
            raise RuntimeError("scheduler defect")

    db = Database(tmp_path / "test.db")
    one_node_run(db)
    current = db.get_run("r")
    current.allow_network = network_before
    db.save_run(current)
    executor = Executor(db, PlatformProvider(), MODELS)
    permissions = []

    async def check(*args, allow_network=False):
        permissions.append(allow_network)
        return await passing_check()

    monkeypatch.setattr(executor.platform, "_check_command", check)
    launched = asyncio.Event()

    async def capture(job):
        launched.set()

    monkeypatch.setattr(executor, "_activate_platform", capture)
    await executor.execute("r")
    assert executor.platform_pending is not None, db.get_run("r").diagnoses
    await asyncio.wait_for(launched.wait(), timeout=2)
    run = db.get_run("r")
    assert run.status == RunStatus.PAUSED
    assert run.maintenance_id == executor.platform_pending["id"]
    assert run.plan.nodes[0].status == NodeStatus.PENDING
    assert run.diagnoses[-1].scope == "super_partner"
    assert run.diagnoses[-1].verdict == "platform_pending"
    assert executor.platform_pending["allow_network"] == network_after
    assert permissions == [network_after] * 5
    assert "broken" in (source_checkout / "backend/super_partner/api.py").read_text()


def test_restart_only_resumes_held_runs_with_remaining_budget(tmp_path, source_checkout, monkeypatch):
    db = Database(tmp_path / "test.db")
    one_node_run(db)
    original = db.get_run("r")
    for identifier, status, marker, budget in [
        ("held", RunStatus.PAUSED, "repair", 1),
        ("user-paused", RunStatus.PAUSED, None, 1),
        ("cancelled", RunStatus.CANCELLED, "repair", 1),
        ("over-budget", RunStatus.PAUSED, "repair", 0),
    ]:
        run = original.model_copy(deep=True)
        run.id, run.status, run.maintenance_id, run.budget_usd = identifier, status, marker, budget
        run.actual_cost_usd = .1
        run.diagnoses = [Diagnosis(id="d", trigger="auto", status="done", repair_id="repair")]
        db.save_run(run)
    executor = Executor(db, Providers(), MODELS)
    restarted = []
    monkeypatch.setattr(executor, "start", restarted.append)
    executor._finish_platform({"id": "repair", "status": "activated", "run_id": "held", "files": ["backend/x.py"]})
    assert restarted == ["held"]
    assert db.get_run("user-paused").status == RunStatus.PAUSED
    assert db.get_run("cancelled").status == RunStatus.CANCELLED
    assert db.get_run("over-budget").status == RunStatus.PAUSED
    assert db.shared_repairs()[0]["scope"] == "super_partner"


def test_source_symlinks_cannot_escape_the_repair(tmp_path, source_checkout):
    external = tmp_path / "secret.py"
    external.write_text("private")
    (source_checkout / "backend/linked.py").symlink_to(external)
    with pytest.raises(platform.RepairError, match="符号链接"):
        platform.source_files(source_checkout)

import asyncio
import os
import subprocess
from pathlib import Path

import httpx
import pytest
from super_partner.catalog import builtin_models
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import ExecutionRun, NodeStatus, RunStatus, TaskNode, TaskPlan
from super_partner.planner import _validate_plan, heuristic_plan
from super_partner.projects import (
    NativePickerError,
    ProjectDirError,
    browse_directories,
    git_snapshot,
    native_pick_directory,
    native_picker_status,
    project_context,
    resolve_project_dir,
)
from super_partner.providers import ProviderError, ProviderRegistry, ProviderResult
from super_partner.routing import NoEligibleModelError, route_node


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "home"
    (root / "app").mkdir(parents=True)
    monkeypatch.setattr(settings, "project_roots", [root])
    monkeypatch.setattr(settings, "data_dir", root / "app" / ".super-partner")
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "scratch")
    return root


def test_resolve_project_dir_accepts_directory_under_root(project_root: Path):
    assert resolve_project_dir(str(project_root / "app" / ".." / "app")) == (project_root / "app").resolve()


@pytest.mark.parametrize("raw, message", [
    ("relative/app", "absolute"),
    ("/nonexistent-super-partner-dir", "does not exist"),
])
def test_resolve_project_dir_rejects_bad_paths(project_root: Path, raw: str, message: str):
    with pytest.raises(ProjectDirError, match=message):
        resolve_project_dir(raw)


def test_resolve_project_dir_rejects_outside_roots(project_root: Path, tmp_path: Path):
    with pytest.raises(ProjectDirError, match="allowed root"):
        resolve_project_dir(str(tmp_path))


def test_resolve_project_dir_rejects_data_dir(project_root: Path):
    settings.data_dir.mkdir()
    with pytest.raises(ProjectDirError, match="data directory"):
        resolve_project_dir(str(settings.data_dir))


def test_git_snapshot_and_context_describe_repository(tmp_path: Path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
        check=True,
    )
    (tmp_path / "new.txt").write_text("x")
    snapshot = git_snapshot(tmp_path)
    assert snapshot["git"] and snapshot["branch"] == "main"
    assert snapshot["uncommitted_files"] == 1
    context = project_context(tmp_path)
    assert "README.md" in context and "1 uncommitted" in context


def test_git_snapshot_handles_plain_directory(tmp_path: Path):
    assert git_snapshot(tmp_path) == {"git": False}


def test_workspace_nodes_only_route_to_cli():
    node = heuristic_plan("Implement this feature", in_project=True).nodes[0]
    assert node.needs_workspace
    models = builtin_models()
    route_node(node, models, {"cli:qwen", "openai:gpt-5.6-sol", "mock:local"})
    assert node.model_id == "cli:qwen"
    with pytest.raises(NoEligibleModelError, match="membership CLI"):
        route_node(node, models, {"openai:gpt-5.6-sol", "mock:local"})


def test_planner_reads_needs_workspace_flag():
    plan = _validate_plan({"nodes": [
        {"key": "a", "instructions": "edit", "needs_workspace": True},
        {"key": "b", "instructions": "sum up", "dependencies": ["a"], "needs_workspace": "yes"},
    ]})
    assert [node.needs_workspace for node in plan.nodes] == [True, False]


class RecordingProviders:
    def __init__(self, project=None):
        self.project = project
        self.calls = []
        self.active_in_project = 0
        self.peak_in_project = 0

    async def complete(self, model_id, prompt, workspace=None, allow_api_fallback=True, **_):
        self.calls.append((Path(workspace), allow_api_fallback, prompt))
        in_project = Path(workspace) == self.project
        self.active_in_project += in_project
        self.peak_in_project = max(self.peak_in_project, self.active_in_project)
        await asyncio.sleep(0.02)
        self.active_in_project -= in_project
        return ProviderResult(text="ok", input_tokens=0, output_tokens=0, used_model_id=model_id)


@pytest.mark.asyncio
async def test_project_nodes_run_serially_in_project_without_fallback(project_root: Path, tmp_path: Path):
    project = project_root / "app"
    nodes = [
        TaskNode(id=f"edit{i}", title=f"Edit {i}", instructions="edit", capability="coding",
                 model_id="cli:codex", needs_workspace=True)
        for i in range(3)
    ] + [TaskNode(id="notes", title="Notes", instructions="write notes", capability="research",
                  model_id="cli:gemini")]
    run = ExecutionRun(id="proj", prompt="demo", status=RunStatus.QUEUED, budget_usd=1,
                       plan=TaskPlan(summary="proj", nodes=nodes), project_dir=str(project))
    database = Database(tmp_path / "test.db")
    database.save_run(run)
    providers = RecordingProviders(project)
    executor = Executor(database, providers, {model.id: model for model in builtin_models()})
    await executor.execute(run.id)
    result = database.get_run(run.id)
    assert result.status == RunStatus.SUCCEEDED
    assert {node.status for node in result.plan.nodes} == {NodeStatus.SUCCEEDED}
    project_calls = [call for call in providers.calls if call[0] == project]
    assert len(project_calls) == 3
    assert all(not fallback and str(project) in prompt for _, fallback, prompt in project_calls)
    assert providers.peak_in_project == 1
    scratch_calls = [call for call in providers.calls if call[0] != project]
    assert [call[0] for call in scratch_calls] == [settings.workspace_root / run.id]
    snapshot = next(event for event in database.events(run.id) if event.kind == "workspace.snapshot")
    assert snapshot.data["project_dir"] == str(project)


async def test_cli_failure_without_fallback_never_calls_api(tmp_path: Path, monkeypatch):
    failing_cli = tmp_path / "claude"
    failing_cli.write_text("#!/bin/sh\necho boom >&2\nexit 1\n")
    failing_cli.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    api_calls = []
    providers = ProviderRegistry()
    providers.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: api_calls.append(request) or httpx.Response(500)),
    )
    with pytest.raises(ProviderError, match="boom"):
        await providers.complete("cli:claude", "edit", tmp_path, allow_api_fallback=False)
    await providers.client.aclose()
    assert api_calls == []


async def test_planner_marks_workspace_nodes_only_for_project_runs():
    class NoPlanner:
        async def complete(self, *_args, **_kwargs):
            raise AssertionError("mock planner should not be called")

    models = builtin_models()
    from super_partner.planner import autonomous_plan
    scratch = await autonomous_plan("Implement a fix", models, {"mock:local"}, NoPlanner())
    project = await autonomous_plan("Implement a fix", models, {"mock:local"}, NoPlanner(), "Target project: /x")
    assert not any(node.needs_workspace for node in scratch.nodes)
    assert any(node.needs_workspace for node in project.nodes)


def test_browse_lists_subdirectories_within_roots(project_root: Path, tmp_path: Path):
    (project_root / "app" / ".git").mkdir()
    (project_root / "docs").mkdir()
    (project_root / ".cache").mkdir()
    (project_root / "notes.txt").write_text("x")
    (project_root / "escape").symlink_to(tmp_path)
    listing = browse_directories()
    assert listing["path"] == str(project_root.resolve())
    assert listing["parent"] is None
    assert [(entry["name"], entry["git"]) for entry in listing["entries"]] == [("app", True), ("docs", False)]
    assert ".cache" in [entry["name"] for entry in browse_directories(show_hidden=True)["entries"]]
    child = browse_directories(str(project_root / "app"))
    assert child["parent"] == str(project_root.resolve()) and child["git"]


def test_browse_hides_data_dir_and_rejects_outside_roots(project_root: Path, tmp_path: Path):
    settings.data_dir.mkdir()
    assert browse_directories(str(project_root / "app"))["entries"] == []
    with pytest.raises(ProjectDirError, match="allowed root"):
        browse_directories(str(tmp_path))


def test_native_picker_needs_a_desktop_and_zenity(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert native_picker_status() == {"available": False, "reason": "No desktop session is attached to the API process"}
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr("super_partner.projects.shutil.which", lambda _: None)
    assert native_picker_status()["reason"] == "zenity is not installed"
    monkeypatch.setattr("super_partner.projects.shutil.which", lambda name: f"/bin/{name}")
    assert native_picker_status() == {"available": True, "reason": None}


@pytest.fixture
def fake_zenity(project_root: Path, tmp_path: Path, monkeypatch):
    def install(script: str):
        (tmp_path / "zenity").write_text(f"#!/bin/sh\n{script}\n")
        (tmp_path / "zenity").chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
        monkeypatch.setenv("DISPLAY", ":0")
    return install


@pytest.mark.asyncio
async def test_native_pick_returns_chosen_directory(project_root: Path, fake_zenity):
    fake_zenity(f"echo {project_root / 'app'}")
    assert await native_pick_directory() == (project_root / "app").resolve()


@pytest.mark.asyncio
async def test_native_pick_returns_none_when_cancelled(fake_zenity):
    fake_zenity("exit 1")
    assert await native_pick_directory() is None


@pytest.mark.asyncio
async def test_native_pick_rejects_directory_outside_roots(tmp_path: Path, fake_zenity):
    fake_zenity("echo /etc")
    with pytest.raises(ProjectDirError, match="allowed root"):
        await native_pick_directory()


@pytest.mark.asyncio
async def test_native_pick_reports_dialog_failure(fake_zenity):
    fake_zenity("echo 'cannot open display' >&2\nexit 255")
    with pytest.raises(NativePickerError, match="cannot open display"):
        await native_pick_directory()


@pytest.mark.asyncio
async def test_native_pick_kills_a_dialog_that_outlives_the_timeout(fake_zenity, monkeypatch):
    fake_zenity("sleep 30")
    monkeypatch.setattr("super_partner.projects.NATIVE_PICKER_TIMEOUT", 0.2)
    with pytest.raises(asyncio.TimeoutError):
        await native_pick_directory()

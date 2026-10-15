import io
import json
import subprocess
import zipfile

import httpx
import pytest
from super_partner import publish, secrets
from super_partner.config import settings
from super_partner.models import ExecutionRun, NodeStatus, RunStatus, TaskNode, TaskPlan

ORIGIN = {"origin": "http://localhost:5173"}


def finished_run(run_id="done", project_dir=None, status=RunStatus.SUCCEEDED, changed=()) -> ExecutionRun:
    node = TaskNode(id="n", title="Build", instructions="build", capability="coding", model_id="cli:codex",
                    status=NodeStatus.SUCCEEDED, result="Built the game", changed_files=list(changed))
    return ExecutionRun(id=run_id, prompt="写一个游戏", status=status, budget_usd=1, project_dir=project_dir,
                        final_result="Game is ready", plan=TaskPlan(summary="Tetris game", nodes=[node]))


@pytest.fixture
def scratch(task_api, tmp_path, monkeypatch):
    api, client = task_api
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    root = settings.workspace_root / "done"
    (root / "tetris").mkdir(parents=True)
    (root / "tetris" / "index.html").write_text("<canvas></canvas>")
    (root / "tetris" / "logo.bin").write_bytes(b"\x00\x01")
    (root / ".review").mkdir()
    (root / ".review" / "note.txt").write_text("internal")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text("{}")
    api.db.save_run(finished_run())
    return api, client, root


async def test_output_files_skip_agent_folders_and_stay_inside(scratch):
    _, client, _ = scratch
    listing = (await client.get("/api/tasks/done/files")).json()
    assert [item["path"] for item in listing["files"]] == ["tetris/index.html", "tetris/logo.bin"]
    assert listing["in_project"] is False
    content = await client.get("/api/tasks/done/files/content", params={"path": "tetris/index.html"})
    assert content.json()["text"] == "<canvas></canvas>"
    for path in ["../../etc/passwd", ".review/note.txt", "tetris/../.claude/settings.json"]:
        assert (await client.get("/api/tasks/done/files/content", params={"path": path})).status_code == 400
    binary = await client.get("/api/tasks/done/files/content", params={"path": "tetris/logo.bin"})
    assert "二进制" in binary.json()["detail"]


async def test_archive_holds_the_output_and_a_result_record(scratch):
    _, client, _ = scratch
    response = await client.get("/api/tasks/done/archive")
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"].startswith('attachment; filename="tetris-game-')
    bundle = zipfile.ZipFile(io.BytesIO(response.content))
    assert sorted(bundle.namelist()) == ["RESULT.md", "tetris/index.html", "tetris/logo.bin"]
    record = bundle.read("RESULT.md").decode()
    assert "Game is ready" in record and "Built the game" in record and "写一个游戏" in record


async def test_project_tasks_only_output_the_files_they_changed(task_api, tmp_path):
    api, client = task_api
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("changed")
    (project / "untouched.py").write_text("mine")
    api.db.save_run(finished_run("proj", str(project), changed=["app.py", "deleted.py"]))
    listing = (await client.get("/api/tasks/proj/files")).json()
    assert [item["path"] for item in listing["files"]] == ["app.py"]
    response = await client.post("/api/tasks/proj/github", json={"name": "x"}, headers=ORIGIN)
    assert response.status_code == 409


def bare_remote(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    return remote


def remote_files(remote) -> list[str]:
    return subprocess.run(["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                          capture_output=True, text=True, check=True).stdout.split()


async def test_publishing_creates_the_repository_and_pushes_the_output(scratch, tmp_path, monkeypatch):
    api, client, _ = scratch
    remote = bare_remote(tmp_path)
    created = []

    def github(request: httpx.Request) -> httpx.Response:
        created.append((request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(201, json={"html_url": "https://github.com/me/tetris", "clone_url": str(remote)})

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token_value")
    monkeypatch.setattr(api.providers, "client", httpx.AsyncClient(transport=httpx.MockTransport(github)))
    response = await client.post("/api/tasks/done/github", json={"name": "tetris", "private": True}, headers=ORIGIN)

    assert response.status_code == 200, response.text
    assert response.json() == {"url": "https://github.com/me/tetris"}
    assert created == [("Bearer ghp_test_token_value", {"name": "tetris", "private": True, "description": "Tetris game", "auto_init": False})]
    assert remote_files(remote) == ["RESULT.md", "tetris/index.html", "tetris/logo.bin"]
    assert api.db.get_run("done").github_url == "https://github.com/me/tetris"
    assert (await client.get("/api/tasks/done/files")).json()["github_url"] == "https://github.com/me/tetris"


async def test_publishing_without_a_token_needs_an_existing_repository(scratch, monkeypatch):
    api, client, _ = scratch
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    response = await client.post("/api/tasks/done/github", json={"name": "tetris"}, headers=ORIGIN)
    assert response.status_code == 400 and "令牌" in response.json()["detail"]
    response = await client.post("/api/tasks/done/github", json={"remote": "https://evil.example/x.git"}, headers=ORIGIN)
    assert response.status_code == 400 and "GitHub 仓库地址" in response.json()["detail"]
    api.db.save_run(finished_run("running", status=RunStatus.RUNNING))
    assert (await client.post("/api/tasks/running/github", json={"name": "x"}, headers=ORIGIN)).status_code == 409


def test_push_snapshot_to_an_existing_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    settings.data_dir.mkdir()
    (settings.workspace_root / "done").mkdir(parents=True)
    (settings.workspace_root / "done" / "main.py").write_text("print('hi')")
    remote = bare_remote(tmp_path)
    publish.push_snapshot(finished_run(), str(remote))
    assert remote_files(remote) == ["RESULT.md", "main.py"]
    assert not list(settings.data_dir.iterdir())  # staging is cleaned up
    assert publish.check_remote("git@github.com:me/repo.git") == "git@github.com:me/repo.git"
    with pytest.raises(publish.PublishError):
        publish.check_remote("git@github.com:me/repo.git; rm -rf /")


async def test_github_token_is_kept_with_the_other_secrets(task_api, monkeypatch):
    _, client = task_api
    writes = []
    monkeypatch.setattr(secrets, "_keyring_write", lambda action, provider, value=None, timeout=None: writes.append((action, provider)))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert (await client.get("/api/github")).json()["token"] is False
    assert (await client.put("/api/github/token", json={"token": "ghp_saved_token_1"}, headers=ORIGIN)).json() == {"stored": "keyring"}
    assert (await client.get("/api/github")).json()["token"] is True
    assert (await client.delete("/api/github/token", headers=ORIGIN)).json() == {"removed": "keyring"}
    assert writes == [("set", "github"), ("delete", "github")]
    assert (await client.get("/api/github")).json()["token"] is False

"""Acceptance must see earlier work and verification output without gaining write access."""
import json
import subprocess

import httpx
import pytest
from super_partner import agent, quality
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import (
    CapabilityScore,
    ExecutionRun,
    ModelProfile,
    RunStatus,
    TaskNode,
    TaskPlan,
)
from super_partner.providers import ProviderRegistry, ProviderResult


def commit_fixture(root):
    git = ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "fixture"], check=True)


@pytest.mark.parametrize("tracked", [False, True])
def test_long_delivery_document_cannot_displace_code_and_tests(tmp_path, tracked):
    (tmp_path / "DELIVERY.md").write_text("previous delivery\n")
    (tmp_path / "db.py").write_text("CREATE_TABLE = 'CREATE TABLE email_tokens'\n")
    (tmp_path / "test_db.py").write_text("def test_existing_users_migrated(): pass\n")
    if tracked:
        commit_fixture(tmp_path)
    (tmp_path / "DELIVERY.md").write_text("delivery detail\n" * quality.MAX_DIFF_CHARS)

    evidence = quality.change_evidence(tmp_path, ["DELIVERY.md", "db.py", "test_db.py"])

    assert "CREATE TABLE email_tokens" in evidence
    assert "def test_existing_users_migrated" in evidence
    assert "clipped" in evidence
    assert len(evidence) < quality.MAX_DIFF_CHARS + 1000


def test_explicit_verification_log_is_included_but_private_files_are_not(tmp_path):
    (tmp_path / ".tmp-verify").mkdir()
    log = tmp_path / ".tmp-verify" / "backend.log"
    log.write_text("test detail\n" * 10_000 + "Ran 155 tests in 92.794s\nOK\n")
    (tmp_path / ".env").write_text("SECRET=never-include\n")
    (tmp_path / ".tmp-verify" / "credentials.json").write_text('{"token":"never-include"}')
    (tmp_path / "db.py").write_text("migrated = True\n")
    references = quality.mentioned_files(
        tmp_path, "See db.py, .tmp-verify/backend.log:10001, .env and .tmp-verify/credentials.json"
    )
    assert references == ["db.py", ".tmp-verify/backend.log"]
    evidence = quality.change_evidence(tmp_path, references, referenced=references)
    assert "Ran 155 tests in 92.794s\nOK" in evidence
    assert "never-include" not in evidence
    assert "Ran 155 tests" not in quality.change_evidence(tmp_path, [".tmp-verify/backend.log"])


@pytest.mark.parametrize("tracked", [False, True])
def test_inline_evidence_filters_private_files_and_symlink_targets(tmp_path, tracked):
    root = tmp_path / "work"
    root.mkdir()
    for name in [".env", "credentials.json", "secrets.json", "server.key"]:
        (root / name).write_text("private-value\n")
    (tmp_path / "outside.py").write_text("outside-value\n")
    (root / "outside.py").symlink_to(tmp_path / "outside.py")
    (root / "alias.py").symlink_to(root / ".env")
    if tracked:
        commit_fixture(root)
        (root / ".env").write_text("private-value-updated\n")
    evidence = quality.change_evidence(root, [p.name for p in root.iterdir() if p.is_file()])
    assert "private-value" not in evidence
    assert "outside-value" not in evidence
    assert "credentials.json" not in evidence
    assert "alias.py" not in evidence


async def test_review_reads_a_log_tail_and_line_ranges(tmp_path):
    (tmp_path / ".tmp-verify").mkdir()
    (tmp_path / ".tmp-verify" / "tests.log").write_text(
        "test passed\n" * 5000 + "Ran 155 tests in 92.794s\nOK\n"
    )
    (tmp_path / "db.py").write_text("first\nmigration\nlast\n")
    ws = agent.Workspace(tmp_path, read_only=True, readable_files=[".tmp-verify/tests.log"])
    output = await ws.call("read_file", {"path": ".tmp-verify/tests.log", "tail": True, "limit": 2})
    assert "Ran 155 tests in 92.794s\nOK" in output
    assert "5001-5002 of 5002" in output
    assert "migration" in await ws.call("read_file", {"path": "db.py", "offset": 2, "limit": 1})


async def test_review_rejects_mutations_and_private_or_outside_reads(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    (root / "db.py").write_text("original\n")
    blocked = [".env", ".env.local", "credentials.json", "secrets.json", "server.pem",
               ".ssh/id_rsa", ".aws/credentials", ".git/config", ".tmp-verify/unreferenced.log",
               ".tmp-verify/.env", ".tmp-verify/secrets.json"]
    for name in blocked:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private-value\n")
    (tmp_path / "outside.txt").write_text("outside-value\n")
    (root / "outside-link").symlink_to(tmp_path / "outside.txt")
    (root / "secret-link").symlink_to(root / ".env")
    ws = agent.Workspace(root, read_only=True, host=True, network=True, readable_files=[
        ".env", ".tmp-verify/.env", ".tmp-verify/secrets.json", ".ssh/id_rsa", "../outside.txt",
    ])
    for name in [*blocked, "../outside.txt", str(tmp_path / "outside.txt"), "outside-link", "secret-link"]:
        assert (await ws.call("read_file", {"path": name})).startswith("Error:"), name
    listing = await ws.call("list_dir", {})
    assert "db.py" in listing
    assert ".env" not in listing and "credentials.json" not in listing and "secret-link" not in listing
    assert (await ws.call("list_dir", {"path": ".."})).startswith("Error:")
    for name, args in [
        ("write_file", {"path": "created.txt", "content": "changed"}),
        ("edit_file", {"path": "db.py", "old_text": "original", "new_text": "changed"}),
        ("run_command", {"command": "touch created.txt"}),
    ]:
        assert "read-only" in await ws.call(name, args)
    assert (root / "db.py").read_text() == "original\n"
    assert not (root / "created.txt").exists()


@pytest.mark.parametrize("provider", ["openai", "anthropic", "google"])
async def test_api_review_exposes_only_read_tools_and_returns_the_log_tail(tmp_path, monkeypatch, provider):
    for name in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"]:
        monkeypatch.setenv(name, "test")
    review_folder = tmp_path / "review"
    project = tmp_path / "project"
    review_folder.mkdir()
    (project / ".tmp-verify").mkdir(parents=True)
    (project / ".tmp-verify" / "tests.log").write_text("details\n" * 5000 + "Ran 155 tests\nOK\n")
    args = {"path": ".tmp-verify/tests.log", "tail": True, "limit": 2}
    calls = [("read_file", args), ("write_file", {"path": "bad.txt", "content": "bad"})]
    verdict = '{"passed":true,"reason":"verified"}'
    sent = []

    def respond(request):
        body = json.loads(request.content)
        sent.append(body)
        first = len(sent) == 1
        if provider == "openai":
            payload = {"choices": [{"message": {"tool_calls": [
                {"id": str(i), "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
                for i, (name, arguments) in enumerate(calls)
            ]} if first else {"content": verdict}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        elif provider == "anthropic":
            payload = {"content": [
                {"type": "tool_use", "id": str(i), "name": name, "input": arguments}
                for i, (name, arguments) in enumerate(calls)
            ] if first else [{"type": "text", "text": verdict}], "usage": {"input_tokens": 10, "output_tokens": 5}}
        else:
            payload = {"candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"name": name, "args": arguments}} for name, arguments in calls
            ] if first else [{"text": verdict}]}}], "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}
        return httpx.Response(200, json=payload)

    registry = ProviderRegistry()
    await registry.client.aclose()
    registry.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        result = await registry.complete(
            f"{provider}:reviewer", "Review the work", review_folder,
            review_workspace=project, review_files=[".tmp-verify/tests.log"], max_turns=3,
        )
    finally:
        await registry.client.aclose()
    assert result.text == verdict and result.input_tokens == 20 and result.output_tokens == 10
    if provider == "openai":
        offered = [tool["function"]["name"] for tool in sent[0]["tools"]]
    elif provider == "anthropic":
        offered = [tool["name"] for tool in sent[0]["tools"]]
    else:
        offered = [tool["name"] for tool in sent[0]["tools"][0]["functionDeclarations"]]
    assert set(offered) == {"list_dir", "read_file"}
    assert "Ran 155 tests\\nOK" in json.dumps(sent[1])
    assert "Error:" in json.dumps(sent[1]) and "read-only" in json.dumps(sent[1])
    assert not (project / "bad.txt").exists() and not (review_folder / "bad.txt").exists()


async def test_new_changes_do_not_hide_earlier_files_and_review_uses_the_project(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "review_nodes", True)
    project = tmp_path / "project"
    project.mkdir()
    (project / "DELIVERY.md").write_text("delivery\n" * quality.MAX_DIFF_CHARS)
    (project / "db.py").write_text("existing_users_migrated = True\n")
    (project / "test_db.py").write_text("def test_tokens(): pass\n")
    (project / ".tmp-verify").mkdir()
    (project / ".tmp-verify" / "tests.log").write_text("Ran 155 tests\nOK\n")
    task = TaskNode(id="work", title="Work", instructions="Implement email verification; check test_db.py",
                    capability="coding", model_id="openai:worker", changed_files=["db.py"])
    run = ExecutionRun(id="review-run", prompt="implement", status=RunStatus.RUNNING, budget_usd=10,
                       use_cli=False, project_dir=str(project), plan=TaskPlan(summary="implement", nodes=[task]))
    reviewer = ModelProfile(id="openai:reviewer", provider="openai", display_name="Reviewer",
                            context_window=1_000_000, input_per_million=1, output_per_million=1,
                            scores=CapabilityScore(coding=99, reliability=99))
    captured = []

    class Providers:
        def configured_models(self, ids):
            return set(ids)

        async def complete(self, model_id, prompt, workspace=None, **kwargs):
            captured.append((prompt, workspace, kwargs))
            return ProviderResult('{"passed":true,"reason":"checked"}', 10, 10, model_id)

    database = Database(tmp_path / "test.db")
    try:
        database.save_run(run)
        executor = Executor(database, Providers(), {reviewer.id: reviewer})
        verdict = await executor._check(run, task, "See .tmp-verify/tests.log", ["DELIVERY.md"], project)
        assert verdict.passed
        prompt, folder, kwargs = captured[0]
        assert "Files changed: DELIVERY.md" in prompt
        assert "existing_users_migrated = True" in prompt and "def test_tokens()" in prompt
        assert "Ran 155 tests\nOK" in prompt
        assert kwargs["review_workspace"] == project
        assert ".tmp-verify/tests.log" in kwargs["review_files"]
        assert folder != project
        await executor._finish(run.id, task.id, ProviderResult("done", 0, 0), verdict, [], ["DELIVERY.md"], 0, None)
        assert set(database.get_run(run.id).plan.nodes[0].changed_files) == {"db.py", "DELIVERY.md"}
    finally:
        database.connection.close()

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
    NodeStatus,
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
    output = await ws.call("Read", {"file_path": ".tmp-verify/tests.log", "offset": -2})
    assert "5001\tRan 155 tests in 92.794s\n  5002\tOK" in output
    assert "5001-5002 of 5002" in output
    assert "migration" in await ws.call("Read", {"file_path": "db.py", "offset": 2, "limit": 1})


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
        assert (await ws.call("Read", {"file_path": name})).startswith("Error:"), name
    listing = await ws.call("LS", {})
    assert "db.py" in listing
    assert ".env" not in listing and "credentials.json" not in listing and "secret-link" not in listing
    assert (await ws.call("LS", {"path": ".."})).startswith("Error:")
    for name, args in [
        ("Write", {"file_path": "created.txt", "content": "changed"}),
        ("Edit", {"file_path": "db.py", "old_string": "original", "new_string": "changed"}),
    ]:
        assert "read-only" in await ws.call(name, args)
    if agent.sandbox_available():
        # Its commands cannot change the workspace or see its credentials either, nor reach the network.
        assert "read-only" in (await ws.call("Bash", {"command": "touch created.txt; echo x >> db.py"})).lower()
        # (An uncited log in a hidden folder is not private: only the file tools leave it out.)
        private = [name for name in blocked if name != ".tmp-verify/unreferenced.log"]
        seen = await ws.call("Bash", {"command": "cat db.py " + " ".join(private) + " secret-link; ls -a .ssh"})
        assert "original" in seen and "private-value" not in seen
        assert ws.network is False
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
    args = {"file_path": ".tmp-verify/tests.log", "offset": -2}
    calls = [("Read", args), ("Write", {"file_path": "bad.txt", "content": "bad"})]
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
    assert set(offered) == {"Read", "Glob", "Grep", "LS"} | ({"Bash"} if agent.sandbox_available() else set())
    assert "\\tRan 155 tests\\n  5002\\tOK" in json.dumps(sent[1])
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


@pytest.mark.parametrize("name", ["build/tb/compile.log", "build/reports/coverage.txt", "rtl/build/sim.out"])
async def test_explicit_build_log_is_reviewable_without_entering_source_inventory(tmp_path, name):
    log = tmp_path / name
    log.parent.mkdir(parents=True)
    log.write_text("simulation detail\n" * 5000 + "SIMULATION_PASS\nRC=0\n")
    (log.parent / "unreferenced.log").write_text("not cited\n")

    assert name not in quality.file_state(tmp_path)
    assert name not in quality.workspace_listing(tmp_path)
    references = quality.mentioned_files(tmp_path, f"Check `{name}` and `{log}`.")
    assert references == [name]
    assert "SIMULATION_PASS\nRC=0" in quality.change_evidence(tmp_path, references, referenced=references)
    assert "SIMULATION_PASS" not in quality.change_evidence(tmp_path, [name])

    ws = agent.Workspace(tmp_path, read_only=True, readable_files=references)
    assert "SIMULATION_PASS" in await ws.call("Read", {"file_path": name, "offset": -2})
    assert (await ws.call("Read", {"file_path": str(log.parent / "unreferenced.log")})).startswith("Error:")
    for tool, args in [
        ("Write", {"file_path": name, "content": "changed"}),
        ("Edit", {"file_path": name, "old_string": "RC=0", "new_string": "RC=1"}),
    ]:
        assert "read-only" in await ws.call(tool, args)
    if agent.sandbox_available():
        assert "read-only" in (await ws.call("Bash", {"command": f"echo RC=1 > {name}"})).lower()
        # A check is rerun on a copy in its own /tmp.
        copied = await ws.call("Bash", {"command": f"mkdir -p /tmp/check && cp {name} /tmp/check/ && "
                                                   "echo RC=1 >> /tmp/check/*.log && tail -1 /tmp/check/*.log"})
        assert copied.strip() == "RC=1"
    assert log.read_text().endswith("SIMULATION_PASS\nRC=0\n")


async def test_explicit_build_logs_cannot_bypass_private_paths_or_symlink_targets(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    blocked = [
        "build/.env.log", "build/credentials.txt", "build/secrets.log", "build/server.pem",
        "build/.aws/check.log", "build/.git/check.log", "build/.super-partner/check.log",
        "build/node_modules/check.log", "build/.venv/check.log", "build/.review/check.log",
        "build/program.py", "build/simulation", "build/summary.json", "dist/check.log",
    ]
    for name in blocked:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private-value\n")
    outside = tmp_path / "outside.log"
    outside.write_text("outside-value\n")
    (root / "build/outside.log").symlink_to(outside)
    (root / "build/alias.log").symlink_to(root / "build/credentials.txt")
    (root / "build/private").symlink_to(root / "build/.aws", target_is_directory=True)
    blocked += ["build/outside.log", "build/alias.log", "build/private/check.log", "../outside.log"]

    assert quality.mentioned_files(root, " ".join(blocked)) == []
    evidence = quality.change_evidence(root, blocked, referenced=blocked)
    assert "private-value" not in evidence and "outside-value" not in evidence
    ws = agent.Workspace(root, read_only=True, readable_files=blocked)
    for name in blocked:
        assert (await ws.call("Read", {"file_path": name})).startswith("Error:"), name


@pytest.mark.parametrize("passed", [True, False])
async def test_review_receives_build_log_and_keeps_simulation_failure(tmp_path, monkeypatch, passed):
    monkeypatch.setattr(settings, "review_nodes", True)
    project = tmp_path / "project"
    log = project / "build/tb/compile.log"
    log.parent.mkdir(parents=True)
    log.write_text("SIMULATION_PASS\nRC=0\n" if passed else "SIMULATION_FAIL: assertion failed\nRC=1\n")
    task = TaskNode(id="work", title="Work", instructions="Run simulation", capability="coding",
                    model_id="openai:worker")
    other = TaskNode(id="other", title="Other", instructions="Preserve revised coverage plan",
                     capability="coding", model_id="openai:worker", status=NodeStatus.SUCCEEDED,
                     result="revised coverage model")
    run = ExecutionRun(id="build-review", prompt="simulate", status=RunStatus.RUNNING, budget_usd=10,
                       use_cli=False, project_dir=str(project), plan=TaskPlan(summary="simulate", nodes=[task, other]))
    reviewer = ModelProfile(id="openai:reviewer", provider="openai", display_name="Reviewer",
                            context_window=1_000_000, input_per_million=1, output_per_million=1,
                            scores=CapabilityScore(coding=99, reliability=99))

    class Providers:
        def configured_models(self, ids):
            return set(ids)

        async def complete(self, model_id, prompt, workspace=None, **kwargs):
            assert log.read_text() in prompt
            assert kwargs["review_workspace"] == project
            assert kwargs["review_files"] == ["build/tb/compile.log"]
            ws = agent.Workspace(project, read_only=True, readable_files=kwargs["review_files"])
            output = await ws.call("Read", {"file_path": "build/tb/compile.log"})
            assert "RC=" in output
            return ProviderResult(json.dumps({"passed": "RC=0" in output, "reason": output}), 10, 10, model_id)

    database = Database(tmp_path / "test.db")
    try:
        database.save_run(run)
        executor = Executor(database, Providers(), {reviewer.id: reviewer})
        # A worker's PASS claim must not replace the log's actual failure in the evidence.
        result = ProviderResult("Simulation passed; see build/tb/compile.log", 0, 0)
        verdict = await executor._check(run, task, result.text, [], project)
        assert verdict.passed is passed
        await executor._finish(run.id, task.id, result, verdict, [], [], 0, None)
        saved = database.get_run(run.id)
        assert saved.plan.nodes[0].attempts[-1].outcome == ("passed" if passed else "rejected")
        assert saved.plan.nodes[1] == other
        assert saved.rework_rounds == 0
    finally:
        database.connection.close()
@pytest.mark.parametrize("content", [None, "", "old report"])
async def test_artifact_missing_empty_or_unchanged_report_cannot_pass_on_a_claim(tmp_path, monkeypatch, content):
    from artifact_helpers import contract_node, mock_acceptance, setup_executor
    from super_partner import contracts
    from super_partner.models import RunStatus

    async def claim_only(*args):
        pass

    node = contract_node("write", None, output="report.txt", contract={"max_turns": 1})
    executor, db, providers = setup_executor(tmp_path, monkeypatch, [node], claim_only)
    root = contracts.workspace(db.get_run("artifacts"), node)
    root.mkdir(parents=True)
    if content is not None:
        (root / "report.txt").write_text(content)
    checks = []
    mock_acceptance(monkeypatch, checks)
    await executor.execute("artifacts")
    run = db.get_run("artifacts")
    assert run.status == RunStatus.INCOMPLETE and len(providers.calls) == 1
    assert not run.plan.nodes[0].review_passed
    assert checks == []


async def test_artifact_failed_acceptance_preserves_command_log_and_blocks_audit(tmp_path, monkeypatch):
    from artifact_helpers import mock_acceptance, setup_executor
    from super_partner.models import NodeStatus

    mock_acceptance(monkeypatch, ok=False)
    executor, db, providers = setup_executor(tmp_path, monkeypatch)
    run = db.get_run("artifacts")
    run.plan.nodes[0].artifact_contract.max_turns = 1
    db.save_run(run)
    await executor.execute("artifacts")
    run = db.get_run("artifacts")
    assert providers.calls == ["vdb"]
    assert all(node.status == NodeStatus.BLOCKED for node in run.plan.nodes)
    assert run.plan.nodes[0].artifact_evidence.checks[0]["ok"] is False
    assert "artifact check failed" in run.plan.nodes[0].attempts[0].tool_calls[-1].output
    assert run.plan.nodes[0].attempts[0].trace


async def test_artifact_accepted_report_becomes_stale_when_upstream_vdb_changes(tmp_path, monkeypatch):
    from artifact_helpers import coverage_nodes, mock_acceptance, setup_executor
    from super_partner import contracts
    from super_partner.models import NodeStatus, RunStatus

    nodes = coverage_nodes()
    executor, db, providers = setup_executor(tmp_path, monkeypatch, nodes[:2])
    mock_acceptance(monkeypatch)
    await executor.execute("artifacts")
    run = db.get_run("artifacts")
    assert run.status == RunStatus.SUCCEEDED
    (contracts.workspace(run, nodes[0]) / "coverage.vdb" / "coverage.db").write_text("changed simulation")
    run.plan.nodes.append(nodes[2])
    run.status = RunStatus.QUEUED
    providers.nodes[nodes[2].id] = nodes[2]
    db.save_run(run)
    await executor.execute("artifacts")
    run = db.get_run("artifacts")
    assert run.plan.nodes[2].status == NodeStatus.BLOCKED
    assert providers.calls == ["vdb", "report"]
    assert "过期" in run.plan.nodes[2].review


@pytest.mark.parametrize("text", ["BLOCKED: missing evidence", "STATUS: BLOCKED", '{"status":"blocked","reason":"missing evidence"}'])
def test_artifact_blocked_claims_do_not_become_passing_reviews(text):
    from super_partner import quality

    assert quality.blocked_claim(text)


async def test_artifact_unfinished_audit_does_not_rework_accepted_report(tmp_path, monkeypatch):
    from artifact_helpers import coverage_nodes, mock_acceptance, setup_executor
    from super_partner.models import NodeStatus, RunStatus

    nodes = coverage_nodes()
    nodes[2].artifact_contract.max_turns = 2
    executor, db, providers = setup_executor(tmp_path, monkeypatch, nodes[:2])
    mock_acceptance(monkeypatch)
    await executor.execute("artifacts")
    run = db.get_run("artifacts")
    assert run.status == RunStatus.SUCCEEDED
    run.plan.nodes.append(nodes[2])
    run.status = RunStatus.QUEUED
    db.save_run(run)

    async def no_output(*args):
        pass

    providers.nodes[nodes[2].id], providers.work = nodes[2], no_output
    await executor.execute("artifacts")
    run = db.get_run("artifacts")
    assert providers.calls == ["vdb", "report", "audit", "audit"]
    assert run.rework_rounds == 0
    assert all(n.status == NodeStatus.SUCCEEDED and len(n.attempts) == 1 for n in run.plan.nodes[:2])
    assert run.plan.nodes[2].attempts[-1].outcome == "stopped"


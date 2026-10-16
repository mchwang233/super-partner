import os

import httpx
import pytest
from super_partner import secrets
from super_partner.catalog import builtin_models
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import CapabilityScore, ModelProfile
from super_partner.quota import QuotaTracker


def scored_models() -> list[ModelProfile]:
    """The built-in catalog with uniform stand-in scores, as if AA had scored every model.

    Routing scores come only from Artificial Analysis, so the bare catalog routes nothing but
    the demo model; tests of routing order use these instead.
    """
    models = builtin_models()
    for model in models:
        if model.scores is None:
            model.scores = CapabilityScore(**{key: 90 for key in CapabilityScore.model_fields})
    return models


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """No test reads the real tools folder, and failures are not diagnosed unless a test of
    diagnosis turns it on: a diagnosis would be one more call to every test's fake models."""
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "auto_diagnose", False)
    monkeypatch.setattr(settings, "platform_self_repair", False)


@pytest.fixture
def network_enabled():
    """Live network checks run only when the validation task permits networking."""
    if os.environ.get("SUPER_PARTNER_VALIDATION_ALLOW_NETWORK") == "false":
        pytest.skip("The task disabled network access for this validation")


@pytest.fixture
async def task_api(tmp_path, monkeypatch):
    # API initialization must never read or seed the user's real database/keyring.
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "database_path", tmp_path / "tasks.db")
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    monkeypatch.setattr(secrets, "hydrate_provider_keys", lambda: None)
    from super_partner import api

    database = Database(settings.db_path)
    monkeypatch.setattr(api.providers, "quota", QuotaTracker(database))
    monkeypatch.setattr(api, "db", database)
    monkeypatch.setattr(api, "executor", Executor(database, api.providers, api.models))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api.app), base_url="http://127.0.0.1"
        ) as client:
            yield api, client
    finally:
        await api.catalog_sync.close()
        await api.providers.client.aclose()
        database.connection.close()


@pytest.fixture
def database(tmp_path, monkeypatch):
    """A one-step task with a lead (test_leading.py, test_replan.py): failures go to the lead."""
    from super_partner.config import settings
    from super_partner.db import Database
    from super_partner.models import ExecutionRun, RunStatus, SupervisorSession, TaskNode, TaskPlan

    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "review_nodes", False)
    monkeypatch.setattr(settings, "auto_diagnose", True)
    monkeypatch.setattr(settings, "lead_decisions", True)
    database = Database(tmp_path / "test.db")
    node = TaskNode(id="a", title="实现", instructions="写代码", capability="coding", model_id="x:api")
    database.save_run(ExecutionRun(
        id="r", prompt="写一个工具", status=RunStatus.QUEUED, budget_usd=1,
        supervisor=SupervisorSession(model_id="cli:claude", session_id="lead-1", cwd=str(tmp_path / "lead")),
        plan=TaskPlan(summary="s", nodes=[node])))
    return database

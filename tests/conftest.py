import httpx
import pytest
from super_partner import secrets
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor


@pytest.fixture
async def task_api(tmp_path, monkeypatch):
    # API initialization must never read or seed the user's real database/keyring.
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "database_path", tmp_path / "tasks.db")
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    monkeypatch.setattr(secrets, "hydrate_provider_keys", lambda: None)
    from super_partner import api

    database = Database(settings.db_path)
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

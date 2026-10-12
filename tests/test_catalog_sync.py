import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from super_partner.catalog_sync import (
    OFFICIAL_URLS,
    PUBLIC_CATALOG_URL,
    PUBLIC_PROVIDERS,
    CatalogSync,
    public_models,
    seed_catalog,
)
from super_partner.db import Database
from super_partner.models import ModelProfile, TaskNode, utcnow
from super_partner.planner import select_planner_model
from super_partner.routing import NoEligibleModelError, node_cost, route_node
from super_partner.secrets import PROVIDER_ENV


@pytest.fixture
def catalog():
    return {
        source: {
            "models": {
                "new-model": {
                    "id": "new-model",
                    "name": "New Model",
                    "release_date": "2026-10-01",
                    "modalities": {"input": ["text", "image"], "output": ["text"]},
                    "limit": {"context": 100000},
                    "cost": {"input": 1.25, "output": 5},
                    "tool_call": True,
                    "reasoning": True,
                }
            }
        }
        for source in PUBLIC_PROVIDERS.values()
    }


@pytest.fixture
def sync(tmp_path, monkeypatch):
    for env in PROVIDER_ENV.values():
        monkeypatch.delenv(env, raising=False)
    db = Database(tmp_path / "catalog.db")
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    return CatalogSync(db, seed_catalog(db), client=client)


async def test_public_sync_persists_all_providers_without_keys(sync, catalog):
    def respond(request):
        assert str(request.url) == PUBLIC_CATALOG_URL
        assert "authorization" not in request.headers and "x-api-key" not in request.headers
        return httpx.Response(200, json=catalog)

    sync.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        status = await sync.refresh()
        assert status["added"] == 9
        assert all(s["public_status"] == "ok" for s in status["providers"].values())
        new = sync.models["openai:new-model"]
        assert new.input_per_million == 1.25 and new.supports_vision is True
        assert new.scores is None and not new.enabled and not new.routing_ready
        assert str(new.release_date) == "2026-10-01"
        assert new.catalog_source == "models.dev" and not new.pricing_verified
        assert new.available_via_api is None
        assert (await sync.refresh())["added"] == 0
        assert seed_catalog(sync.db)[new.id] == new.model_copy(
            update={
                "catalog_checked_at": sync.models[new.id].catalog_checked_at,
            }
        )
        assert sync.db.load_catalog_state()["last_success_at"]
    finally:
        await sync.close()


async def test_sync_preserves_enabled_scores_verified_prices_and_history(sync, catalog):
    original = sync.models["openai:gpt-5.6-sol"].model_copy(
        update={
            "enabled": False,
            "pricing_verified": True,
            "input_per_million": 42,
        }
    )
    sync.models[original.id] = original
    catalog["openai"]["models"]["gpt-5.6-sol"] = {
        **catalog["openai"]["models"]["new-model"],
        "id": "gpt-5.6-sol",
    }
    sync.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=catalog))
    )
    try:
        await sync.refresh()
        updated = sync.models[original.id]
        assert not updated.enabled and updated.scores == original.scores
        assert updated.input_per_million == 42 and updated.pricing_verified
        assert updated.context_window == 100000
        assert seed_catalog(sync.db)[original.id] == updated
        old_id = "openai:new-model"
        del catalog["openai"]["models"]["new-model"]
        await sync.refresh()
        assert old_id in sync.models
        assert sync.models[old_id].catalog_status == "unlisted"
        assert "cli:codex" in sync.models and "mock:local" in sync.models
    finally:
        await sync.close()


async def test_failures_keep_cache_isolate_providers_and_hide_secrets(sync, catalog, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-test-key")
    catalog["google"] = {"models": {}}

    def respond(request):
        if request.url.host == "api.openai.com":
            return httpx.Response(401, text="secret-test-key")
        assert "authorization" not in request.headers
        return httpx.Response(200, json=catalog)

    sync.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        status = await sync.refresh()
        assert status["providers"]["google"]["public_status"] == "error"
        assert status["providers"]["openai"]["official_status"] == "error"
        assert status["providers"]["anthropic"]["public_status"] == "ok"
        assert "secret-test-key" not in json.dumps(status)
        cached = {key: model.model_dump() for key, model in sync.models.items()}
        await sync.client.aclose()
        sync.client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(503))
        )
        status = await sync.refresh()
        assert all(s["public_status"] == "error" for s in status["providers"].values())
        assert cached == {key: model.model_dump() for key, model in sync.models.items()}
        assert status["providers"]["anthropic"]["last_success_at"]
    finally:
        await sync.close()


@pytest.mark.parametrize("provider", ["anthropic", "google"])
async def test_official_pagination_and_auth(sync, catalog, monkeypatch, provider):
    monkeypatch.setenv(PROVIDER_ENV[provider], "private-key")
    pages = []

    def respond(request):
        if str(request.url) == PUBLIC_CATALOG_URL:
            assert "authorization" not in request.headers and "x-api-key" not in request.headers
            return httpx.Response(200, json=catalog)
        assert str(request.url).startswith(OFFICIAL_URLS[provider])
        pages.append(request)
        second = len(pages) == 2
        if provider == "google":
            assert request.headers["x-goog-api-key"] == "private-key"
            if second:
                assert request.url.params["pageToken"] == "page-two"
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": f"models/official-{len(pages)}",
                            "displayName": "Fresh",
                            "inputTokenLimit": 120000,
                            "supportedGenerationMethods": ["generateContent"],
                        }
                    ],
                    **({} if second else {"nextPageToken": "page-two"}),
                },
            )
        assert request.headers["x-api-key"] == "private-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        if second:
            assert request.url.params["after_id"] == "page-two"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": f"official-{len(pages)}",
                        "display_name": "Fresh",
                        "max_input_tokens": 120000,
                    }
                ],
                "has_more": not second,
                "last_id": "page-two",
            },
        )

    sync.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        status = await sync.refresh()
        assert len(pages) == 2 and status["providers"][provider]["official_count"] == 2
        for index in (1, 2):
            model = sync.models[f"{provider}:official-{index}"]
            assert model.catalog_source == "official" and model.available_via_api is True
            assert model.context_window == 120000 and model.scores is None
            assert model.input_per_million is None and model.release_date is None
        assert sync.models[f"{provider}:new-model"].available_via_api is False
    finally:
        await sync.close()


async def test_failed_pagination_never_commits_partial_official_catalog(sync, catalog, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-key")

    def respond(request):
        if request.url.host == "models.dev":
            return httpx.Response(200, json=catalog)
        if "after_id" in request.url.params:
            return httpx.Response(429)
        return httpx.Response(
            200, json={"data": [{"id": "partial"}], "has_more": True, "last_id": "partial"}
        )

    sync.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        status = await sync.refresh()
        assert status["providers"]["anthropic"]["official_status"] == "error"
        assert "anthropic:partial" not in sync.models
        assert sync.models["anthropic:new-model"].available_via_api is None
    finally:
        await sync.close()


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
async def test_official_bearer_auth_and_missing_metadata(sync, catalog, monkeypatch, provider):
    monkeypatch.setenv(PROVIDER_ENV[provider], "private-key")

    def respond(request):
        if request.url.host == "models.dev":
            return httpx.Response(200, json=catalog)
        assert str(request.url) == OFFICIAL_URLS[provider]
        assert request.headers["authorization"] == "Bearer private-key"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "new-model"},
                    {"id": "fresh-model", "created": 1788000000},
                    {"id": "retired-model", "shutdown_date": "2020-01-01"},
                ]
            },
        )

    sync.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        result = await sync.refresh()
        assert result["providers"][provider]["official_status"] == "ok"
        assert sync.models[f"{provider}:new-model"].input_per_million == 1.25
        fresh = sync.models[f"{provider}:fresh-model"]
        assert fresh.release_date is None and fresh.input_per_million is None
        assert fresh.available_via_api is True and not fresh.routing_ready
        assert sync.models[f"{provider}:retired-model"].catalog_status == "retired"
    finally:
        await sync.close()


async def test_retired_model_is_not_revived_by_missing_or_stale_public_data(sync, catalog):
    original = sync.models["openai:gpt-5.6-sol"]
    entry = {**catalog["openai"]["models"]["new-model"], "id": "gpt-5.6-sol", "status": "retired"}
    catalog["openai"]["models"]["gpt-5.6-sol"] = entry
    sync.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=catalog))
    )
    try:
        await sync.refresh()
        assert not sync.models[original.id].routing_ready
        del entry["status"]
        await sync.refresh()
        assert sync.models[original.id].catalog_status == "retired"
        del catalog["openai"]["models"]["gpt-5.6-sol"]
        await sync.refresh()
        assert sync.models[original.id].catalog_status == "retired"
        assert not sync.models[original.id].routing_ready
    finally:
        await sync.close()


def test_unknown_metadata_is_not_free_or_a_routing_candidate(catalog):
    entry = catalog["openai"]["models"]["new-model"]
    entry.pop("cost")
    entry["limit"]["context"] = -1
    metadata = public_models(catalog, "openai")["openai:new-model"]
    model = ModelProfile(id="openai:new-model", provider="openai", enabled=True, **metadata)
    assert model.context_window is None and model.input_per_million is None
    node = TaskNode(
        id="n", title="task", instructions="work", capability="coding", quality_threshold=0
    )
    assert select_planner_model([model], {model.id}) is None
    with pytest.raises(NoEligibleModelError):
        route_node(node, [model], {model.id})
    with pytest.raises(NoEligibleModelError):
        node_cost(node, model)


async def test_concurrent_refresh_deduplicates_and_shutdown_cancels(sync):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def respond(request):
        calls.append(request)
        started.set()
        await release.wait()
        return httpx.Response(503)

    sync.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    sync.trigger()
    await asyncio.wait_for(started.wait(), timeout=1)
    sync.trigger()
    assert (await sync.refresh())["running"]
    assert len(calls) == 1
    await sync.close()
    assert sync.task.cancelled() and sync.client.is_closed


async def test_startup_uses_persisted_schedule_and_runs_when_due(sync, catalog):
    calls = []
    sync.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(200, json=catalog),
        )
    )
    sync.state["next_sync_at"] = (utcnow() + timedelta(hours=12)).isoformat()
    sync.start()
    await asyncio.sleep(0)
    assert not calls
    sync.scheduler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sync.scheduler
    sync.state["next_sync_at"] = (utcnow() - timedelta(hours=1)).isoformat()
    sync.start()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await sync.task
    assert len(calls) == 1
    assert (utcnow() + timedelta(hours=23)).isoformat() < sync.status()["next_sync_at"]
    await sync.close()

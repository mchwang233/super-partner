"""Refresh model metadata without inventing quality scores or making inference calls."""

from __future__ import annotations

import asyncio
import math
import os
import re
from contextlib import suppress
from datetime import date, datetime, timedelta
from typing import Optional

import httpx

from .catalog import builtin_models
from .db import Database
from .models import ModelProfile, utcnow
from .scoring import rescore
from .secrets import PROVIDER_ENV

PUBLIC_CATALOG_URL = "https://models.dev/api.json"
# Match the regions used by providers.py; never take credential destinations from remote data.
PUBLIC_PROVIDERS = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "google",
    "deepseek": "deepseek",
    "qwen": "alibaba-cn",
    "doubao": "volcengine",
    "glm": "zhipuai",
    "minimax": "minimax",
    "kimi": "moonshotai-cn",
}
OFFICIAL_URLS = {
    "openai": "https://api.openai.com/v1/models",
    "anthropic": "https://api.anthropic.com/v1/models",
    "google": "https://generativelanguage.googleapis.com/v1beta/models",
    "deepseek": "https://api.deepseek.com/models",
}


def seed_catalog(db: Database) -> dict[str, ModelProfile]:
    """Seed only missing entries. Restarting must preserve discoveries and user choices."""
    models = {model.id: model for model in db.list_models()}
    missing = [model for model in builtin_models() if model.id not in models]
    db.save_models(missing)
    models.update({model.id: model for model in missing})
    return models


def number(value, *, positive: bool = False):
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and (value > 0 if positive else value >= 0)
    ):
        return value
    return None


def iso_date(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def model_id(provider: str, value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[\w][\w./:@-]{0,199}", value):
        raise ValueError("Invalid model ID")
    return f"{provider}:{value}"


def public_models(payload: dict, provider: str) -> dict[str, dict]:
    group = payload.get(PUBLIC_PROVIDERS[provider])
    entries = group.get("models") if isinstance(group, dict) else None
    if not isinstance(entries, dict) or not entries:
        raise ValueError("Missing provider catalog")
    result = {}
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            raise TypeError("Invalid model metadata")
        modalities = entry.get("modalities", {})
        # This application executes text tasks; specialized image/audio/embedding models are omitted.
        if not isinstance(modalities, dict) or "text" not in modalities.get("output", []):
            continue
        identifier = model_id(provider, entry.get("id", key))
        cost, limit = entry.get("cost") or {}, entry.get("limit") or {}
        if not isinstance(cost, dict) or not isinstance(limit, dict):
            raise TypeError("Invalid model limits or prices")
        context = number(limit.get("context"), positive=True)
        result[identifier] = {
            "display_name": str(entry.get("name") or key)[:200],
            "description": str(entry.get("description") or "")[:2000] or None,
            "context_window": int(context) if context else None,
            "input_per_million": number(cost.get("input")),
            "output_per_million": number(cost.get("output")),
            "cached_input_per_million": number(cost.get("cache_read")),
            "supports_tools": entry.get("tool_call")
            if isinstance(entry.get("tool_call"), bool)
            else None,
            "supports_vision": "image" in modalities["input"]
            if isinstance(modalities.get("input"), list)
            else None,
            "supports_reasoning": entry.get("reasoning")
            if isinstance(entry.get("reasoning"), bool)
            else None,
            "supports_structured_output": entry.get("structured_output")
            if isinstance(entry.get("structured_output"), bool)
            else None,
            "release_date": iso_date(entry.get("release_date")),
            "catalog_status": entry.get("status")
            if entry.get("status") in {"deprecated", "retired"}
            else "listed",
            "catalog_source": "models.dev",
            "pricing_source": "models.dev",
            "pricing_verified": False,
            "source_url": f"https://models.dev/?search={PUBLIC_PROVIDERS[provider]}",
        }
    if not result:
        raise ValueError("Empty text model catalog")
    return result


def error_message(exc: Exception) -> str:
    # Never expose URLs with query credentials, response bodies, or exception messages.
    if isinstance(exc, httpx.HTTPStatusError):
        return f"请求失败（HTTP {exc.response.status_code}），保留上次目录"
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
        return "请求超时，保留上次目录"
    if isinstance(exc, httpx.TransportError):
        return "网络连接失败，保留上次目录"
    return "目录格式不兼容，保留上次目录"


class CatalogSync:
    def __init__(
        self,
        db: Database,
        models: dict[str, ModelProfile],
        *,
        auto_sync: bool = True,
        interval_hours: float = 24,
        client: Optional[httpx.AsyncClient] = None,
        benchmarks=None,
    ):
        self.db, self.models = db, models
        self.benchmarks = benchmarks
        self.auto_sync, self.interval_hours = auto_sync, interval_hours
        self.client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)
        self.state = db.load_catalog_state()
        self.lock = asyncio.Lock()
        self.task: Optional[asyncio.Task] = None
        self.scheduler: Optional[asyncio.Task] = None

    def status(self) -> dict:
        return {
            **self.state,
            "running": self.lock.locked() or bool(self.task and not self.task.done()),
            "auto_sync": self.auto_sync,
            "interval_hours": self.interval_hours,
            "next_sync_at": self.state.get("next_sync_at") if self.auto_sync else None,
            "benchmarks": self.benchmarks.status(list(self.models.values())) if self.benchmarks else None,
        }

    def trigger(self) -> None:
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self.refresh())

    def start(self) -> None:
        if self.auto_sync:
            self.scheduler = asyncio.create_task(self._schedule())

    async def close(self) -> None:
        for task in (self.scheduler, self.task):
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        await self.client.aclose()

    async def _schedule(self) -> None:
        while True:
            due = self.state.get("next_sync_at")
            if (
                not due or utcnow() >= datetime.fromisoformat(due)
                or (self.benchmarks and not self.benchmarks.cache)
                or (
                    self.benchmarks
                    and self.benchmarks.cache.get("artificial_analysis", {}).get("status") == "no_key"
                )
            ):
                self.trigger()
                await self.task
            await asyncio.sleep(30)

    async def _get(self, url: str, **kwargs) -> dict:
        response = await self.client.get(url, **kwargs)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError("Invalid catalog")
        return payload

    async def _official_models(self, provider: str, key: str) -> dict[str, dict]:
        headers = {"Authorization": f"Bearer {key}"}
        params = {}
        if provider == "anthropic":
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
            params = {"limit": 1000}
        elif provider == "google":
            headers = {"x-goog-api-key": key}
            params = {"pageSize": 1000}
        result, cursors = {}, set()
        for _ in range(100):
            payload = await self._get(OFFICIAL_URLS[provider], headers=headers, params=params)
            entries = payload.get("models" if provider == "google" else "data")
            if not isinstance(entries, list):
                raise TypeError("Missing models")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise TypeError("Invalid model")
                if provider == "google":
                    if "generateContent" not in entry.get("supportedGenerationMethods", []):
                        continue
                    raw_id = str(entry.get("name", "")).removeprefix("models/")
                else:
                    raw_id = entry.get("id")
                identifier = model_id(provider, raw_id)
                data = {"available_via_api": True}
                name = entry.get("display_name") or entry.get("displayName") or entry.get("name")
                if name:
                    data["display_name"] = str(name)[:200]
                context = number(
                    entry.get("inputTokenLimit")
                    or entry.get("max_input_tokens")
                    or entry.get("context_window"),
                    positive=True,
                )
                if context:
                    data["context_window"] = int(context)
                # Creation timestamps are not release dates. Do not present them as such.
                if entry.get("lifecycle") in {"deprecated", "retired"}:
                    data["catalog_status"] = entry["lifecycle"]
                shutdown = iso_date(entry.get("shutdown_date"))
                if shutdown and shutdown <= utcnow().date().isoformat():
                    data["catalog_status"] = "retired"
                result[identifier] = data
            cursor = (
                payload.get("nextPageToken")
                if provider == "google"
                else (payload.get("last_id") if payload.get("has_more") else None)
            )
            if not cursor:
                if payload.get("has_more"):
                    raise ValueError("Missing pagination cursor")
                if not result:
                    raise ValueError("Empty model catalog")
                return result
            if cursor in cursors:
                raise ValueError("Repeated pagination cursor")
            cursors.add(cursor)
            params["pageToken" if provider == "google" else "after_id"] = cursor
        raise ValueError("Catalog pagination limit exceeded")

    def _merge(self, provider: str, entries: dict[str, dict], *, official: bool) -> int:
        now = utcnow()
        changed = []
        added = 0
        for identifier, metadata in entries.items():
            existing = self.models.get(identifier)
            data = (
                existing.model_dump()
                if existing
                else {
                    "id": identifier,
                    "provider": provider,
                    "display_name": identifier.split(":", 1)[1],
                    "enabled": False,
                    "catalog_source": "official" if official else "models.dev",
                    "source_url": OFFICIAL_URLS.get(provider) if official else PUBLIC_CATALOG_URL,
                    "pricing_source": "unknown",
                }
            )
            if not existing:
                added += 1
            # An incomplete remote field must not erase previously known metadata.
            values = {key: value for key, value in metadata.items() if value is not None}
            # A stale public snapshot or a missing entry cannot revive an explicitly retired model.
            if existing and existing.catalog_status == "retired":
                values["catalog_status"] = "retired"
            if not official and (
                metadata.get("input_per_million") is None
                or metadata.get("output_per_million") is None
            ):
                # Preserve an existing complete price pair and its provenance together.
                if existing:
                    for key in (
                        "input_per_million",
                        "output_per_million",
                        "cached_input_per_million",
                        "pricing_source",
                        "pricing_verified",
                    ):
                        values.pop(key, None)
                elif (
                    metadata.get("input_per_million") is None
                    and metadata.get("output_per_million") is None
                ):
                    values["pricing_source"] = "unknown"
            if existing and existing.pricing_verified:
                for key in (
                    "input_per_million",
                    "output_per_million",
                    "cached_input_per_million",
                    "pricing_source",
                    "pricing_verified",
                ):
                    values.pop(key, None)
            data.update(values)
            data["api_checked_at" if official else "catalog_checked_at"] = now
            if official and (not existing or existing.catalog_source == "official"):
                data["catalog_checked_at"] = now
            changed.append(ModelProfile.model_validate(data))
        for model in list(self.models.values()):
            if model.provider != provider or model.channel != "api" or model.id in entries:
                continue
            if official:
                changed.append(
                    model.model_copy(update={"available_via_api": False, "api_checked_at": now})
                )
            elif model.catalog_source == "models.dev":
                changed.append(
                    model.model_copy(
                        update={
                            "catalog_status": model.catalog_status
                            if model.catalog_status in {"deprecated", "retired"}
                            else "unlisted",
                            "catalog_checked_at": now,
                        }
                    )
                )
        # No await between reading preferences, persisting, and replacing the live objects.
        self.db.save_models(changed)
        self.models.update({model.id: model for model in changed})
        return added

    async def refresh(self) -> dict:
        if self.lock.locked():
            return self.status()
        async with self.lock:
            started = utcnow()
            self.state["last_attempt_at"] = started.isoformat()
            states = self.state.setdefault("providers", {})
            try:
                public = await asyncio.wait_for(self._get(PUBLIC_CATALOG_URL), timeout=35)
                public_error = None
            except Exception as exc:
                public, public_error = {}, error_message(exc)

            async def sync_provider(provider: str):
                previous = states.get(provider, {})
                state = {**previous, "checked_at": utcnow().isoformat(), "added": 0}
                try:
                    if public_error:
                        raise ValueError("Public catalog unavailable")
                    entries = public_models(public, provider)
                    state["added"] += self._merge(provider, entries, official=False)
                    state.update(
                        public_status="ok",
                        public_error=None,
                        count=len(entries),
                        last_success_at=utcnow().isoformat(),
                    )
                except Exception as exc:
                    state.update(
                        public_status="error", public_error=public_error or error_message(exc)
                    )
                if provider not in OFFICIAL_URLS:
                    state.update(official_status="not_supported", official_error=None)
                elif not os.getenv(PROVIDER_ENV[provider]):
                    state.update(official_status="no_key", official_error=None)
                else:
                    try:
                        entries = await asyncio.wait_for(
                            self._official_models(provider, os.environ[PROVIDER_ENV[provider]]),
                            timeout=45,
                        )
                        state["added"] += self._merge(provider, entries, official=True)
                        state.update(
                            official_status="ok",
                            official_error=None,
                            official_count=len(entries),
                            official_success_at=utcnow().isoformat(),
                        )
                    except Exception as exc:
                        state.update(official_status="error", official_error=error_message(exc))
                states[provider] = state

            jobs = [sync_provider(provider) for provider in PUBLIC_PROVIDERS]
            if self.benchmarks:
                jobs.append(self.benchmarks.refresh(self.client))
            await asyncio.gather(*jobs)
            if self.benchmarks:
                # New AA results or newly listed models can change routing scores.
                rescore(self.models, self.benchmarks, self.db)
            finished = utcnow()
            all_ok = all(state["public_status"] == "ok" for state in states.values())
            if self.benchmarks:
                all_ok = all_ok and all(
                    entry.get("status") != "error" for entry in self.benchmarks.cache.values()
                )
            any_ok = any(
                state["public_status"] == "ok" or state["official_status"] == "ok"
                for state in states.values()
            )
            if any_ok:
                self.state["last_success_at"] = finished.isoformat()
            # Failed sources retry after an hour; successful refreshes use the configured interval.
            interval = (
                self.interval_hours
                if all_ok and all(s["official_status"] != "error" for s in states.values())
                else min(1, self.interval_hours)
            )
            self.state.update(
                last_finished_at=finished.isoformat(),
                next_sync_at=(finished + timedelta(hours=interval)).isoformat(),
                added=sum(state["added"] for state in states.values()),
            )
            self.db.save_catalog_state(self.state)
        return self.status()

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .catalog import builtin_models
from .config import settings
from .db import Database
from .executor import Executor
from .models import Event, ExecutionRun, NodeStatus, RunStatus
from .planner import autonomous_plan
from .projects import ProjectDirError, project_context, resolve_project_dir
from .providers import ProviderError, ProviderRegistry
from .routing import NoEligibleModelError, eligible_for_node, estimate, route_node
from .secrets import (
    PROVIDER_ENV,
    configured_providers,
    delete_provider_key,
    hydrate_provider_keys,
    set_provider_key,
)


class CreateTask(BaseModel):
    prompt: str = Field(min_length=3)
    budget_usd: float = Field(default=2, gt=0, le=10_000)
    use_cli: bool = True
    project_dir: Optional[str] = Field(default=None, max_length=4096)


class ConfirmTask(BaseModel):
    budget_usd: Optional[float] = Field(default=None, gt=0, le=10_000)


class ProviderKey(BaseModel):
    api_key: str = Field(min_length=8)


class UpdateModel(BaseModel):
    enabled: bool


class UpdateModelById(UpdateModel):
    model_id: str


settings.prepare()
db = Database(settings.db_path)
existing_enabled = {model.id: model.enabled for model in db.list_models()}
seed_models = builtin_models()
for seed_model in seed_models:
    if seed_model.id in existing_enabled:
        seed_model.enabled = existing_enabled[seed_model.id]
db.save_models(seed_models)
db.delete_models_except(model.id for model in seed_models)
hydrate_provider_keys()
providers = ProviderRegistry()
models = {model.id: model for model in db.list_models()}
executor = Executor(db, providers, models)


@asynccontextmanager
async def lifespan(_: FastAPI):
    for run in db.list_runs():
        if run.status in {RunStatus.QUEUED, RunStatus.RUNNING}:
            run.status = RunStatus.QUEUED
            db.save_run(run)
            executor.start(run.id)
    yield
    await providers.client.aclose()


app = FastAPI(title="Super Partner", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)


@app.get("/health")
def health():
    return {
        "status": "ok", "configured_providers": configured_providers() + ["mock"],
        "available_cli_channels": sorted(providers.cli.available_model_ids()),
    }


@app.get("/api/channels/cli")
def list_cli_channels(refresh: bool = False):
    statuses = providers.cli.refresh() if refresh else providers.cli.statuses
    return [status.public() for status in statuses.values()]


@app.get("/api/models")
def list_models():
    configured = providers.configured_models(list(models))
    return [
        {
            **model.model_dump(), "configured": model.id in configured,
            "auth_type": (
                providers.cli.statuses[model.id].auth_type if model.channel == "cli" else "api_key"
            ),
        }
        for model in models.values()
    ]


@app.patch("/api/models/{model_id}")
def update_model(model_id: str, body: UpdateModel):
    model = models.get(model_id)
    if not model:
        raise HTTPException(404, "Model not found")
    model.enabled = body.enabled
    db.save_models([model])
    return {"id": model.id, "enabled": model.enabled}


@app.patch("/api/models")
def update_model_by_id(body: UpdateModelById):
    return update_model(body.model_id, body)


@app.put("/api/providers/{provider}/key", status_code=204)
def store_provider_key(provider: str, body: ProviderKey):
    try:
        set_provider_key(provider, body.api_key)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/providers")
def list_providers():
    configured = set(configured_providers())
    labels = {
        "openai": "OpenAI", "anthropic": "Anthropic Claude", "google": "Google Gemini",
        "deepseek": "DeepSeek", "qwen": "通义千问", "doubao": "豆包",
        "glm": "智谱 GLM", "minimax": "MiniMax", "kimi": "Kimi（月之暗面）",
    }
    return [
        {"id": provider, "name": labels[provider], "configured": provider in configured}
        for provider in PROVIDER_ENV
    ]


@app.delete("/api/providers/{provider}/key", status_code=204)
def remove_provider_key(provider: str):
    try:
        delete_provider_key(provider)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/providers/{provider}/test")
async def test_provider(provider: str):
    try:
        message = await providers.test_provider(provider)
        return {"ok": True, "message": message}
    except ProviderError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/tasks", response_model=ExecutionRun, status_code=201)
async def create_task(body: CreateTask):
    configured = providers.configured_models(list(models))
    if not body.use_cli:
        configured = {model_id for model_id in configured if not model_id.startswith("cli:")}
    project_dir = None
    if body.project_dir and body.project_dir.strip():
        try:
            project_dir = resolve_project_dir(body.project_dir.strip())
        except ProjectDirError as exc:
            raise HTTPException(400, str(exc)) from exc
    plan = await autonomous_plan(
        body.prompt, list(models.values()), configured, providers,
        project_context=await asyncio.to_thread(project_context, project_dir) if project_dir else None,
    )
    try:
        for node in plan.nodes:
            available = eligible_for_node(node, list(models.values()), configured)
            if available:
                best_score = max(
                    model.scores.score_for(node.capability) for model in available
                )
                if node.quality_threshold > best_score:
                    original = node.quality_threshold
                    node.quality_threshold = best_score
                    adjustment = (
                        f"Adjusted {node.title} {node.capability} threshold "
                        f"from {original:.2f} to {best_score:.2f} for available models"
                    )
                    plan.planning_note = (
                        f"{plan.planning_note}; {adjustment}" if plan.planning_note else adjustment
                    )
            route_node(node, list(models.values()), configured)
    except NoEligibleModelError as exc:
        raise HTTPException(422, str(exc)) from exc
    plan.estimate = estimate(plan.nodes, body.budget_usd, plan.planning_cost_usd)
    run = ExecutionRun(
        id=str(uuid4()), prompt=body.prompt, status=RunStatus.AWAITING_CONFIRMATION,
        plan=plan, budget_usd=body.budget_usd, use_cli=body.use_cli,
        project_dir=str(project_dir) if project_dir else None,
        actual_cost_usd=plan.planning_cost_usd,
    )
    db.save_run(run)
    db.add_event(Event(
        run_id=run.id, kind="plan.created",
        data={
            "estimate": plan.estimate.model_dump(), "use_cli": body.use_cli,
            "project_dir": run.project_dir,
            "planning_method": plan.planning_method,
            "planner_model_id": plan.planner_model_id,
            "planning_note": plan.planning_note,
        },
    ))
    return run


@app.get("/api/tasks", response_model=list[ExecutionRun])
def list_tasks():
    return db.list_runs()


@app.get("/api/tasks/{run_id}", response_model=ExecutionRun)
def get_task(run_id: str):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Task not found")
    return run


@app.post("/api/tasks/{run_id}/confirm", response_model=ExecutionRun)
async def confirm_task(run_id: str, body: ConfirmTask):
    run = get_task(run_id)
    if run.status not in {RunStatus.AWAITING_CONFIRMATION, RunStatus.PAUSED}:
        raise HTTPException(409, f"Cannot confirm task in {run.status} status")
    budget = body.budget_usd if body.budget_usd is not None else run.budget_usd
    required = run.actual_cost_usd + sum(
        node.estimated_cost_usd for node in run.plan.nodes if node.status != NodeStatus.SUCCEEDED
    )
    if required > budget:
        raise HTTPException(
            409, f"Budget ${budget:.2f} is below the ${required:.4f} needed to finish this task",
        )
    run.budget_usd = budget
    if run.plan.estimate:
        run.plan.estimate.hard_limit_usd = budget
    run.status = RunStatus.QUEUED
    db.save_run(run)
    db.add_event(Event(run_id=run.id, kind="run.confirmed", data={"budget_usd": run.budget_usd}))
    executor.start(run.id)
    return run


@app.post("/api/tasks/{run_id}/cancel", response_model=ExecutionRun)
async def cancel_task(run_id: str):
    run = get_task(run_id)
    if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}:
        raise HTTPException(409, "Task has already finished")
    run.status = RunStatus.CANCELLED
    db.save_run(run)
    db.add_event(Event(run_id=run.id, kind="run.cancelled"))
    executor.cancel(run.id)
    return run


@app.get("/api/tasks/{run_id}/events")
async def task_events(run_id: str, after: int = 0):
    if not db.get_run(run_id):
        raise HTTPException(404, "Task not found")

    async def stream():
        cursor = after
        idle = 0
        while idle < 300:
            events = db.events(run_id, cursor)
            if events:
                idle = 0
                for event in events:
                    cursor = event.id or cursor
                    yield f"id: {cursor}\nevent: {event.kind}\ndata: {json.dumps(event.data)}\n\n"
            else:
                idle += 1
                yield ": keepalive\n\n"
            run = db.get_run(run_id)
            if run and run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED} and not events:
                break
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream")

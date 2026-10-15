from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from . import attachments as files
from . import publish
from .benchmarks import BenchmarkCatalog
from .catalog_sync import CatalogSync, seed_catalog
from .config import settings
from .db import Database
from .executor import Executor
from .models import Attachment, Event, ExecutionRun, NodeStatus, RunStatus
from .planner import autonomous_plan
from .projects import (
    NativePickerError,
    ProjectDirError,
    discover_projects,
    inspect_project,
    native_pick_directory,
    native_picker_status,
    project_context,
    resolve_project_dir,
)
from .providers import ProviderError, ProviderRegistry
from .routing import NoEligibleModelError, eligible_for_node, estimate, route_node
from .scoring import AA_SOURCE, rescore
from .secrets import (
    PROVIDER_ENV,
    SECRET_ENV,
    configured_providers,
    delete_provider_key,
    hydrate_provider_keys,
    set_provider_key,
)


class CreateTask(BaseModel):
    prompt: str = Field(min_length=3)
    attachments: list[str] = Field(default_factory=list)
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
models = seed_catalog(db)
hydrate_provider_keys()
providers = ProviderRegistry(db)
executor = Executor(db, providers, models)
benchmarks = BenchmarkCatalog(db)
rescore(models, benchmarks, db)
catalog_sync = CatalogSync(
    db, models, auto_sync=settings.catalog_auto_sync,
    interval_hours=settings.catalog_sync_interval_hours,
    benchmarks=benchmarks,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    catalog_sync.start()
    for run in db.list_runs():
        # An interrupted run may have left a project half-edited; resume only once confirmed.
        if run.status in {RunStatus.QUEUED, RunStatus.RUNNING}:
            run.status = RunStatus.PAUSED
            db.save_run(run)
            db.add_event(Event(run_id=run.id, kind="approval.required", data={"reason": "interrupted"}))
        interrupted = [item for item in run.diagnoses if item.status == "running"]
        for item in interrupted:
            item.status, item.error = "failed", "服务重启打断了诊断。"
        if interrupted:
            db.save_run(run)
    try:
        yield
    finally:
        await executor.shutdown()
        await catalog_sync.close()
        await providers.client.aclose()


app = FastAPI(title="Super Partner", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=settings.allowed_origins,
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _host_name(host: str) -> str:
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


@app.middleware("http")
async def local_only(request: Request, call_next):
    """Refuse rebinding hosts and cross-site writes; the API can drive agents on local files."""
    if _host_name(request.headers.get("host", "")).lower() not in settings.allowed_hosts:
        return JSONResponse({"detail": "Host not allowed"}, status_code=403)
    origin = request.headers.get("origin")
    if request.method not in SAFE_METHODS and origin and origin not in settings.allowed_origins:
        return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
    return await call_next(request)


@app.get("/health")
def health():
    return {
        "status": "ok", "configured_providers": configured_providers() + ["mock"],
        "available_cli_channels": sorted(providers.cli.available_model_ids()),
        "exhausted_models": providers.quota.public(),
    }


@app.get("/api/models/quota")
def model_quota():
    """Models out of quota and when they come back; routing skips them until then."""
    return providers.quota.public()


@app.get("/api/recovery")
def shared_recovery():
    """Auditable shared recoveries reused by all projects, including after a restart."""
    return {"models": providers.quota.public(), "rules": providers.quota.rules,
            "shared_repairs": db.shared_repairs()}


@app.delete("/api/models/quota/{model_id}")
def reset_model_quota(model_id: str):
    """The user knows the quota is back (or topped up the account): use the model again."""
    providers.quota.clear(model_id)
    return providers.quota.public()


@app.get("/api/channels/cli")
def list_cli_channels(refresh: bool = False):
    statuses = providers.cli.refresh() if refresh else providers.cli.statuses
    return [status.public() for status in statuses.values()]


@app.get("/api/models")
async def list_models():
    # Only models with Artificial Analysis results (CLIs inherit them) are shown and routed.
    configured = providers.configured_models(list(models))
    benchmark_matches = benchmarks.match_models(list(models.values()))
    return [
        {
            **model.model_dump(), "configured": model.id in configured,
            "routing_ready": model.routing_ready,
            "benchmarks": benchmark_matches.get(model.id, []),
            "auth_type": (
                providers.cli.statuses[model.id].auth_type if model.channel == "cli" else "api_key"
            ),
        }
        for model in models.values() if model.scores_source == AA_SOURCE
    ]


@app.get("/api/models/sync")
async def catalog_status():
    return catalog_sync.status()


@app.post("/api/models/sync", status_code=202)
async def refresh_catalog():
    catalog_sync.trigger()
    return catalog_sync.status()


@app.patch("/api/models/{model_id}")
async def update_model(model_id: str, body: UpdateModel):
    model = models.get(model_id)
    if not model:
        raise HTTPException(404, "Model not found")
    if body.enabled and not model.routing_ready:
        raise HTTPException(422, "模型尚缺少调度所需的评分、规格或价格，或已被来源标记为不可用。")
    model.enabled = body.enabled
    db.save_models([model])
    return {"id": model.id, "enabled": model.enabled}


@app.patch("/api/models")
async def update_model_by_id(body: UpdateModelById):
    return await update_model(body.model_id, body)


@app.put("/api/providers/{provider}/key")
def store_provider_key(provider: str, body: ProviderKey):
    try:
        stored = set_provider_key(provider, body.api_key)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    providers.quota.clear_provider(provider)
    return {"stored": stored}


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


@app.delete("/api/providers/{provider}/key")
def remove_provider_key(provider: str):
    try:
        removed = delete_provider_key(provider)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"removed": removed}


@app.post("/api/providers/{provider}/test")
async def test_provider(provider: str):
    try:
        message = await providers.test_provider(provider)
        return {"ok": True, "message": message}
    except ProviderError as exc:
        raise HTTPException(400, str(exc)) from exc


# Walking the home directory takes up to a few seconds, so reuse a recent scan.
DISCOVERY_TTL = 60.0
_discovery: dict = {"at": 0.0, "result": None}


@app.get("/api/projects/discover")
def discover_project_dirs(refresh: bool = False):
    now = time.monotonic()
    if refresh or _discovery["result"] is None or now - _discovery["at"] > DISCOVERY_TTL:
        _discovery.update(at=now, result=discover_projects())
    return _discovery["result"]


@app.get("/api/projects/inspect")
def inspect_project_dir(path: str):
    try:
        return inspect_project(path)
    except ProjectDirError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/projects/native-picker")
def native_picker():
    return native_picker_status()


@app.post("/api/projects/native-picker")
async def open_native_picker(start: Optional[str] = None):
    """Open the desktop folder dialog on the machine running this API."""
    try:
        path = await native_pick_directory(start)
    except NativePickerError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ProjectDirError as exc:
        raise HTTPException(400, str(exc)) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(408, "The folder dialog timed out") from exc
    return {"path": str(path) if path else None}


@app.post("/api/attachments", response_model=Attachment, status_code=201)
async def upload_attachment(request: Request, name: str = "attachment"):
    """Raw file body; the name comes in the query so no multipart parser is needed."""
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > files.MAX_ATTACHMENT_BYTES:
            raise HTTPException(413, f"单个附件不能超过 {files.MAX_ATTACHMENT_BYTES // 1024 // 1024} MB。")
    try:
        return await asyncio.to_thread(files.save, name, bytes(data))
    except files.AttachmentError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/attachments/{attachment_id}/content")
def attachment_content(attachment_id: str):
    item = files.get(attachment_id)
    if not item:
        raise HTTPException(404, "附件不存在")
    # Only detected raster images are shown inline; everything else downloads as plain bytes.
    inline = item.kind == "image"
    return FileResponse(
        files.path(item), media_type=item.media_type if inline else "application/octet-stream",
        filename=item.name, content_disposition_type="inline" if inline else "attachment",
        headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"},
    )


@app.delete("/api/attachments/{attachment_id}", status_code=204)
def delete_attachment(attachment_id: str):
    """Discard an upload removed from the composer before the task was created."""
    if any(item.id == attachment_id for run in db.list_runs() for item in run.attachments):
        raise HTTPException(409, "附件属于已创建的任务，随任务一起删除。")
    if not files.delete(attachment_id):
        raise HTTPException(404, "附件不存在")


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
    try:
        attached = files.resolve(body.attachments)
    except files.AttachmentError as exc:
        raise HTTPException(400, str(exc)) from exc
    needs_vision = any(item.kind == "image" for item in attached)
    plan = await autonomous_plan(
        body.prompt + files.describe(attached, with_paths=False), list(models.values()), configured, providers,
        project_context=await asyncio.to_thread(project_context, project_dir) if project_dir else None,
    )
    try:
        for node in plan.nodes:
            available = eligible_for_node(node, list(models.values()), configured, needs_vision)
            if available:
                best_score = max(
                    model.scores.score_for(node.capability) for model in available
                )
                if node.quality_threshold > best_score:
                    original = node.quality_threshold
                    node.quality_threshold = best_score
                    adjustment = (
                        f"Adjusted {node.title} {node.capability} threshold "
                        f"from {original:g} to {best_score:g} for available models"
                    )
                    plan.planning_note = (
                        f"{plan.planning_note}; {adjustment}" if plan.planning_note else adjustment
                    )
            route_node(node, list(models.values()), configured, needs_vision)
    except NoEligibleModelError as exc:
        raise HTTPException(422, str(exc)) from exc
    plan.estimate = estimate(plan.nodes, body.budget_usd, plan.planning_cost_usd)
    run = ExecutionRun(
        id=str(uuid4()), prompt=body.prompt, status=RunStatus.AWAITING_CONFIRMATION,
        plan=plan, budget_usd=body.budget_usd, use_cli=body.use_cli,
        project_dir=str(project_dir) if project_dir else None, attachments=attached,
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


@app.delete("/api/tasks/{run_id}", status_code=204)
async def delete_task(run_id: str):
    if any(task and not task.done() for task in (executor.tasks.get(run_id), executor.diagnosing.get(run_id))):
        raise HTTPException(409, "任务仍在执行或结束处理中，请稍后再删除。")
    existing = db.get_run(run_id)
    deleted_attachments = existing.attachments if existing else []
    try:
        deleted = db.delete_run(run_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not deleted:
        raise HTTPException(404, "Task not found")
    executor.tasks.pop(run_id, None)
    executor.locks.pop(run_id, None)
    executor.reservations.pop(run_id, None)
    executor.budget_events.pop(run_id, None)
    for item in deleted_attachments:
        files.delete(item.id)


def reopen_node(run: ExecutionRun, node) -> None:
    """Give a failed or stopped node fresh attempts; the next one sees why the last one
    failed, and the latest diagnosis's advice."""
    if node.status == NodeStatus.FAILED and node.attempts:
        node.feedback = node.feedback or node.attempts[-1].reason
        advice = next((item.advice for item in reversed(run.diagnoses)
                       if item.status == "done" and item.advice and item.node_id in {node.id, None}), None)
        if advice and advice not in node.feedback:
            node.feedback = f"{node.feedback}\n诊断建议：{advice}"
    node.status, node.failed_attempts, node.environment_fixes = NodeStatus.PENDING, 0, 0


@app.post("/api/tasks/{run_id}/confirm", response_model=ExecutionRun)
async def confirm_task(run_id: str, body: ConfirmTask):
    run = get_task(run_id)
    diagnosis = executor.diagnosing.get(run_id)
    if diagnosis and not diagnosis.done():
        raise HTTPException(409, "诊断或修复仍在进行，完成后会自动继续；也可以取消任务。")
    if run.status not in {RunStatus.AWAITING_CONFIRMATION, RunStatus.PAUSED}:
        raise HTTPException(409, f"Cannot confirm task in {run.status} status")
    budget = body.budget_usd if body.budget_usd is not None else run.budget_usd
    configured = providers.configured_models(list(models))
    if any(n.model_id and not n.model_id.startswith("mock:") for n in run.plan.nodes):
        configured = {mid for mid in configured if not mid.startswith("mock:")}
    if not run.use_cli:
        configured = {mid for mid in configured if not mid.startswith("cli:")}
    try:
        for node in run.plan.nodes:
            if node.status in {NodeStatus.SUCCEEDED, NodeStatus.RUNNING}:
                continue
            available = eligible_for_node(node, list(models.values()), configured, any(a.kind == "image" for a in run.attachments))
            if not any(model.id == node.model_id for model in available):
                route_node(node, list(models.values()), configured, any(a.kind == "image" for a in run.attachments))
    except NoEligibleModelError as exc:
        raise HTTPException(422, str(exc)) from exc
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
    # A task paused after a diagnosis continues with its failed steps run again.
    reopened = [node.id for node in run.plan.nodes if node.status == NodeStatus.FAILED]
    for node in run.plan.nodes:
        if node.id in reopened:
            reopen_node(run, node)
    if reopened:
        run.rework_rounds, run.error = 0, None
    # A task paused by the user may still be finishing its running steps; that loop goes on.
    task = executor.tasks.get(run.id)
    run.status = RunStatus.RUNNING if task and not task.done() else RunStatus.QUEUED
    db.save_run(run)
    db.add_event(Event(run_id=run.id, kind="run.confirmed", data={"budget_usd": run.budget_usd, "reopened": reopened}))
    executor.start(run.id)
    return run


@app.post("/api/tasks/{run_id}/pause", response_model=ExecutionRun)
async def pause_task(run_id: str):
    """Stop starting new steps; steps already running finish and keep their results."""
    run = get_task(run_id)
    if run.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
        raise HTTPException(409, f"Cannot pause task in {run.status} status")
    async with executor.locks.setdefault(run.id, asyncio.Lock()):
        run = get_task(run_id)
        run.status = RunStatus.PAUSED
        db.save_run(run)
    running = [node.id for node in run.plan.nodes if node.status == NodeStatus.RUNNING]
    db.add_event(Event(run_id=run.id, kind="approval.required", data={"reason": "user_paused", "running": running}))
    return run


@app.post("/api/tasks/{run_id}/diagnose", response_model=ExecutionRun)
async def diagnose_task(run_id: str):
    """Ask a strong model (a membership CLI when possible) why the task is stuck."""
    async with executor.locks.setdefault(run_id, asyncio.Lock()):
        run = get_task(run_id)
        failed = next((node.id for node in run.plan.nodes if node.status == NodeStatus.FAILED), None)
        if failed is None:
            failed = next((node.id for node in run.plan.nodes if node.status == NodeStatus.PENDING and node.attempts), None)
        diagnosis_id = executor.begin_diagnosis(run, "manual", failed)
        if diagnosis_id is None and (not run.diagnoses or run.diagnoses[-1].status != "failed"):
            raise HTTPException(409, "诊断正在进行中，请稍候。")
        db.save_run(run)
    if diagnosis_id:
        executor.start_diagnosis(run.id, diagnosis_id)
    return run


@app.post("/api/tasks/{run_id}/resume", response_model=ExecutionRun)
async def resume_task(run_id: str, body: ConfirmTask):
    """Continue a failed or cancelled task from where it stopped: finished nodes keep their
    results; failed and cancelled nodes run again with fresh attempts and rework rounds."""
    run = get_task(run_id)
    diagnosis = executor.diagnosing.get(run_id)
    if diagnosis and not diagnosis.done():
        raise HTTPException(409, "诊断或修复仍在进行，完成后会自动继续；也可以取消任务。")
    if run.status not in {RunStatus.FAILED, RunStatus.CANCELLED}:
        raise HTTPException(409, f"Cannot resume task in {run.status} status")
    task = executor.tasks.get(run_id)
    if task and not task.done():
        raise HTTPException(409, "任务仍在结束处理中，请稍后再继续。")
    configured = providers.configured_models(list(models))
    if any(n.model_id and not n.model_id.startswith("mock:") for n in run.plan.nodes):
        configured = {mid for mid in configured if not mid.startswith("mock:")}
    if not run.use_cli:
        configured = {model_id for model_id in configured if not model_id.startswith("cli:")}
    needs_vision = any(item.kind == "image" for item in run.attachments)
    reopened = []
    try:
        for node in run.plan.nodes:
            if node.status == NodeStatus.SUCCEEDED:
                continue
            if node.status in {NodeStatus.FAILED, NodeStatus.CANCELLED, NodeStatus.RUNNING}:
                reopened.append(node.id)
                reopen_node(run, node)
            available = eligible_for_node(node, list(models.values()), configured, needs_vision)
            if not any(model.id == node.model_id for model in available):
                # Its model was disabled or lost its key since; pick again under current rules.
                route_node(node, list(models.values()), configured, needs_vision)
    except NoEligibleModelError as exc:
        raise HTTPException(422, str(exc)) from exc
    budget = body.budget_usd if body.budget_usd is not None else run.budget_usd
    required = run.actual_cost_usd + sum(
        node.estimated_cost_usd for node in run.plan.nodes if node.status != NodeStatus.SUCCEEDED
    )
    if required > budget:
        raise HTTPException(
            409, f"Budget ${budget:.2f} is below the ${required:.4f} needed to finish this task",
        )
    run.budget_usd, run.rework_rounds, run.error = budget, 0, None
    if run.plan.estimate:
        run.plan.estimate.hard_limit_usd = budget
    run.status = RunStatus.QUEUED
    db.save_run(run)
    db.add_event(Event(run_id=run.id, kind="run.resumed", data={"budget_usd": budget, "reopened": reopened}))
    executor.start(run.id)
    return run


@app.post("/api/tasks/{run_id}/cancel", response_model=ExecutionRun)
async def cancel_task(run_id: str):
    run = get_task(run_id)
    diagnosis = executor.diagnosing.get(run_id)
    if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED} and not (diagnosis and not diagnosis.done()):
        raise HTTPException(409, "Task has already finished")
    run.status = RunStatus.CANCELLED
    db.save_run(run)
    db.add_event(Event(run_id=run.id, kind="run.cancelled"))
    executor.cancel(run.id)
    return run


class GithubToken(BaseModel):
    token: str = Field(min_length=10, max_length=500)


class PublishRequest(BaseModel):
    # Either create ``name`` with the saved token, or push to an existing empty ``remote``.
    name: Optional[str] = None
    private: bool = True
    description: str = ""
    remote: Optional[str] = None


@app.get("/api/github")
def github_status():
    return {"token": bool(os.getenv(SECRET_ENV["github"])), "git": shutil.which("git") is not None}


@app.put("/api/github/token")
def store_github_token(body: GithubToken):
    return {"stored": set_provider_key("github", body.token.strip())}


@app.delete("/api/github/token")
def remove_github_token():
    return {"removed": delete_provider_key("github")}


@app.get("/api/tasks/{run_id}/files")
def task_files(run_id: str):
    run = get_task(run_id)
    return {"root": str(publish.output_root(run)), "in_project": bool(run.project_dir),
            "files": publish.output_files(run), "github_url": run.github_url}


@app.get("/api/tasks/{run_id}/files/content")
def task_file_content(run_id: str, path: str):
    try:
        return {"path": path, "text": publish.read_output(get_task(run_id), path)}
    except publish.PublishError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/tasks/{run_id}/archive")
def task_archive(run_id: str):
    run = get_task(run_id)
    try:
        data = publish.archive(run)
    except publish.PublishError as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(data, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{publish.slug(run)}.zip"',
    })


@app.post("/api/tasks/{run_id}/github")
async def publish_task(run_id: str, body: PublishRequest):
    """Commit the task's output to a new or empty GitHub repository."""
    run = get_task(run_id)
    if run.project_dir:
        raise HTTPException(409, "项目任务的改动在你自己的仓库里，请在项目中提交并推送。")
    if run.status != RunStatus.SUCCEEDED:
        raise HTTPException(409, "任务完成后才能上传。")
    token = os.getenv(SECRET_ENV["github"])
    try:
        if body.remote:
            remote, url = publish.check_remote(body.remote), None
            await asyncio.to_thread(publish.push_snapshot, run, remote)
            url = re.sub(r"\.git/?$", "", remote.replace("git@github.com:", "https://github.com/"))
        else:
            if not token:
                raise publish.PublishError("还没有保存 GitHub 令牌；可以先保存令牌，或填写已建好的空仓库地址。")
            name = (body.name or publish.slug(run)).strip()
            if not publish.REPO_NAME.match(name):
                raise publish.PublishError("仓库名只能包含字母、数字、点、横线和下划线")
            repo = await publish.create_repository(token, name, body.private, body.description or run.plan.summary, providers.client)
            await asyncio.to_thread(publish.push_snapshot, run, repo["clone_url"], token)
            url = repo["html_url"]
    except publish.PublishError as exc:
        raise HTTPException(400, str(exc)) from exc
    run.github_url = url
    db.save_run(run)
    db.add_event(Event(run_id=run.id, kind="run.published", data={"url": url}))
    return {"url": url}


@app.get("/api/tasks/{run_id}/timeline")
def task_timeline(run_id: str, after: int = 0):
    """All events of a task so far, oldest first, for the plan view's progress panel."""
    if not db.get_run(run_id):
        raise HTTPException(404, "Task not found")
    return [event.model_dump(mode="json") for event in db.events(run_id, after)]


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
            if not run or (
                run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
                and not events
            ):
                break
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream")

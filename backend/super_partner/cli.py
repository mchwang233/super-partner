from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import httpx
import typer

from .config import settings

app = typer.Typer(help="Super Partner multi-model task orchestrator")


@app.command()
def models():
    response = httpx.get(f"{settings.api_base_url}/api/models")
    response.raise_for_status()
    for model in response.json():
        ready = "ready" if model["configured"] and model["enabled"] else "not active"
        if not model.get("routing_ready", True):
            ready = "pending metadata / unavailable"
        prices = "/".join(
            f"${model[key]}" if model[key] is not None else "unknown"
            for key in ("input_per_million", "output_per_million")
        )
        typer.echo(f"{model['id']:<40} {ready:<30} {prices}")


@app.command()
def plan(
    prompt: str,
    budget: float = 2.0,
    use_cli: bool = typer.Option(True, "--use-cli/--no-use-cli", help="Allow membership CLIs"),
    project: Optional[Path] = None,
    as_json: bool = False,
):
    response = httpx.post(
        f"{settings.api_base_url}/api/tasks",
        json={
            "prompt": prompt, "budget_usd": budget, "use_cli": use_cli,
            "project_dir": str(project.expanduser().resolve()) if project else None,
        },
        timeout=180,
    )
    if response.is_error:
        typer.echo(response.json().get("detail", response.text), err=True)
        raise typer.Exit(1)
    response.raise_for_status()
    data = response.json()
    if as_json:
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2))
        return
    typer.echo(f"Task {data['id']} — {data['plan']['summary']}")
    typer.echo(f"CLI routing: {'enabled' if data['use_cli'] else 'disabled'}")
    if data.get("project_dir"):
        typer.echo(f"Project: {data['project_dir']}")
    for node in data["plan"]["nodes"]:
        where = " [project]" if node.get("needs_workspace") else ""
        typer.echo(f"  {node['title']}: {node['model_id']}{where} (${node['estimated_cost_usd']:.6f})")
    estimate = data["plan"]["estimate"]
    typer.echo(f"Estimated ${estimate['low_usd']:.4f}–${estimate['high_usd']:.4f}; limit ${estimate['hard_limit_usd']:.2f}")


@app.command()
def run(task_id: str, budget: Optional[float] = None, wait: bool = True):
    response = httpx.post(f"{settings.api_base_url}/api/tasks/{task_id}/confirm", json={"budget_usd": budget})
    response.raise_for_status()
    if not wait:
        typer.echo(response.json()["status"])
        return
    while True:
        data = httpx.get(f"{settings.api_base_url}/api/tasks/{task_id}").json()
        typer.echo(f"\r{data['status']}  ${data['actual_cost_usd']:.6f}", nl=False)
        if data["status"] in {"succeeded", "failed", "cancelled", "incomplete", "paused"}:
            typer.echo()
            typer.echo(data.get("final_result") or data.get("error") or "Paused")
            return
        time.sleep(1)


@app.command()
def status(task_id: str, as_json: bool = False):
    response = httpx.get(f"{settings.api_base_url}/api/tasks/{task_id}")
    response.raise_for_status()
    data = response.json()
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2) if as_json else f"{data['status']} ${data['actual_cost_usd']:.6f}")

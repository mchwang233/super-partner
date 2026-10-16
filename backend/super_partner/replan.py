"""Changing a running task's plan at its lead's decision: adding steps, removing or rewriting
steps that have not finished. Finished steps never change. Raises PlanError with what is wrong,
so the lead can be asked again."""

from __future__ import annotations

from uuid import uuid4

from .models import ExecutionRun, ModelProfile, NodeStatus, TaskNode
from .planner import MAX_NODES, _difficulty, package
from .routing import NoEligibleModelError, route_node


class PlanError(Exception):
    """A plan change that cannot be made, and why."""


CAPABILITIES = {"coding", "planning", "research", "chinese", "tool_use", "structured_output"}
# Steps that may not be removed or rewritten: their work is done or under way.
SETTLED = {NodeStatus.SUCCEEDED, NodeStatus.RUNNING}


def change_plan(run: ExecutionRun, change: dict, models: list[ModelProfile], configured: set[str],
                needs_vision: bool = False, deciding: str | None = None) -> dict:
    """Apply ``change`` ({"add", "remove", "update"}) to ``run``'s plan in place. ``deciding`` is
    the step the decision is about, which may be changed although it is held as running.
    Returns the titles added, removed and updated."""
    nodes = {node.id: node for node in run.plan.nodes}

    def changeable(node_id: str, what: str) -> TaskNode:
        node = nodes.get(node_id)
        if node is None:
            raise PlanError(f"{what}: no step with id {node_id}")
        if node.status in SETTLED and node.id != deciding:
            raise PlanError(f"{what}: step {node_id} ({node.title}) is {node.status.value}; finished or running steps stay")
        return node

    removed = [changeable(str(item), "remove") for item in change.get("remove") or []]
    gone = {node.id for node in removed}
    add = change.get("add") or []
    if not isinstance(add, list):
        raise PlanError("add must be a list of steps")
    keys = {}
    for raw in add:
        key = str(raw.get("key") or "").strip()
        if not key or key in keys or key in nodes:
            raise PlanError(f"every added step needs its own key (got {key!r})")
        keys[key] = uuid4().hex[:8]

    def resolve(items, what: str) -> list[str]:
        if not isinstance(items, list):
            raise PlanError(f"{what} must be a list")
        resolved = []
        for item in map(str, items):
            target = keys.get(item) or (item if item in nodes and item not in gone else None)
            if target is None:
                raise PlanError(f"{what}: unknown or removed step {item}")
            resolved.append(target)
        return resolved

    workspace = any(node.needs_workspace for node in run.plan.nodes)
    added = []
    for raw in add:
        capability = raw.get("capability", "coding")
        if capability not in CAPABILITIES:
            raise PlanError(f"step {raw['key']}: unsupported capability {capability}")
        instructions = str(raw.get("instructions") or "").strip()
        if not instructions:
            raise PlanError(f"step {raw['key']} needs instructions")
        added.append(TaskNode(
            id=keys[str(raw["key"]).strip()], title=str(raw.get("title") or raw["key"])[:120],
            instructions=instructions, capability=capability,
            acceptance_criteria=str(raw.get("acceptance_criteria") or "").strip()[:1000],
            dependencies=resolve(raw.get("dependencies") or [], f"step {raw['key']} dependencies"),
            verifies=resolve(raw.get("verifies") or [], f"step {raw['key']} verifies"),
            needs_workspace=workspace, **_difficulty(raw), **package(raw),
        ))

    updated = []
    for raw in change.get("update") or []:
        node = changeable(str(raw.get("id")), "update")
        if node.id in gone:
            raise PlanError(f"update: step {node.id} is also removed")
        for field in ("title", "instructions", "acceptance_criteria"):
            if raw.get(field):
                setattr(node, field, str(raw[field]).strip()[:120 if field == "title" else 20_000])
        for field, value in package(raw).items():
            if field in raw:
                setattr(node, field, value)
        if "dependencies" in raw:
            node.dependencies = resolve(raw["dependencies"], f"step {node.id} dependencies")
        updated.append(node)

    remaining = [node for node in run.plan.nodes if node.id not in gone] + added
    if len(remaining) > MAX_NODES:
        raise PlanError(f"a plan may have at most {MAX_NODES} steps")
    for node in remaining:
        node.dependencies = [item for item in node.dependencies if item not in gone]
        node.verifies = [item for item in node.verifies if item not in gone]
    _check_acyclic(remaining)
    for node in added:
        try:
            route_node(node, models, configured, needs_vision)
        except NoEligibleModelError as exc:
            raise PlanError(f"no model can take step {node.title}: {exc}") from exc
    run.plan.nodes = remaining
    return {"added": [node.title for node in added], "removed": [node.title for node in removed],
            "updated": [node.title for node in updated]}


def _check_acyclic(nodes: list[TaskNode]) -> None:
    ids = {node.id for node in nodes}
    waiting = {node.id: {dep for dep in node.dependencies if dep in ids} for node in nodes}
    for node in nodes:
        if any(dep not in ids for dep in node.dependencies):
            raise PlanError(f"step {node.id} depends on a step that is not in the plan")
    while waiting:
        ready = [node_id for node_id, deps in waiting.items() if not deps]
        if not ready:
            raise PlanError("the changed plan has a dependency cycle")
        for node_id in ready:
            del waiting[node_id]
        for deps in waiting.values():
            deps.difference_update(ready)

import json
import re
from typing import Optional
from uuid import uuid4

from .models import ModelProfile, TaskNode, TaskPlan
from .providers import ProviderRegistry


def _node(
    title: str, instructions: str, capability: str, dependencies: Optional[list[str]] = None,
    needs_workspace: bool = False,
) -> TaskNode:
    return TaskNode(
        id=str(uuid4())[:8], title=title, instructions=instructions,
        capability=capability, dependencies=dependencies or [], needs_workspace=needs_workspace,
    )


def heuristic_plan(prompt: str, in_project: bool = False) -> TaskPlan:
    """Deterministic fallback planner; a provider-backed planner can replace this later."""
    lower = prompt.lower()
    is_code = any(word in lower for word in ("code", "bug", "repo", "implement", "代码", "修复", "实现"))
    is_research = any(word in lower for word in ("research", "source", "compare", "调研", "资料", "对比"))
    nodes: list[TaskNode] = []
    if is_research:
        research = _node("Research", f"Collect reliable evidence for: {prompt}", "research")
        nodes.append(research)
    if is_code:
        inspect = _node(
            "Inspect workspace", f"Inspect the current project and identify changes for: {prompt}",
            "planning", needs_workspace=in_project,
        )
        implement = _node(
            "Implement", f"Implement the requested change: {prompt}", "coding", [inspect.id],
            needs_workspace=in_project,
        )
        verify = _node(
            "Verify", "Review the implementation and report validation results.", "coding",
            [implement.id], needs_workspace=in_project,
        )
        nodes.extend([inspect, implement, verify])
    if not nodes:
        analyze = _node("Analyze", prompt, "planning")
        nodes.append(analyze)
    dependencies = [node.id for node in nodes]
    synthesis = _node("Synthesize result", "Combine all completed work into a concise final answer.", "structured_output", dependencies)
    nodes.append(synthesis)
    summary = re.sub(r"\s+", " ", prompt).strip()[:180]
    return TaskPlan(summary=summary, nodes=nodes)


PLANNER_PROMPT = """You are the planning engine for an autonomous multi-model executor.
Decompose the user request into a small, executable DAG. Return JSON only, with this shape:
{
  "summary": "short objective",
  "nodes": [
    {
      "key": "n1",
      "title": "short title",
      "instructions": "self-contained execution instructions and acceptance criteria",
      "capability": "coding|planning|research|chinese|tool_use|structured_output",
      "dependencies": [],
      "estimated_input_tokens": 1500,
      "estimated_output_tokens": 800,
      "quality_threshold": 0.75,
      "needs_workspace": false
    }
  ]
}
Use 2-10 nodes. Dependencies reference earlier node keys only. Parallelize independent work.
Include verification and final synthesis nodes when appropriate. Set needs_workspace to true
only for nodes that must read or modify files in the target project; those run one at a time. Token estimates must include expected
dependency results and project context. Never include markdown fences.

User request:
"""


def select_planner_model(models: list[ModelProfile], configured: set[str]) -> Optional[ModelProfile]:
    candidates = [model for model in models if model.enabled and model.id in configured]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda model: (
            -model.scores.planning,
            -model.scores.reliability,
            model.execution_priority,
            model.input_per_million + model.output_per_million,
        ),
    )


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("planner did not return a JSON object")
    return json.loads(text[start:end + 1])


def _validate_plan(payload: dict) -> TaskPlan:
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list) or not 2 <= len(raw_nodes) <= 10:
        raise ValueError("planner must return 2-10 nodes")
    allowed = {"coding", "planning", "research", "chinese", "tool_use", "structured_output"}
    key_to_id: dict[str, str] = {}
    nodes: list[TaskNode] = []
    for index, raw in enumerate(raw_nodes):
        key = str(raw.get("key", f"n{index + 1}"))
        if key in key_to_id:
            raise ValueError(f"duplicate planner node key: {key}")
        dependencies = raw.get("dependencies", [])
        if not isinstance(dependencies, list) or any(dep not in key_to_id for dep in dependencies):
            raise ValueError(f"node {key} has a forward or unknown dependency")
        capability = raw.get("capability", "planning")
        if capability not in allowed:
            raise ValueError(f"node {key} has unsupported capability: {capability}")
        node_id = str(uuid4())[:8]
        key_to_id[key] = node_id
        nodes.append(TaskNode(
            id=node_id,
            title=str(raw.get("title", key))[:120],
            instructions=str(raw.get("instructions", "")).strip(),
            capability=capability,
            dependencies=[key_to_id[dep] for dep in dependencies],
            estimated_input_tokens=max(100, min(int(raw.get("estimated_input_tokens", 1500)), 1_000_000)),
            estimated_output_tokens=max(50, min(int(raw.get("estimated_output_tokens", 800)), 128_000)),
            quality_threshold=max(.5, min(float(raw.get("quality_threshold", .7)), .99)),
            needs_workspace=raw.get("needs_workspace") is True,
        ))
    if any(not node.instructions for node in nodes):
        raise ValueError("every planner node needs instructions")
    return TaskPlan(summary=str(payload.get("summary", "Autonomous task plan"))[:240], nodes=nodes)


async def autonomous_plan(
    prompt: str,
    models: list[ModelProfile],
    configured: set[str],
    providers: ProviderRegistry,
    project_context: Optional[str] = None,
) -> TaskPlan:
    in_project = project_context is not None
    planner = select_planner_model(models, configured)
    if planner is None or planner.provider == "mock":
        plan = heuristic_plan(prompt, in_project)
        plan.planning_note = "No capable configured planner; used deterministic fallback"
        return plan
    planning_cost = 0.0
    try:
        result = await providers.complete(
            planner.id, PLANNER_PROMPT + prompt + (f"\n\n{project_context}" if project_context else ""),
            cli_timeout=120,
        )
        charged_id = result.used_model_id or planner.id
        charged_model = next(model for model in models if model.id == charged_id)
        planning_cost = (
            result.input_tokens * charged_model.input_per_million
            + result.output_tokens * charged_model.output_per_million
        ) / 1_000_000
        plan = _validate_plan(_extract_json(result.text))
        if not in_project:
            # Without a project every node runs in its own scratch workspace, so any model will do.
            for node in plan.nodes:
                node.needs_workspace = False
        plan.planning_method = "model"
        plan.planner_model_id = planner.id
        plan.planning_cost_usd = round(planning_cost, 6)
        if charged_id != planner.id:
            plan.planning_note = f"Planner CLI fell back to {charged_id}"
        return plan
    except Exception as exc:
        plan = heuristic_plan(prompt, in_project)
        plan.planner_model_id = planner.id
        plan.planning_cost_usd = round(planning_cost, 6)
        plan.planning_note = f"Planner fallback: {str(exc)[:300]}"
        return plan

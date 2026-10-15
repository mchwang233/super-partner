import json
import re
from typing import Optional
from uuid import uuid4

from .models import (
    DIFFICULTY_THRESHOLDS,
    ModelProfile,
    TaskNode,
    TaskPlan,
    difficulty_for,
    to_percent_scale,
)
from .providers import ProviderRegistry
from .quota import ModelUnavailable


def _node(
    title: str, instructions: str, capability: str, dependencies: Optional[list[str]] = None,
    needs_workspace: bool = False, difficulty: str = "normal", acceptance: str = "",
    verifies: Optional[list[str]] = None,
) -> TaskNode:
    return TaskNode(
        id=str(uuid4())[:8], title=title, instructions=instructions,
        capability=capability, dependencies=dependencies or [], needs_workspace=needs_workspace,
        difficulty=difficulty, quality_threshold=DIFFICULTY_THRESHOLDS[difficulty],
        acceptance_criteria=acceptance, verifies=verifies or [],
    )


def heuristic_plan(prompt: str, in_project: bool = False) -> TaskPlan:
    """Deterministic fallback planner; a provider-backed planner can replace this later."""
    lower = prompt.lower()
    is_code = any(word in lower for word in ("code", "bug", "repo", "implement", "代码", "修复", "实现"))
    is_research = any(word in lower for word in ("research", "source", "compare", "调研", "资料", "对比"))
    nodes: list[TaskNode] = []
    if is_research:
        research = _node(
            "Research", f"Collect reliable evidence for: {prompt}", "research",
            acceptance="Cites concrete sources or evidence for each finding; covers the question asked.",
        )
        nodes.append(research)
    if is_code:
        inspect = _node(
            "Inspect workspace", f"Inspect the current project and identify changes for: {prompt}",
            "planning", needs_workspace=in_project,
            acceptance="Names the files and changes needed, grounded in the actual project.",
        )
        implement = _node(
            "Implement", f"Implement the requested change: {prompt}", "coding", [inspect.id],
            needs_workspace=in_project, difficulty="hard",
            acceptance="The requested change is implemented in the files, not just described.",
        )
        verify = _node(
            "Verify", "Review the implementation and report validation results.", "coding",
            [implement.id], needs_workspace=in_project, verifies=[implement.id],
        )
        nodes.extend([inspect, implement, verify])
    if not nodes:
        analyze = _node("Analyze", prompt, "planning")
        nodes.append(analyze)
    dependencies = [node.id for node in nodes]
    synthesis = _node("Synthesize result", "Combine all completed work into a concise final answer.", "structured_output", dependencies, difficulty="easy")
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
      "difficulty": "easy|normal|hard",
      "acceptance_criteria": "checkable conditions a reviewer uses to accept the result",
      "verifies": [],
      "needs_workspace": false
    }
  ]
}
Rate each node's difficulty for the model that will do it:
- easy: routine, well-specified work (formatting, summarising given results, small edits).
- normal: ordinary analysis, research or implementation with clear requirements.
- hard: open-ended design, subtle debugging, complex multi-file changes, deep reasoning.
Easy and normal nodes go to the cheapest capable model; hard nodes go to the strongest one.
Every node's result is checked against its acceptance_criteria by another model and retried
if it fails, so make the criteria concrete and checkable. A verification node lists in
"verifies" the keys of the nodes it checks (they must also be in its dependencies); if it
finds problems, those nodes are redone with its findings.
What executors can do: membership CLIs (Codex, Claude Code) edit files and run shell commands
in a sandbox that can write only to their working directory and has no network access, so
they can run syntax checks, tests and scripts but cannot install packages or fetch URLs.
API models that support tool calls work the same way through file and command tools in
the same sandbox. Keep acceptance criteria within these limits.
Use 2-10 nodes. Dependencies reference earlier node keys only. Parallelize independent work.
Include verification and final synthesis nodes when appropriate. Set needs_workspace to true
only for nodes that must read or modify files in the target project; those run one at a time. Token estimates must include expected
dependency results and project context. Never include markdown fences.

User request:
"""


def select_planner_model(models: list[ModelProfile], configured: set[str]) -> Optional[ModelProfile]:
    candidates = [
        model for model in models
        if model.enabled and model.routing_ready and model.id in configured
    ]
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


def _verified(raw: dict, dependencies: list) -> list[str]:
    """Keys a verification node checks; only its own dependencies can be sent back."""
    verifies = raw.get("verifies") or []
    return [item for item in dict.fromkeys(verifies) if item in dependencies] if isinstance(verifies, list) else []


def _difficulty(raw: dict) -> dict:
    """Difficulty and threshold for a planner node; a bare threshold (older prompt) still works."""
    difficulty = str(raw.get("difficulty", "")).strip().lower()
    if difficulty in DIFFICULTY_THRESHOLDS:
        return {"difficulty": difficulty, "quality_threshold": DIFFICULTY_THRESHOLDS[difficulty]}
    threshold = max(50, min(to_percent_scale(float(raw.get("quality_threshold", 80))), 99))
    return {"difficulty": difficulty_for(threshold), "quality_threshold": threshold}


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
            **_difficulty(raw),
            acceptance_criteria=str(raw.get("acceptance_criteria", "")).strip()[:1000],
            verifies=[key_to_id[item] for item in _verified(raw, dependencies)],
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
    switched = []
    try:
        while True:
            try:
                result = await providers.complete(
                    planner.id, PLANNER_PROMPT + prompt + (f"\n\n{project_context}" if project_context else ""),
                    cli_timeout=120,
                )
                break
            except ModelUnavailable:
                # Out of quota: the next best planner takes over, up to a few.
                configured = configured - {planner.id}
                backup = select_planner_model(models, configured)
                if backup is None or backup.provider == "mock" or len(switched) >= 2:
                    raise
                switched.append(planner.id)
                planner = backup
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
        if switched:
            plan.planning_note = f"{'、'.join(switched)} 暂不可用，改由 {planner.id} 规划"
        return plan
    except Exception as exc:
        plan = heuristic_plan(prompt, in_project)
        plan.planner_model_id = planner.id
        plan.planning_cost_usd = round(planning_cost, 6)
        plan.planning_note = f"Planner fallback: {str(exc)[:300]}"
        return plan

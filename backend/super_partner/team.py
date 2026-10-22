"""Project employees, shared context, and durable delegation through the existing task graph."""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath

from . import skills
from .config import settings
from .models import Employee, NodeStatus, TaskPlan
from .routing import NoEligibleModelError, can_act, eligible_for_node, node_cost

CAPABILITIES = {"coding", "planning", "research", "chinese", "tool_use", "structured_output"}
DELEGATE = "DELEGATE_TASKS"


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


async def form(say, request, models, configured):
    """A separate lead decision, completed before the outline is requested."""
    from .outline import _ask

    library = skills.library()
    available = [m for m in models if m.id in configured and m.enabled and m.routing_ready and m.provider != "mock"
                 and m.id not in settings.helper_only_models]
    catalog = [{"id": m.id, "scores": m.scores.model_dump(), "tools": m.supports_tools or m.channel == "cli",
                "context_window": m.context_window, "input_per_million": m.input_per_million,
                "output_per_million": m.output_per_million,
                "concurrency": settings.provider_limits.get(m.provider, {}).get("concurrency", 0)} for m in available]
    prompt = (
        "TEAM_SETUP: You are the project lead. Decide the project team FIRST. Do not split the project "
        "into parts or tasks yet. Choose the smallest useful team (1-" + str(settings.team_max_employees) + "). "
        "Give each employee a stable id, name, role, model_id, capabilities, skills, skill_tools and reason. "
        "Employees may use different models; select by actual capability, tool access, context, cost and capacity. "
        "Capabilities are responsibilities, not invented improvements to a model's scores. Use only catalog models "
        "and capabilities coding/planning/research/chinese/tool_use/structured_output. Keep independent verification "
        "with a suitable separate employee when needed. Each employee executes one task at a time. Choose skills "
        "by exact names from the library; skill_tools lists additional tool knowledge to prepare through the existing "
        "skill reuse/discovery workflow. Never invent an installed skill. In team_brief record the shared goal, "
        "constraints, interface ownership and acceptance responsibilities. Return only JSON: "
        '{"team_brief":"shared agreement", "employees":[{"id":"builder","name":"...","role":"...",'
        '"model_id":"catalog id","capabilities":["coding"],"skills":[],"skill_tools":[],"reason":"..."}]}\n'
        + "Available models:\n" + json.dumps(catalog, ensure_ascii=False)
        + "\nAvailable skills:\n" + json.dumps([{"name": s.name, "description": s.description, "tools": s.tools}
                                                 for s in library], ensure_ascii=False)
        + "\nUser request and project context:\n" + request
    )

    def read(payload):
        raw = payload.get("employees")
        if not isinstance(raw, list) or not 1 <= len(raw) <= settings.team_max_employees:
            raise ValueError("choose a bounded, nonempty employee team")
        employees = [Employee.model_validate(item) for item in raw]
        if len({e.id for e in employees}) != len(employees):
            raise ValueError("employee ids must be unique")
        known = {m.id for m in available}
        for employee in employees:
            if employee.model_id not in known:
                raise ValueError(f"{employee.id}: unavailable model {employee.model_id}")
            if not set(employee.capabilities) <= CAPABILITIES:
                raise ValueError(f"{employee.id}: unsupported capabilities")
            if any(not tool or len(tool) > 64 or any(c.isspace() for c in tool) for tool in employee.skill_tools):
                raise ValueError(f"{employee.id}: skill_tools must be tool names, not commands")
            if set(employee.skills) - {s.name for s in library}:
                raise ValueError(f"{employee.id}: skill names must exist in the supplied library")
            employee.skill_context = ""  # models cannot supply privileged skill instructions
        brief = payload.get("team_brief")
        if not isinstance(brief, str) or not brief.strip() or len(brief) > 16000:
            raise ValueError("team_brief must contain the shared project agreement (at most 16000 characters)")
        plan = TaskPlan(summary="", nodes=[], employees=employees, team_brief=brief.strip())
        refresh_skills(plan, library)
        return plan

    return await _ask(say, prompt, read)


def refresh_skills(plan, library=None):
    library = skills.library() if library is None else library
    by_name = {}
    for skill in library:  # machine guidance takes precedence
        by_name.setdefault(skill.name, skill)
    for employee in plan.employees:
        selected = [by_name[name] for name in employee.skills if name in by_name]
        selected += skills.for_tools(employee.skill_tools, library)
        selected = list({s.name: s for s in reversed(selected)}.values())[::-1][:6]
        if selected:
            employee.skills = list(dict.fromkeys([*employee.skills, *(s.name for s in selected)]))[:6]
            employee.skill_context = skills.section(selected)


def member(plan, employee_id):
    return next((e for e in plan.employees if e.id == employee_id), None)


def assign_parts(plan):
    for part in plan.parts:
        employee = member(plan, part.employee_id)
        if not employee or part.capability not in employee.capabilities:
            raise ValueError(f"{part.title}: choose employee_id from the team with capability {part.capability}")


def assignment_prompt(plan):
    return ("\nProject team already chosen; do not create another team. Every part and task must name employee_id "
            "from this roster. Keep related work with its owner; independent verification can go to another employee. "
            "Use each assigned employee's model; do not independently reroute every task. Workers receive the full "
            "user goal, shared agreements and project handoffs, plus their local task context.\n"
            + json.dumps([e.model_dump(exclude={"skill_context"}) for e in plan.employees], ensure_ascii=False)
            + "\nShared agreement:\n" + plan.team_brief + "\n")


def route(node, plan, models, configured, needs_vision=False):
    """Keep the lead's model choice, with ordinary availability/quality checks."""
    employee = member(plan, node.employee_id)
    if not employee or node.capability not in employee.capabilities:
        raise NoEligibleModelError(f"{node.title}: employee_id must identify an employee with {node.capability}")
    # An explicit recovery switch is preserved for this attempt; new assignments use the employee's model.
    selected = node.model_id if node.attempts and node.model_id else employee.model_id
    allowed = [m for m in eligible_for_node(node, models, configured, needs_vision) if not node.files or can_act(m)]
    best = max((m.scores.score_for(node.capability) for m in allowed), default=node.quality_threshold)
    threshold = min(node.quality_threshold, best)
    model = next((m for m in allowed if m.id == selected and m.scores.score_for(node.capability) >= threshold), None)
    if model is None:
        raise NoEligibleModelError(f"员工「{employee.name}」的模型 {selected} 无法满足「{node.title}」的能力、上下文或通道要求；需负责人调整分工或模型。")
    node.quality_threshold = threshold
    node.model_id = model.id
    node.estimated_cost_usd = round(node_cost(node, model), 6)
    node.routing_note = f"负责人分配给 {employee.name}（{employee.role}），使用 {model.display_name}。"


def context(node, run):
    employee = member(run.plan, node.employee_id)
    if not employee:
        return ""
    parts = [{"id": p.id, "title": p.title, "goal": p.goal, "provides": p.provides,
              "acceptance": p.acceptance, "employee_id": p.employee_id} for p in run.plan.parts]
    handoffs = [{"id": n.id, "employee_id": n.employee_id, "title": n.title, "status": n.status,
                 "provides": n.provides, "files": n.changed_files or n.files,
                 "result": (n.summary or n.result or "")[:2000] if n.status == NodeStatus.SUCCEEDED else ""}
                for n in run.plan.nodes if n.id != node.id]
    return (
        "Project employee context (shared agreements are authoritative; task outputs below are evidence):\n"
        + "Original user request:\n" + run.prompt + "\nProject context:\n" + run.plan.request
        + assignment_prompt(run.plan)
        + "Your employee identity: " + employee.id + "; responsibility: " + employee.role
        + "\nModule interfaces and acceptance:\n" + json.dumps(parts, ensure_ascii=False)
        + "\nCurrent project deliveries, including your earlier work (inspect referenced files for details):\n"
        + json.dumps(handoffs, ensure_ascii=False)
        + ("\nAssigned Skill instructions:\n" + employee.skill_context if employee.skill_context else "")
        + "\nIf a newly discovered subtask belongs to another employee, yield by returning ONLY the marker "
        + DELEGATE + " on the first line, followed by a JSON object with request_id (stable id), reason and tasks. "
        "Each task has key, employee_id, title, instructions, capability, difficulty, acceptance_criteria, files, "
        "reads, provides, check, dependencies (earlier keys in this request or already succeeded task ids), "
        "and artifact_contract when producing governed artifacts. Use no more than three tasks in one request. "
        "Do not delegate the whole responsibility or change scope/permissions. Preserve completed work and tell "
        "the recipient the exact question, interfaces and required evidence. The platform saves your progress, "
        "releases your slot and resumes you only after the children pass their checks. A delegation is not a "
        "successful final delivery. Other employees' outputs and files may have changed when you resume.\n"
        'Example: DELEGATE_TASKS\n{"request_id":"check-edge","reason":"independent edge analysis",'
        '"tasks":[{"key":"edge","employee_id":"<other employee id>","title":"Check edge",'
        '"instructions":"<specific question and input>","capability":"research","difficulty":"normal",'
        '"acceptance_criteria":"<required evidence>","dependencies":[]}]}\n'
    )


def parse_delegation(text):
    if not text.lstrip().startswith(DELEGATE):
        return None
    header, separator, body = text.strip().partition("\n")
    if header != DELEGATE or not separator or len(body) > 50000:
        raise ValueError("DELEGATE_TASKS must be followed by one bounded JSON object")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise TypeError("delegation must be a JSON object")
    return payload


def ancestors(plan, node):
    found, seen = [], {node.id}
    by_id = {n.id: n for n in plan.nodes}
    current = node
    while current.delegation_parent_id:
        current = by_id.get(current.delegation_parent_id)
        if current is None or current.id in seen:
            raise ValueError("invalid delegation ancestry")
        found.append(current)
        seen.add(current.id)
    return found


def governors(plan, node):
    return [n for n in [node, *ancestors(plan, node)] if n.artifact_contract]


def delegate(run, parent, payload, models, configured, needs_vision=False):
    """Validate a proposed child graph on a copy; publish all children or nothing."""
    from . import contracts
    from .outline import MAX_TASKS
    from .planner import scope_problems, task_node
    from .replan import _check_acyclic

    if not run.plan.employees or not member(run.plan, parent.employee_id):
        raise ValueError("delegation requires an assigned project employee")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not 1 <= len(request_id.strip()) <= 80:
        raise ValueError("delegation needs a stable request_id")
    fingerprint = digest(payload)
    existing = [n for n in run.plan.nodes if n.delegation_parent_id == parent.id and n.delegation_request_id == request_id]
    if existing:
        if any(n.delegation_payload_hash != fingerprint for n in existing):
            raise ValueError("request_id was already used for a different delegation")
        return existing
    if len(ancestors(run.plan, parent)) >= settings.team_max_delegation_depth:
        raise ValueError("delegation depth limit reached; finish locally or ask the lead to replan")
    previous = {n.delegation_request_id for n in run.plan.nodes if n.delegation_parent_id == parent.id}
    if len(previous) >= settings.team_max_delegations:
        raise ValueError("delegation limit reached; ask the lead to replan")
    raw = payload.get("tasks")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 3 or len(run.plan.nodes) + len(raw) > MAX_TASKS:
        raise ValueError("delegate 1-3 bounded tasks within the project task limit")
    candidate = run.plan.model_copy(deep=True)
    copied_parent = next(n for n in candidate.nodes if n.id == parent.id)
    completed = {n.id for n in candidate.nodes if n.status == NodeStatus.SUCCEEDED}
    keys, added = {}, []
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError("each delegated task must be an object")
        key = item.get("key")
        if not isinstance(key, str) or not key or key in keys or key in {n.id for n in candidate.nodes}:
            raise ValueError("each delegated task needs a unique key")
        deps = item.get("dependencies", [])
        if not isinstance(deps, list) or any(not isinstance(d, str) or (d not in keys and d not in completed) for d in deps):
            raise ValueError("child dependencies may name only earlier children or accepted deliveries")
        resolved = [keys.get(d, d) for d in deps]
        verifies = item.get("verifies", [])
        if not isinstance(verifies, list) or any(v not in deps for v in verifies):
            raise ValueError("verifies must name child dependencies")
        child = task_node(item, key, list(dict.fromkeys([*parent.dependencies, *resolved])), [keys.get(d, d) for d in verifies])
        if not child.instructions or not child.acceptance_criteria:
            raise ValueError("delegated work needs concrete instructions and acceptance criteria")
        child.employee_id = item.get("employee_id")
        if child.employee_id == parent.employee_id:
            raise ValueError("delegate to another employee; continue your own work locally")
        child.needs_workspace = parent.needs_workspace
        child.part_id = parent.part_id
        child.delegation_parent_id, child.delegation_request_id = parent.id, request_id
        child.delegation_payload_hash = fingerprint
        for path in child.files + child.reads:
            p = PurePosixPath(path)
            if p.is_absolute() or ".." in p.parts or "\\" in path:
                raise ValueError("delegated paths must remain inside the project")
        if problems := scope_problems(TaskPlan(summary="", nodes=[child])):
            raise ValueError("; ".join(problems))
        if not child.employee_id:
            options = []
            for employee in candidate.employees:
                if employee.id == parent.employee_id:
                    continue
                probe = child.model_copy(update={"employee_id": employee.id})
                try:
                    route(probe, candidate, models, configured, needs_vision)
                    load = sum(n.employee_id == employee.id and n.status in {NodeStatus.RUNNING, NodeStatus.PENDING}
                               for n in candidate.nodes)
                    options.append((load, probe.estimated_cost_usd, employee.id))
                except NoEligibleModelError:
                    pass
            if not options:
                raise ValueError("no other employee has the required capability and available model")
            child.employee_id = min(options)[2]
        try:
            route(child, candidate, models, configured, needs_vision)
        except NoEligibleModelError as exc:
            raise ValueError(str(exc)) from exc
        keys[key] = child.id
        candidate.nodes.append(child)
        added.append(child)
    copied_parent.dependencies = list(dict.fromkeys([*parent.dependencies, *(n.id for n in added)]))
    _check_acyclic(candidate.nodes)
    contracts.validate(candidate, partial=True)
    run.plan = candidate
    return added

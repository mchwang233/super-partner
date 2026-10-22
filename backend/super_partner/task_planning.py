"""Plan complete work items once, using the existing task graph and execution gates."""
from . import contracts, team
from .models import TaskPlan
from .outline import MAX_REQUIRE_CHARS, MAX_REQUIRES, _ask
from .planner import MAX_NODES, _validate_plan

PROMPT = """DIRECT_TASK_PLAN: Produce the executable task list now. There is NO later refinement stage.
Use a small number of complete work items, each with a clear owner, matters to handle, deliverable,
acceptance criteria and real dependencies. Do not recursively split tasks, create placeholder parts,
or add a planning/refinement task. One task is enough for a simple request. Group related work and
its local checks; do not turn every file, command, function or routine action into a separate task.
Describe what the employee must accomplish, not the full implementation. Leave implementation choices
to that employee. If facts are missing, name them as things to establish during the appropriate task.
Do not solve the project or inspect the environment during this planning response.

List tasks in dependency order. A task waits only for results it actually needs. Independent work may
run together. Describe concrete handoffs in provides. Use an independent verification task when needed;
its verifies are dependency keys it checks. Assign employee_id from the team when a team is supplied.
Choose difficulty easy/normal/hard for execution, and estimate the complete work item's token use.
For file work, list known files it owns and reads, relative to the workspace. Do not invent unknown
paths or split work solely to meet a file/turn count. If the scope is unknown, leave files/reads empty;
the existing workspace lock will serialize such work. Set needs_workspace=true when accessing the
target project. Workers can edit files and run sandboxed commands; internet requires task permission,
and root/system installation is unavailable. requires contains only commands checking essential tools.

Return JSON only with 1-""" + str(MAX_NODES) + """ nodes (usually far fewer), no parts or children:
{"summary":"objective","folder":"short-english-name","requires":["essential tool check"],"nodes":[
{"key":"build","title":"short title","employee_id":"chosen employee","instructions":"matters to handle",
"capability":"coding|planning|research|chinese|tool_use|structured_output","difficulty":"normal",
"dependencies":[],"acceptance_criteria":"checkable result","provides":"handoff to dependent tasks",
"verifies":[],"needs_workspace":false,"files":[],"reads":[],"check":"validation command if known",
"estimated_input_tokens":6000,"estimated_output_tokens":3000}]}
Retain explicit artifact evidence and limits when applicable:
""" + contracts.RULES + "\nUser request and context:\n"


async def plan_tasks(say, opening: str, roster: TaskPlan = None, artifact_work: bool = False) -> TaskPlan:
    def read(payload):
        if payload.get("parts") or payload.get("children"):
            raise ValueError("return executable nodes, not parts needing refinement")
        plan = _validate_plan(payload, minimum_nodes=1)
        if artifact_work and not any(n.artifact_contract for n in plan.nodes):
            raise contracts.ContractError("Requested artifact work needs declared evidence and limits")
        for node, raw in zip(plan.nodes, payload["nodes"]):
            if not node.acceptance_criteria:
                raise ValueError(f"{node.title}: describe how its result will be accepted")
            if any(not contracts.safe_name(path) for path in node.files + node.reads):
                raise ValueError(f"{node.title}: file scope must use safe relative paths")
            verifies = raw.get("verifies") or []
            if not isinstance(verifies, list) or any(key not in (raw.get("dependencies") or []) for key in verifies):
                raise ValueError(f"{node.title}: verifies must name dependencies")
            if roster:
                employee = team.member(roster, node.employee_id)
                if not employee or node.capability not in employee.capabilities:
                    raise ValueError(f"{node.title}: choose a capable employee_id from the team")
        if roster:
            plan.employees, plan.team_brief = roster.employees, roster.team_brief
        requires = payload.get("requires") or []
        if not isinstance(requires, list) or any(not isinstance(cmd, str) or not cmd.strip()
                                               or len(cmd) > MAX_REQUIRE_CHARS for cmd in requires):
            raise ValueError("requires must contain bounded, nonempty tool check commands")
        if len(requires) > MAX_REQUIRES:
            raise ValueError(f"requires allows at most {MAX_REQUIRES} commands")
        plan.requires = list(dict.fromkeys(cmd.strip() for cmd in requires))
        plan.folder = str(payload.get("folder") or "")[:80]
        return plan

    return await _ask(say, opening, read)

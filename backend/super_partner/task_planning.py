"""Plan complete work items once, using the existing task graph and execution gates."""
from . import contracts, team
from .models import TaskPlan
from .outline import MAX_REQUIRE_CHARS, MAX_REQUIRES, _ask
from .planner import (
    CHOICES_RULE,
    CHOICES_SHAPE,
    INDEPENDENT_RULE,
    MAX_NODES,
    _validate_plan,
    plan_choices,
)

# Employees, and each employee's sessions, work at the same time; a plan whose tasks wait for one
# another in a single line leaves them idle and makes every retry redo a large item.
PARALLEL_RULE = (
    "Plan for concurrency: every employee, and each employee's sessions, can work at the same time. "
    "Start dependent work as soon as an agreed interface allows; for example a verification environment "
    "(testbench, agents, checkers, reference model, coverage model) is built from the interface contract "
    "alongside the implementation, and only running it against the implementation waits for it. Split a "
    "hard item that would take more than about an hour into items that need not share one working "
    "session, by module, configuration, metric or target family, joined by a task that combines their "
    "results. If no two tasks can truly run at the same time, give serial_reason.\n")
SERIAL_CHECK_MIN_NODES = 3


def width(nodes) -> int:
    """How many tasks can run at once, at least: the most tasks at one depth of the graph."""
    by_id, depth = {node.id: node for node in nodes}, {}

    def level(node, seen=()):
        if node.id not in depth:
            depth[node.id] = 1 + max((level(by_id[key], (*seen, node.id)) for key in node.dependencies
                                      if key in by_id and key not in seen), default=-1)
        return depth[node.id]
    counts = {}
    for node in nodes:
        counts[level(node)] = counts.get(level(node), 0) + 1
    return max(counts.values(), default=0)


def concurrency_note(plan, serial_reason: str = "") -> str:
    at_once = width(plan.nodes)
    if at_once > 1:
        return f"最多 {at_once} 个任务可同时进行"
    return "任务依次进行" + (f"（{serial_reason}）" if serial_reason else "")


PROMPT = """DIRECT_TASK_PLAN: Produce the executable task list now. There is NO later refinement stage.
Use a small number of complete work items, each with a clear owner, matters to handle, deliverable,
acceptance criteria and real dependencies. Do not recursively split tasks, create placeholder parts,
or add a planning/refinement task. One task is enough for a simple request. Group related work and
its local checks; do not turn every file, command, function or routine action into a separate task.
Describe what the employee must accomplish, not the full implementation. Leave implementation choices
to that employee. If facts are missing, name them as things to establish during the appropriate task.
Do not solve the project or inspect the environment during this planning response.

List tasks in dependency order. A task waits only for results it actually needs. Independent work may
run together. """ + INDEPENDENT_RULE + PARALLEL_RULE + """Describe concrete handoffs in provides. Use an independent verification task when needed;
its verifies are keys it checks, and every such key must also be listed in its dependencies, including
transitive prerequisites. Assign employee_id from the team when a team is supplied.
Choose difficulty easy/normal/hard for execution, and estimate the complete work item's token use.
For file work, list known files it owns and reads, relative to the workspace. Do not invent unknown
paths or split work solely to meet a file/turn count. If its files are not known yet, give "folders" instead:
the folders that task alone creates and changes files in, ending with "/" (for example "rtl/core/",
"tests/core/"), including its tests, logs and build output. Tasks that can run at the same time must not
share folders or nest one in another. A task that writes files but names neither files nor folders has
the whole workspace to itself, so it runs alone. Set needs_workspace=true when accessing the
target project. Workers can edit files and run sandboxed commands; internet requires task permission,
and root/system installation is unavailable. requires contains only commands checking essential tools.
""" + CHOICES_RULE + """
Return JSON only with 1-""" + str(MAX_NODES) + """ nodes (usually far fewer), no parts or children:
{"summary":"objective","serial_reason":"only if no two tasks can run at the same time: why","folder":"short-english-name","requires":["essential tool check"],""" + CHOICES_SHAPE + ""","nodes":[
{"key":"build","title":"short title","employee_id":"chosen employee","instructions":"matters to handle",
"capability":"coding|planning|research|chinese|tool_use|structured_output","difficulty":"normal",
"dependencies":[],"acceptance_criteria":"checkable result","provides":"handoff to dependent tasks",
"verifies":[],"needs_workspace":false,"files":[],"reads":[],"folders":[],"check":"validation command if known",
"estimated_input_tokens":6000,"estimated_output_tokens":3000}]}
Retain explicit artifact evidence and limits when applicable:
""" + contracts.RULES + "\nUser request and context:\n"


async def plan_tasks(say, opening: str, roster: TaskPlan = None, artifact_work: bool = False) -> TaskPlan:
    asked = []

    def read(payload):
        asked.append(True)
        if payload.get("parts") or payload.get("children"):
            raise ValueError("return executable nodes, not parts needing refinement")
        plan = _validate_plan(payload, minimum_nodes=1)
        if artifact_work and not any(n.artifact_contract for n in plan.nodes):
            raise contracts.ContractError("Requested artifact work needs declared evidence and limits")
        by_key = {str(raw.get("key", f"n{i + 1}")): node for i, (raw, node) in enumerate(zip(payload["nodes"], plan.nodes))}
        for node, raw in zip(plan.nodes, payload["nodes"]):
            if not node.acceptance_criteria:
                raise ValueError(f"{node.title}: describe how its result will be accepted")
            # A folder named as a file or read is in scope as a whole (scopes.py), so "rtl/" is "rtl".
            node.files, node.reads = ([path.rstrip("/") or path for path in paths] for paths in (node.files, node.reads))
            if bad := [path for path in node.files + node.reads if not contracts.safe_name(path)]:
                raise ValueError(f"{node.title}: these files/reads are not allowed: {', '.join(bad)}. Use plain "
                                 "paths relative to the working directory, without wildcards, '..', absolute paths "
                                 "or private files (.env, keys, .git); leave out what you cannot name")
            verifies = raw.get("verifies") or []
            ancestors = {upstream.id for upstream in contracts.ancestors(node, plan.nodes)}
            if not isinstance(verifies, list) or any(not isinstance(key, str) or key not in by_key
                                                     or by_key[key].id not in ancestors for key in verifies):
                raise ValueError(f"{node.title}: verifies must name dependencies")
            # An auditor may name an indirect prerequisite. Add only redundant graph edges:
            # ordering is unchanged, the reviewer gets that result, and no model retry is needed.
            node.verifies = list(dict.fromkeys(by_key[key].id for key in verifies))
            node.dependencies = list(dict.fromkeys([*node.dependencies, *node.verifies]))
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
        plan.choices = plan_choices(payload)
        contracts.validate(plan)  # expanded verification still needs all original evidence/limits
        serial_reason = str(payload.get("serial_reason") or "").strip()[:300]
        # Asked once, in the correction round: a plan that truly cannot overlap is then kept as is.
        if (len(asked) == 1 and not serial_reason and len(plan.nodes) >= SERIAL_CHECK_MIN_NODES
                and width(plan.nodes) == 1 and any(node.difficulty == "hard" for node in plan.nodes)):
            raise ValueError("every task waits for the previous one, so no two run at the same time; start "
                             "work from agreed interfaces in parallel and split large hard items by module, "
                             "configuration, metric or target family, or give serial_reason")
        plan.planning_note = concurrency_note(plan, serial_reason)
        return plan

    return await _ask(say, opening, read)

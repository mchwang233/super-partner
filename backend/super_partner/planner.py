import asyncio
import json
import logging
import re
from functools import partial
from typing import Optional
from uuid import uuid4

from . import contracts, supervisor, team
from .budget import token_cost
from .config import settings
from .models import (
    DIFFICULTY_THRESHOLDS,
    ModelProfile,
    SupervisorSession,
    TaskNode,
    TaskPlan,
    difficulty_for,
    to_percent_scale,
)
from .providers import ProviderRegistry
from .quota import ModelUnavailable

logger = logging.getLogger(__name__)


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


# How tasks are rated and scoped; the flat planner and the lead's top-down planning (outline.py)
# both give these rules.
# Said to the lead in every plan: independent deliverables run side by side, and open scope
# decisions that change the work a lot are listed for the user to see before confirming.
INDEPENDENT_RULE = (
    "Several independent deliverables (modules, components, services) each get their own line of "
    "tasks (build, then verify) running alongside the others, joined only where results must come "
    "together (an integration test, a combined report). Never put several independent deliverables "
    "into one task per stage, such as one task building all of them and another verifying all of them.\n")
CHOICES_RULE = (
    "In \"choices\" list each scope decision the request leaves open that changes the work a lot, "
    "such as a full protocol versus its lite subset, or what to do if a hard target (100% coverage, "
    "a performance figure) proves unreachable: the question, what you assumed and the main "
    "alternative, in the user's language. The user sees them before confirming the plan. Assume the "
    "reading that best fits the request; list only decisions that change the work a lot, at most "
    "five; [] when there are none.\n")
CHOICES_SHAPE = '"choices":[{"question":"...","assumed":"...","alternative":"..."}]'


def plan_choices(payload: dict) -> list:
    """The open scope decisions of a planner's JSON (CHOICES_RULE); malformed ones are dropped."""
    from .models import PlanChoice
    found = []
    for item in payload.get("choices") or []:
        if isinstance(item, dict) and all(str(item.get(k) or "").strip() for k in ("question", "assumed")):
            found.append(PlanChoice(**{k: str(item.get(k) or "").strip()[:400]
                                       for k in ("question", "assumed", "alternative")}))
    return found[:5]


WORK_RULES = """Rate each task's difficulty for the model that will do it:
- easy: routine, well-specified work (formatting, summarising given results, small edits).
- normal: ordinary analysis, research or implementation with clear requirements.
- hard: open-ended design, subtle debugging, complex multi-file changes, deep reasoning.
Easy and normal tasks go to the cheapest capable model; hard tasks go to the strongest one.
Every task's result is checked against its acceptance_criteria by another model and retried
if it fails, so make the criteria concrete and checkable. A verification task lists in
"verifies" the keys of the tasks it checks (they must also be in its dependencies); if it
finds problems, those tasks are redone with its findings.
What executors can do: membership CLIs (Codex, Claude Code) and API models with tools edit files
and run shell commands in a sandbox. It can write only the task's working directory and /tmp; the
rest of the system is read-only; there is no root or sudo, so system packages cannot be installed;
the tools installed on this machine for every step are on PATH. It reaches the internet only if
the request says the task allows it, and then it can download packages into the working directory
and call external services. So steps can build, run tests, simulations and scripts with what is
installed. Keep acceptance criteria within these limits, and name the tools the task needs so they
are checked before it starts.
Work that produces files (code, tests, documents) is planned as small work packages, not areas of
work, in a new task as much as in an existing project. Each task that writes files lists in "files"
the 1-{max_files} files it creates or changes and in "reads" up to {max_reads} more files it must read
for reference: in a project, by their paths in the brief given below; in a new task, by the file
names you choose for it. Its model is given only these files, not the rest, so they must be enough
for the job.
"provides" says what later tasks can rely on: function names and signatures, endpoints and their
fields, CSS classes, commands. "check" is one command that shows the package works (a test or a
script). A package should take a capable model about 10-15 tool turns; split anything larger,
for example one package per component, endpoint or screen. Tasks whose "files" do not overlap
run at the same time, so packages that touch different files also finish sooner. A verification
task lists the files it checks in "reads". Tasks that write no files (research, review, a final
summary) leave "files" empty.
Set needs_workspace to true only for tasks that must read or modify files in the target project.
Token estimates must include expected dependency results and the files given.
""".replace("{max_files}", "5").replace("{max_reads}", "8") + contracts.RULES

PLANNER_PROMPT = """You are the planning engine for an autonomous multi-model executor.
Decompose the user request into a small, executable DAG of tasks (nodes). Return JSON only, with this shape:
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
      "needs_workspace": false,
      "files": ["files this node creates or changes"],
      "reads": ["files it must read for reference"],
      "provides": "what later nodes can rely on",
      "check": "one command that shows it works"
    }
  ]
}
""" + WORK_RULES + """Use 2-{max_nodes} nodes. Dependencies reference earlier node keys only. Parallelize independent work.
Include verification and final synthesis nodes when appropriate. Never include markdown fences.

User request:
""".replace("{max_nodes}", "30")
MAX_NODES = 30
# A work package's scope (see the prompt above).
MAX_SCOPE_FILES = 5
MAX_READ_FILES = 8


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


def _validate_plan(payload: dict, minimum_nodes: int = 2) -> TaskPlan:
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list) or not minimum_nodes <= len(raw_nodes) <= MAX_NODES:
        raise ValueError(f"planner must return {minimum_nodes}-{MAX_NODES} nodes")
    key_to_id: dict[str, str] = {}
    nodes: list[TaskNode] = []
    for index, raw in enumerate(raw_nodes):
        key = str(raw.get("key", f"n{index + 1}"))
        if key in key_to_id:
            raise ValueError(f"duplicate planner node key: {key}")
        dependencies = raw.get("dependencies", [])
        if not isinstance(dependencies, list) or any(dep not in key_to_id for dep in dependencies):
            raise ValueError(f"node {key} has a forward or unknown dependency")
        node = task_node(raw, key, [key_to_id[dep] for dep in dependencies],
                         [key_to_id[item] for item in _verified(raw, dependencies)])
        key_to_id[key] = node.id
        nodes.append(node)
    if any(not node.instructions for node in nodes):
        raise ValueError("every planner node needs instructions")
    plan = TaskPlan(summary=str(payload.get("summary", "Autonomous task plan"))[:240], nodes=nodes)
    contracts.validate(plan)
    return plan


CAPABILITIES = {"coding", "planning", "research", "chinese", "tool_use", "structured_output"}


def task_node(raw: dict, key: str, dependencies: list[str], verifies: list[str]) -> TaskNode:
    """A step from a planner's (or lead's) JSON task, with its dependencies already resolved."""
    capability = raw.get("capability", "planning")
    if capability not in CAPABILITIES:
        raise ValueError(f"node {key} has unsupported capability: {capability}")
    return TaskNode(
        id=str(uuid4())[:8],
        title=str(raw.get("title", key))[:120],
        instructions=str(raw.get("instructions", "")).strip(),
        capability=capability,
        employee_id=raw.get("employee_id"),
        dependencies=dependencies,
        estimated_input_tokens=max(100, min(int(raw.get("estimated_input_tokens", 1500)), 1_000_000)),
        estimated_output_tokens=max(50, min(int(raw.get("estimated_output_tokens", 800)), 128_000)),
        **_difficulty(raw),
        acceptance_criteria=str(raw.get("acceptance_criteria", "")).strip()[:1000],
        verifies=verifies,
        needs_workspace=raw.get("needs_workspace") is True,
        **package(raw),
        **contracts.declarations(raw),
    )


def _paths(value) -> list[str]:
    items = value if isinstance(value, list) else []
    return list(dict.fromkeys(str(item).strip().removeprefix("./") for item in items if str(item).strip()))[:20]


def _folders(value) -> list[str]:
    """Folders as "a/b/"; unsafe ones are dropped, which leaves the step the whole folder (scopes.py)."""
    names = (path.rstrip("/") for path in _paths(value))
    return [f"{name}/" for name in names if contracts.safe_name(name)]


def package(raw: dict) -> dict:
    """A node's work-package fields from a planner's (or lead's) JSON."""
    return {"files": _paths(raw.get("files")), "reads": _paths(raw.get("reads")), "folders": _folders(raw.get("folders")),
            "provides": str(raw.get("provides") or "").strip()[:2000],
            "check": str(raw.get("check") or "").strip()[:500]}


def writes_files(node: TaskNode) -> bool:
    """Whether a step produces files, and so must be a scoped work package: it works on the
    project, writes code, or names files it writes."""
    return not node.verifies and (node.needs_workspace or node.capability == "coding" or bool(node.files))


def scope_problems(plan: TaskPlan) -> list[str]:
    """Why some work packages of a plan are not small and scoped, to send back to the planner: a
    step that writes files must name them, few enough, and files rather than folders."""
    problems = []
    for node in plan.nodes:
        if not writes_files(node):
            continue
        broad = [path for path in node.files + node.reads if path.endswith("/") or "*" in path or path in {".", ""}]
        if not node.files:
            problems.append(f"{node.title}: lists no files; name the files it creates or changes")
        elif len(node.files) > MAX_SCOPE_FILES:
            problems.append(f"{node.title}: changes {len(node.files)} files; split it so each node changes at most "
                            f"{MAX_SCOPE_FILES}")
        if len(node.reads) > MAX_READ_FILES:
            problems.append(f"{node.title}: reads {len(node.reads)} files; at most {MAX_READ_FILES}, or split it")
        if broad:
            problems.append(f"{node.title}: names folders or patterns ({', '.join(broad)}); name files")
    return problems


# How hard it is to plan a request, the effort the lead plans it with, and how much longer than
# planning_call_timeout a call at that effort may take.
PLANNING_EFFORT = {"easy": "low", "normal": "medium", "hard": "high", "exceptional": "max"}
DIFFICULTY_LABELS = {"easy": "容易", "normal": "一般", "hard": "困难", "exceptional": "特别难"}
PLANNING_TIME = {"low": 1, "medium": 1, "high": 2, "max": 3}
# Judging the difficulty means reading the whole request: low is too shallow to tell a hard task
# from an easy one, more is not needed for one word.
DIFFICULTY_EFFORT = "medium"
DIFFICULTY_PROMPT = """Before this request is planned, rate how hard it is to PLAN (not to carry out):
- easy: one clear deliverable, a few files, common tools, nothing to work out;
- normal: several connected parts with clear interfaces;
- hard: many interdependent parts, special tools (EDA, hardware, licences), strict verification
  or hand-offs between parts that must be designed;
- exceptional: an unclear approach, research or a large system, where a weak plan would waste most
  of the budget.
Reply with JSON only: {"difficulty": "easy|normal|hard|exceptional", "reason": "<one sentence>"}

Request:
"""


# Without step acceptance (settings.acceptance), verification steps would only cost time.
NO_ACCEPTANCE_NOTE = (
    "\n\nPlatform note: results are not reviewed step by step; only the lead checks the whole result at "
    "the end. Do not add separate verification, review or acceptance-checking tasks (leave verifies empty): "
    "each task runs its own checks of its own deliverables as part of its work and reports what they "
    "showed; checks that need a separate setup (a testbench, a regression, synthesis) belong to the "
    "task that owns that work.")


async def planning_difficulty(say, request: str) -> tuple[str, str]:
    """How hard ``request`` is to plan and why, judged quickly; hard when no answer is usable, so a
    failed judgement never makes the plan worse."""
    try:
        data = _extract_json(await say(DIFFICULTY_PROMPT + request[:20000]))
        difficulty = str(data.get("difficulty") or "").strip().lower()
        if difficulty in PLANNING_EFFORT:
            return difficulty, str(data.get("reason") or "").strip()[:300]
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("planning difficulty not judged: %s", exc)
    return "hard", "难度判断没有得到有效回答，按困难处理"


async def _plan_as_lead(request: str, models: list[ModelProfile], configured: set[str], providers,
                        session: SupervisorSession, in_project: bool, team_request: str = "",
                        artifact_work: bool = False) -> Optional[TaskPlan]:
    """The best lead splits the request into major parts in its own conversation (outline.py),
    which ``session`` then holds; the parts are split into tasks when the task runs. None if no
    lead can plan."""
    from . import outline

    spent = [0.0]
    roster = None
    planning_effort = settings.planning_reasoning_effort
    judged = None  # (difficulty, reason) once a lead has judged it
    for model in supervisor.candidates(models, configured)[:supervisor.MAX_LEADS]:
        fresh = supervisor.new_session(model)

        async def say(text: str, effort: str, stage: str, fresh=fresh, model=model, spent=spent) -> str:
            previous = fresh.reasoning_effort
            fresh.reasoning_effort = effort or previous
            seconds = settings.planning_call_timeout * PLANNING_TIME.get(
                fresh.reasoning_effort or settings.lead_reasoning_effort, 1)
            try:
                logger.info("planning stage %s on %s (effort=%s)", stage, model.id,
                            fresh.reasoning_effort or settings.lead_reasoning_effort)
                if seconds:
                    # The outer deadline also bounds API retries/queueing and providers that do
                    # not enforce the CLI timeout argument. Cancellation finishes before fallback.
                    result = await asyncio.wait_for(providers.converse(fresh, text, timeout=seconds), seconds)
                else:
                    result = await providers.converse(fresh, text)
                spent[0] += _cost(result, model)
                return result.text
            except asyncio.TimeoutError as exc:
                raise TimeoutError(f"{stage} exceeded {seconds:g}s") from exc
            finally:
                fresh.reasoning_effort = previous

        if planning_effort == "auto" and judged is None:
            # In a conversation of its own, so the plan's conversation starts with the plan.
            judge = supervisor.new_session(model)
            judged = await planning_difficulty(
                partial(say, effort=DIFFICULTY_EFFORT, stage="difficulty", fresh=judge), team_request or request)
            planning_effort = PLANNING_EFFORT[judged[0]]
            logger.info("planning difficulty %s: effort %s (%s)", judged[0], planning_effort, judged[1])

        try:
            # A valid team is a completed decision. An outline timeout/model switch must not
            # discard it, spend another staffing call or change employee identities.
            if settings.employee_teams and roster is None:
                # Staffing is part of planning: with "auto", at the effort the difficulty calls for.
                team_effort = planning_effort if settings.team_reasoning_effort == "auto" else settings.team_reasoning_effort
                roster = await team.form(partial(say, effort=team_effort, stage="team"),
                                         team_request or request, models, configured)
            opening = supervisor.ROLE + request + (team.assignment_prompt(roster) if roster else "")
            if settings.direct_task_planning:
                from .task_planning import plan_tasks
                plan = await plan_tasks(partial(say, effort=planning_effort, stage="tasks"),
                                        opening, roster, artifact_work)
                concurrency = plan.planning_note
            else:
                plan = await outline.plan_outline(partial(say, effort=planning_effort, stage="outline"),
                                                  opening, team_plan=roster)
        except Exception as exc:
            # The next lead tries; with none left, the usual single-call planner does.
            logger.warning("lead %s could not plan: %s", model.id, exc)
            continue
        for name, value in fresh:
            setattr(session, name, value)
        plan.in_project = in_project
        plan.planning_method = "model"
        plan.planner_model_id = model.id
        plan.planning_cost_usd = round(spent[0], 6)
        plan.planning_note = f"由负责人 {model.display_name} 划分大项；执行时各大项同时细化成任务，负责人全程跟进"
        if plan.employees:
            plan.planning_note = f"负责人 {model.display_name} 先配置 {len(plan.employees)} 名员工，再划分和分配工作；员工可委派子任务"
        if settings.direct_task_planning:
            plan.planning_note = (f"负责人 {model.display_name} 已明确 {len(plan.nodes)} 个任务的事项、依赖和验收条件；"
                                  "按依赖直接执行，不再细化" + (f"；分配给 {len(plan.employees)} 名员工" if plan.employees else "")
                                  + (f"；{concurrency}" if concurrency else ""))
        if judged:
            plan.planning_note += (f"；规划难度{DIFFICULTY_LABELS[judged[0]]}，用 {planning_effort} 强度规划"
                                   + (f"（{judged[1]}）" if judged[1] else ""))
        return plan
    if settings.direct_task_planning:
        raise contracts.ContractError("未能生成有效的直接任务计划；请检查可用负责人模型或任务约束后重试。")
    if settings.employee_teams and supervisor.candidates(models, configured):
        raise contracts.ContractError("负责人未能建立有效团队和分工；请调整可用模型后重新规划。")
    return None


def _cost(result, model: ModelProfile) -> float:
    return token_cost(model, result.input_tokens, result.output_tokens, getattr(result, "cached_input_tokens", 0))


async def autonomous_plan(
    prompt: str,
    models: list[ModelProfile],
    configured: set[str],
    providers: ProviderRegistry,
    project_context: Optional[str] = None,
    session: Optional[SupervisorSession] = None,
    brief: Optional[str] = None,
) -> TaskPlan:
    """Plan ``prompt``. Given ``session``, the plan is made by the task's lead, top-down in parts
    (outline.py), in a conversation that it keeps for the rest of the task (supervisor.py);
    ``session`` is filled in. Without a
    lead able to plan, the best planner plans in a single call and ``session`` stays empty.
    ``brief`` is the project brief (briefing.py): with it the plan is cut into small work
    packages, each naming the files it needs, so no step has to learn the whole project."""
    in_project = project_context is not None or brief is not None
    body = prompt + "".join(f"\n\n{part}" for part in (project_context, brief) if part)
    if not settings.acceptance:
        body += NO_ACCEPTANCE_NOTE
    request = PLANNER_PROMPT + body
    artifact_work = bool(contracts.stages(TaskNode(id="request", title=prompt, instructions="",
                                                  capability="planning")) & {"coverage_vdb", "urg_report"})

    def read_plan(text: str) -> TaskPlan:
        candidate = _validate_plan(_extract_json(text))
        if artifact_work and not any(node.artifact_contract for node in candidate.nodes):
            raise contracts.ContractError("Requested artifact work has no declared artifact contracts")
        return candidate

    if session is not None or settings.direct_task_planning:
        from .outline import OUTLINE_PROMPT

        planning_prompt = OUTLINE_PROMPT
        if settings.direct_task_planning:
            from .task_planning import PROMPT
            planning_prompt = PROMPT
        plan = await _plan_as_lead(planning_prompt + body, models, configured, providers,
                                   session if session is not None else SupervisorSession(), in_project,
                                   team_request=body, artifact_work=artifact_work)
        if plan:
            plan.request = body
            return plan
    planner = select_planner_model(models, configured)
    if planner is None or planner.provider == "mock":
        if artifact_work:
            raise contracts.ContractError("Artifact planning blocked: no capable planner to declare evidence and limits")
        plan = heuristic_plan(prompt, in_project)
        plan.planning_note = "No capable configured planner; used deterministic fallback"
        return plan
    planning_cost = 0.0
    switched = []
    try:
        while True:
            try:
                result = await providers.complete(planner.id, request, cli_timeout=120)
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
        planning_cost = _cost(result, charged_model)
        try:
            plan = read_plan(result.text)
        except contracts.ContractError as exc:
            try:
                correction = await providers.complete(planner.id, request + "\nInvalid artifact plan: "
                                                     + str(exc) + "\nReturn the whole corrected plan.", cli_timeout=120)
                planning_cost += _cost(correction, charged_model)
                plan = read_plan(correction.text)
            except Exception as invalid:
                raise contracts.ContractError(f"Artifact planning blocked: {invalid}") from invalid
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
    except contracts.ContractError:
        # A generic fallback loses the required evidence and limits. Fail closed instead.
        raise
    except Exception as exc:
        if artifact_work:
            raise contracts.ContractError(f"Artifact planning blocked: {exc}") from exc
        plan = heuristic_plan(prompt, in_project)
        plan.planner_model_id = planner.id
        plan.planning_cost_usd = round(planning_cost, 6)
        plan.planning_note = f"Planner fallback: {str(exc)[:300]}"
        return plan

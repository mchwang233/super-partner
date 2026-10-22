"""Planning a task top-down (docs/design/plan-parts.md). Before the task runs, the lead splits the
request into major parts (大项), split by function so they can go on side by side, each saying what
it provides to the others (``plan_outline``). Once the task runs, every major part is split at the
same time, each in a conversation of its own (refining.py, ``refine``): level by level into smaller
parts or tasks, until every piece is a task one model can do alone.

Only tasks run. The parts group them: the lead sees them, a step's prompt names them, and the
progress view shows them. A wait on a part becomes a wait on its last tasks (``link``), so the
executor sees an ordinary DAG of tasks."""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable
from typing import Callable, Optional
from uuid import uuid4

from . import contracts
from .models import PlanPart, TaskNode, TaskPlan
from .planner import WORK_RULES, _extract_json, scope_problems, task_node

# Levels of parts. Every child of a part at the deepest level must be a task.
MAX_DEPTH = 3
MAX_CHILDREN = 8
MAX_TASKS = 60
# How many times a refinement with tasks that are not small and clear goes back to the lead.
ROUNDS = 2
# This many tasks of one part in a single chain are sent back to be made parallel.
CHAIN_LIMIT = 4
# Said to the lead whenever it plans for a task that allows the internet (planner.WORK_RULES).
INTERNET_NOTE = "(This task allows the internet: its steps' commands may download packages and call external services.)"
# Commands the outline may name to check the task's tools before it runs (TaskPlan.requires).
MAX_REQUIRES = 12
MAX_REQUIRE_CHARS = 4000

TASK_RULES = """A task is what one model does in one go. Every task must be:
- independent: its model sees only the task's instructions, the files it names and what the tasks
  it depends on provide, not this plan, the request or the rest of the project; so its instructions
  say everything it needs to know;
- clear: one concrete result, said exactly, with nothing left to guess;
- small: about 10-15 tool turns for a capable model, so it never has to read through much code;
- checkable on its own: acceptance criteria a reviewer can check against its result and files alone.
"""

CAPABILITY_CHOICES = "coding | planning | research | chinese | tool_use | structured_output"
REFINEMENT_EFFORT = {"easy": "low", "normal": "medium", "hard": "high"}

OUTLINE_PROMPT = (
    "You are planning a task for an autonomous multi-model executor, top-down. Now split the whole "
    "request into its major parts (1-" + str(MAX_CHILDREN) + "); nothing else yet. When the task runs, "
    "every part is split into tasks at the same time, each by a planner that sees the request, this "
    "outline and its own part, not the other parts' tasks.\n"
    "How to split:\n"
    "- By function, not by phase: each part is one module, feature or component of the result (a "
    "storage module, an API, a screen, a chapter), and it includes its own tests and checks. Do not "
    "make parts like \"design\", \"implementation\", \"tests\" or \"documentation\" that each touch "
    "everything; only integrating the parts and a final report may be parts of their own.\n"
    "- Parts that do not wait for each other run at the same time, so let a part wait only for what "
    "it truly needs; most parts should be able to start at once. Even a part that waits for another "
    "builds against what that one provides and starts at once: only its tasks that must run or test "
    "the other part's finished work wait for it.\n"
    "- \"provides\" is the contract other parts build on, because they never see this part's tasks: "
    "the files it owns, and exact names, function signatures, endpoints and their fields, data and "
    "file formats, commands. A part that waits for another uses only what that one provides.\n"
    "- \"size\" is how many tasks you expect the part to take, each about 10-15 tool turns for a "
    "capable model; \"capability\" is the main kind of work in it.\n"
    "- As the task's lead, assess each part's difficulty in the context of the whole project. Set "
    "\"difficulty\" (easy/normal/hard), \"refinement_effort\" (low/medium/high/max), and a short "
    "\"refinement_reason\" in the user's language. Choose by novelty, uncertainty, interacting "
    "constraints and verification complexity, not just task count. Routine well-specified splitting "
    "usually needs low or medium; complex integration needs high. Reserve max for exceptional "
    "reasoning and explain why high would be insufficient; do not use max for every part. This is "
    "the effort for splitting the part, not its workers or the project's retrospective.\n"
    "- \"requires\" lists shell commands that show the tools, simulators, licenses and services the "
    "whole task needs work on this machine (such as \"gcc --version\" or \"make --version\"); they run in "
    "the steps' sandbox before the first task, so a missing tool is found before any work. Name only "
    "what the task cannot do without; [] if it needs nothing special.\n"
    "- \"folder\" names the task's folder in two to five lowercase English words joined by hyphens "
    "(such as \"todo-cli-json\"), in English even when the request is not.\n\n"
    "Reply with only a JSON object, no markdown fences:\n"
    '{"summary": "short objective", "folder": "short-english-name", '
    '"requires": ["commands that show the tools it needs work"], '
    '"parts": [{"key": "p1", "title": "short title", '
    '"goal": "what this part delivers", "acceptance": "how to tell it is done", '
    '"provides": "the contract other parts may rely on", "size": 3, '
    '"capability": "' + CAPABILITY_CHOICES + '", '
    '"difficulty": "easy | normal | hard", "refinement_effort": "low | medium | high | max", '
    '"refinement_reason": "why this effort fits this part in this project", '
    '"dependencies": ["keys of earlier parts it needs finished first"]}]}\n'
    + contracts.RULES + '\nUser request:\n'
)

PART_ROLE = (
    "You are planning one part of a multi-model task in Super Partner. The task's lead split the "
    "request into major parts; other planners split the other parts at the same time, so rely only on "
    "what the outline below says each part provides, and deliver exactly what yours provides. Split "
    "your part level by level, as you are asked, until every piece is a task. You may read files and "
    "run read-only commands to look things up; do not edit anything.\n\n"
    + TASK_RULES + "\n" + WORK_RULES
)

PART_SHAPE = ('{"key": "s1", "kind": "part", "title": "...", "goal": "what it delivers", '
              '"acceptance": "how to tell it is done", "dependencies": ["keys of earlier children"]}')
TASK_SHAPE = (
    '{"key": "t1", "kind": "task", "title": "...", "instructions": "everything its model needs to know '
    'and do", "acceptance_criteria": "...", "capability": "coding | planning | research | chinese | '
    'tool_use | structured_output", "difficulty": "easy | normal | hard", "needs_workspace": false, '
    '"files": ["files it creates or changes"], "reads": ["files it reads for reference"], '
    '"provides": "what later tasks can rely on", "check": "a command that shows it works", '
    '"dependencies": ["keys of earlier children"], "verifies": ["keys of the tasks it checks"], '
    '"after_parts": ["titles of the parts listed above whose finished work it runs or tests; usually none"], '
    '"estimated_input_tokens": 1500, "estimated_output_tokens": 800}'
)

Say = Callable[[str], Awaitable[str]]


class SplitConflict(contracts.ContractError):
    """A complete split that needs correction against the other parts, not a fresh planner."""

    def __init__(self, reason: str, parts: list[PlanPart], nodes: list[TaskNode]):
        super().__init__(reason)
        self.parts, self.nodes = parts, nodes


def combine_split(plan: TaskPlan, part_id: str, parts: list[PlanPart], nodes: list[TaskNode]) -> TaskPlan:
    """Validate a prospective merge without mutating either the saved plan or the proposal."""
    combined = plan.model_copy(deep=True)
    combined.parts += [item.model_copy(deep=True) for item in parts]
    combined.nodes += [item.model_copy(deep=True) for item in nodes]
    for item in combined.parts:
        if item.id == part_id:
            item.status, item.error = "ready", None
    link(combined)
    try:
        if len(combined.nodes) > MAX_TASKS:
            raise contracts.ContractError(f"The combined plan exceeds {MAX_TASKS} tasks; merge tasks in your part.")
        if combined.employees:
            from .team import member
            for node in combined.nodes:
                employee = member(combined, node.employee_id)
                if not employee or node.capability not in employee.capabilities:
                    raise contracts.ContractError(f"{node.title}: employee_id must match a capable team member")
        contracts.validate(combined, partial=True)
    except contracts.ContractError as exc:
        raise SplitConflict(str(exc), parts, nodes) from exc
    return combined


def correction_message(plan: TaskPlan, conflict: SplitConflict) -> str:
    """Give the same planner its rejected work, precise errors and the latest declared handoffs."""
    available = [{"id": node.id, "title": node.title,
                  "part": [item.title for item in plan.part_path(node.part_id)],
                  "files": node.files, "artifact_contract": (
                      node.artifact_contract.model_dump() if node.artifact_contract else None)}
                 for node in plan.nodes]
    previous = {"parts": [item.model_dump() for item in conflict.parts],
                "tasks": [item.model_dump(exclude={"attempts"}) for item in conflict.nodes]}
    return (
        "Your previous split was rejected by validation of the combined plan:\n" + str(conflict)
        + "\nOther parts may have finished splitting during your conversation. Correct only your part; "
        "retain valid work and the original acceptance requirements and limits. Use exact declared "
        "artifact paths AND kinds: a parent directory is not a declared file producer. References to "
        "ordinary code/configuration belong in reads; dependency evidence needs a declared artifact "
        "producer. For cross-part waits use after_parts from your outline, not invented task IDs. "
        "Do not edit other parts or relabel their outputs as external to bypass the checks.\n"
        "Latest tasks and declared handoffs (context, not work to reproduce):\n"
        + json.dumps(available, ensure_ascii=False)
        + "\nYour rejected split (context; reply using the children schema below):\n"
        + json.dumps(previous, ensure_ascii=False) + "\n\n"
    )


def refine_message(part: PlanPart, path: list[str], depth: int, room: int, waited: list[PlanPart] = ()) -> str:
    """Ask the lead to split ``part``, which sits ``depth`` levels down; ``waited`` are the
    major parts its major part waits for."""
    deepest = depth >= MAX_DEPTH
    waits = ""
    if waited:
        waits = ("This part's work waits for these parts: " + "; ".join(
            f"\"{item.title}\" (provides: {item.provides or item.goal or 'see the outline'})" for item in waited)
            + ". Its tasks build against what they provide and start at once; only a task that must run or "
            "test their finished work names them in after_parts, and then waits until they are done.\n")
    kinds = ("This is the deepest level: every child must be a task." if deepest else
             "Each child is a task or a smaller part. Make a child a task as soon as it is independent, "
             "clear, small and checkable on its own; make it a part only if it still needs splitting.")
    return (
        f"Split part \"{part.title}\" ({' › '.join(path)}).\n"
        f"Goal: {part.goal or part.title}\n"
        + (f"Done when: {part.acceptance}\n" if part.acceptance else "")
        + f"{kinds}\n" + waits
        + f"Use 1-{MAX_CHILDREN} children; the whole plan may have {room} more tasks. Dependencies name "
        "earlier children of this part only. A task depends on another only when it needs that one's files "
        "or results: tasks that need only a contract run at the same time, so do not chain them one after "
        "another. "
        "A verification task lists in verifies the keys of the tasks it checks, and they must also be "
        "among its dependencies.\n"
        "Reply with only a JSON object, no markdown fences:\n"
        '{"children": [' + TASK_SHAPE + ("" if deepest else ", " + PART_SHAPE) + "]}"
    )


class Tree:
    """Parts and tasks as the lead names them, and what each waits for among its siblings."""

    def __init__(self) -> None:
        self.parts: dict[str, PlanPart] = {}
        self.tasks: dict[str, TaskNode] = {}
        self.parent: dict[str, Optional[str]] = {}
        self.waits: dict[str, list[str]] = {}
        self.checks: dict[str, list[str]] = {}
        self.children: dict[Optional[str], list[str]] = {}

    def add(self, children: list[dict], parent: Optional[str]) -> list[PlanPart]:
        """Add validated children (see ``siblings``) under ``parent``; returns the new parts."""
        ids: dict[str, str] = {}
        added = []
        for raw in children:
            waits = [ids[key] for key in raw["dependencies"]]
            if raw["kind"] == "task":
                node = raw["node"]
                node.part_id = parent
                node.employee_id = node.employee_id or (self.parts[parent].employee_id if parent else None)
                self.tasks[node.id] = node
                item = node.id
                self.checks[item] = [ids[key] for key in raw["verifies"]]
            else:
                part = PlanPart(id=uuid4().hex[:8], title=str(raw.get("title") or raw["key"])[:120],
                                goal=str(raw.get("goal") or "").strip()[:1000],
                                acceptance=str(raw.get("acceptance") or "").strip()[:1000],
                                parent_id=parent, dependencies=waits,
                                employee_id=raw.get("employee_id") or (self.parts[parent].employee_id if parent else None))
                self.parts[part.id] = part
                item = part.id
                added.append(part)
            ids[raw["key"]] = item
            self.children.setdefault(parent, []).append(item)
            self.parent[item] = parent
            self.waits[item] = waits
        return added

    def depth(self, part_id: str) -> int:
        level, item = 0, part_id
        while item is not None:
            level, item = level + 1, self.parent[item]
        return level

    def leaves(self, item: str) -> list[str]:
        if item in self.tasks:
            return [item]
        return [task for task in self.ordered(item) if task in self.tasks]

    def before(self, item: str, top: Optional[str]) -> list[str]:
        """Tasks ``item`` waits for inside ``top`` (None: the whole plan): what it and each of its
        parts below ``top`` wait for among their siblings."""
        found: list[str] = []
        while item != top and item is not None:
            for waited in self.waits[item]:
                found += self.last(waited)
            item = self.parent[item]
        return found

    def last(self, item: str) -> list[str]:
        """The tasks of ``item`` that no other task of it waits for: once they are done, it is."""
        leaves = self.leaves(item)
        if item in self.tasks:
            return leaves
        waited = {task for leaf in leaves for task in self.before(leaf, item)}
        return [leaf for leaf in leaves if leaf not in waited]

    def ordered(self, parent: Optional[str] = None) -> list[str]:
        """Every item under ``parent`` in the order the plan reads: depth first, as split."""
        return [found for item in self.children.get(parent, []) for found in [item, *self.ordered(item)]]

    def plan(self, summary: str) -> TaskPlan:
        order = self.ordered()
        nodes = [self.tasks[item] for item in order if item in self.tasks]
        for node in nodes:
            checked = [task for item in self.checks[node.id] for task in self.leaves(item)]
            node.verifies = list(dict.fromkeys(checked))
            node.dependencies = list(dict.fromkeys(self.before(node.id, None) + checked))
        parts = [self.parts[item] for item in order if item in self.parts]
        return TaskPlan(summary=summary[:240], nodes=nodes, parts=parts)


def siblings(items, deepest: bool, parts_only: bool = False, room: int = MAX_TASKS) -> list[dict]:
    """Check one level of the lead's split: keys, kinds, and dependencies on earlier siblings.
    Tasks are built here (``raw["node"]``). Raises ValueError with what is wrong."""
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_CHILDREN:
        raise ValueError(f"give 1-{MAX_CHILDREN} items")
    seen: set[str] = set()
    checked = []
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise TypeError("every item must be a JSON object")
        key = str(raw.get("key") or f"c{index + 1}").strip()
        if key in seen:
            raise ValueError(f"duplicate key {key}")
        kind = "part" if parts_only else str(raw.get("kind") or ("task" if raw.get("instructions") else "part"))
        if kind not in {"part", "task"}:
            raise ValueError(f"{key}: kind must be part or task")
        if kind == "part" and deepest:
            raise ValueError(f"{key}: this is the deepest level, so every child must be a task")
        dependencies = raw.get("dependencies") or []
        verifies = (raw.get("verifies") or []) if kind == "task" else []
        if not isinstance(dependencies, list) or not isinstance(verifies, list):
            raise TypeError(f"{key}: dependencies and verifies must be lists")
        dependencies, verifies = [str(item) for item in dependencies], [str(item) for item in verifies]
        if any(item not in seen for item in dependencies):
            raise ValueError(f"{key}: dependencies must name earlier items of this level")
        if any(item not in dependencies for item in verifies):
            raise ValueError(f"{key}: verifies must name items that are among its dependencies")
        entry = {**raw, "key": key, "kind": kind, "dependencies": dependencies, "verifies": verifies}
        if kind == "task":
            # Keys for now, so a verification is seen as one; Tree.plan resolves them to tasks.
            entry["node"] = task_node(raw, key, [], verifies)
            # Titles for now; refine resolves them to the waited-for parts.
            entry["node"].after_parts = [str(item) for item in raw.get("after_parts") or []
                                         if isinstance(item, (str, int))] if isinstance(raw.get("after_parts"), list) else []
            if not entry["node"].instructions:
                raise ValueError(f"task {key} needs instructions")
        seen.add(key)
        checked.append(entry)
    if (tasks := sum(item["kind"] == "task" for item in checked)) > room:
        raise ValueError(f"{tasks} tasks, but the plan may have only {room} more; merge some")
    return checked


def longest_chain(children: list[dict]) -> list[str]:
    """The titles along the longest line of children each waiting for the one before (a smaller
    part counts as one): those run one after another however many models are free."""
    lines: dict[str, list[str]] = {}
    for item in children:  # dependencies name earlier children only
        before = max((lines[key] for key in item["dependencies"] if key in lines), key=len, default=[])
        lines[item["key"]] = [*before, str(item.get("title") or item["key"])]
    return max(lines.values(), key=len, default=[])


def task_problems(children: list[dict], expected: Optional[int] = None) -> list[str]:
    """Why some tasks of a split are not yet independent, clear and small, to send back.
    ``expected`` is how many tasks the outline expected the part to take (PlanPart.size)."""
    nodes = [item["node"] for item in children if item["kind"] == "task"]
    problems = scope_problems(TaskPlan(summary="", nodes=nodes))
    coverage = any(contracts.stages(node) & {"coverage_vdb", "urg_report"} for node in nodes)
    problems += [problem for node in nodes for problem in contracts.node_problems(node, coverage=coverage)]
    problems += [f"{node.title}: no acceptance_criteria; say how a reviewer can tell it is done"
                 for node in nodes if not node.acceptance_criteria]
    if len(chain := longest_chain(children)) >= CHAIN_LIMIT:
        problems.append(f"{len(chain)} of these form one chain, each waiting for the one before ("
                        + " → ".join(chain) + "); a task should wait only for tasks whose files or results it "
                        "needs, so that the others run at the same time")
    limit = max(expected + 2, math.ceil(expected * 1.5)) if expected else None
    if limit and len(nodes) == len(children) and len(nodes) > limit:
        problems.append(f"{len(nodes)} tasks, but this part was expected to take about {expected}; merge tasks "
                        "that change the same files or are checked together, so that there are at most "
                        f"{limit}")
    return problems


async def _ask(say: Say, message: str, read):
    """Ask, and once more in the same conversation if the reply is not what was asked for."""
    reply = await say(message)
    try:
        return read(_extract_json(reply))
    except (ValueError, TypeError, KeyError) as exc:
        reply = await say(f"That reply was not a valid plan ({exc}). Reply with the JSON object "
                          "described, and nothing else.")
        return read(_extract_json(reply))


async def plan_outline(say: Say, opening: str, team_plan=None) -> TaskPlan:
    """The major parts of a plan, from the lead through ``say``; their tasks come later
    (``refine``). Raises ValueError if the lead's replies cannot be turned into an outline."""
    def read(payload: dict):
        if team_plan:
            from .team import member
            for item in payload.get("parts") or []:
                employee = member(team_plan, item.get("employee_id"))
                if not employee or item.get("capability", "coding") not in employee.capabilities:
                    raise ValueError("each part needs a capable employee_id from the existing team")
        return (str(payload.get("summary") or "").strip(), siblings(payload.get("parts"), False, parts_only=True),
                payload.get("requires"), str(payload.get("folder") or ""))

    summary, raw, requires, folder = await _ask(say, opening, read)
    tree = Tree()
    parts = tree.add(raw, None)
    for part, item in zip(parts, raw):
        part.provides = str(item.get("provides") or "").strip()[:3000]
        part.capability = str(item.get("capability") or "coding").split("|")[0].strip()
        if part.capability not in CAPABILITY_CHOICES.split(" | "):
            part.capability = "coding"
        difficulty = str(item.get("difficulty") or "normal").strip().lower()
        part.difficulty = difficulty if difficulty in REFINEMENT_EFFORT else "normal"
        part.refinement_reason = str(item.get("refinement_reason") or "").strip()[:500]
        requested = str(item.get("refinement_effort") or "").strip().lower()
        part.refinement_effort = (requested if requested in {"low", "medium", "high", "max"}
                                  and (requested != "max" or part.refinement_reason)
                                  else REFINEMENT_EFFORT[part.difficulty])
        if not part.refinement_reason:
            part.refinement_reason = "按负责人标注的难度选择推理档；未标注时按常规难度处理。"
        try:
            part.size = max(1, min(int(item.get("size") or 3), MAX_TASKS))
        except (TypeError, ValueError):
            part.size = 3
        part.status = "planned"
    # A command cut short would be a broken one: one too long is left out instead.
    commands = [text for item in requires if isinstance(item, (str, int))
                and 0 < len(text := str(item).strip()) <= MAX_REQUIRE_CHARS] if isinstance(requires, list) else []
    return TaskPlan(summary=(summary or "Autonomous task plan")[:240], nodes=[], parts=parts,
                    requires=list(dict.fromkeys(commands))[:MAX_REQUIRES], folder=folder[:80],
                    employees=team_plan.employees if team_plan else [], team_brief=team_plan.team_brief if team_plan else "")


def outline_text(plan: TaskPlan) -> str:
    """The major parts as a planner of one of them sees them."""
    titles = {part.id: part.title for part in plan.parts}
    lines = []
    for part in plan.parts:
        if part.parent_id:
            continue
        waits = ", ".join(titles.get(item, item) for item in part.dependencies)
        lines.append(f"- {part.title}: {part.goal}" + (f" (waits for: {waits})" if waits else ""))
        if part.provides:
            lines.append(f"  provides: {part.provides}")
    return "\n".join(lines)


def part_opening(plan: TaskPlan, part: PlanPart, internet: bool = False) -> str:
    """The first message to the planner of ``part``, before it is asked to split it; ``internet``
    is whether the task allows it now (it can change after planning)."""
    request = plan.request.replace(INTERNET_NOTE, "").rstrip()
    from .team import assignment_prompt
    shared = assignment_prompt(plan) if plan.employees else ""
    return (PART_ROLE + shared + f"\nOutline of the task ({plan.summary}):\n{outline_text(plan)}\n\n"
            f"Your part: {part.title}\n\nRequest and context:\n{request}\n\n"
            + (INTERNET_NOTE if internet else "The task does not allow the internet: its steps have no network.")
            + "\n\n")


async def refine(say: Say, plan: TaskPlan, part: PlanPart, opening: str = "") -> tuple[list[PlanPart], list[TaskNode]]:
    """Split ``part`` of ``plan`` through ``say``, depth first, until every piece is a task.
    ``opening`` comes before the first question. Returns its new parts and its tasks, whose
    dependencies are among themselves; ``link`` adds those on other major parts."""
    tree = Tree()
    tree.parts[part.id], tree.parent[part.id], tree.waits[part.id] = part, None, []
    tree.children[None] = [part.id]
    above = len(plan.part_path(part.parent_id))
    # The major parts this one waits for: its tasks build against them, or name them to wait.
    top = (plan.part_path(part.parent_id) or [part])[0]
    waited = [item for item in plan.parts if item.id in top.dependencies]
    waiting = [part]
    while waiting:
        current = waiting.pop(0)
        depth = above + tree.depth(current.id)
        room = MAX_TASKS - len(plan.nodes) - len(tree.tasks)
        path = [item.title for item in plan.part_path(part.parent_id)] + [item.title for item in _path(tree, current.id)]

        def read(payload: dict, depth=depth, room=room):
            return siblings(payload.get("children"), depth >= MAX_DEPTH, room=room)

        message = refine_message(current, path, depth, room, waited)
        children = await _ask(say, opening + message if current is part else message, read)
        for _ in range(ROUNDS):
            if not (problems := task_problems(children, part.size if current is part else None)):
                break
            # Too broad or vague a task makes its model explore or guess; the planner cuts it up.
            try:
                children = await _ask(say, (
                    f"Some tasks of \"{current.title}\" are not yet small and clear:\n- " + "\n- ".join(problems)
                    + "\nReply with the whole corrected split of this part as the JSON object described, "
                    "and nothing else."), read)
            except (ValueError, TypeError, KeyError):
                break  # keep the split it gave before
        # Depth first: this part's own parts are split before the next one at its level.
        local = [item["node"] for item in children if item["kind"] == "task"]
        coverage = any(contracts.stages(node) & {"coverage_vdb", "urg_report"} for node in local)
        if problems := [p for node in local for p in contracts.node_problems(node, coverage=coverage)]:
            raise contracts.ContractError("Artifact split blocked: " + "; ".join(problems))
        waiting[0:0] = tree.add(children, current.id)
    if not tree.tasks:
        raise ValueError(f"part {part.title} has no tasks")
    split = tree.plan("")
    names = {name: item.id for item in waited for name in (item.title, item.id)}
    for node in split.nodes:
        node.after_parts = list(dict.fromkeys(names[name] for name in node.after_parts if name in names))
    parts = [item for item in split.parts if item.id != part.id]
    combine_split(plan, part.id, parts, split.nodes)
    return parts, split.nodes


def _top(plan: TaskPlan, part_id: Optional[str]) -> Optional[PlanPart]:
    path = plan.part_path(part_id)
    return path[0] if path else None


def last_tasks(plan: TaskPlan, top: PlanPart) -> list[str]:
    """The tasks of major part ``top`` that none of its other tasks waits for."""
    own = [node for node in plan.nodes if (found := _top(plan, node.part_id)) and found.id == top.id]
    ids = {node.id for node in own}
    waited = {item for node in own for item in node.dependencies if item in ids}
    return [node.id for node in own if node.id not in waited]


def link(plan: TaskPlan) -> None:
    """Make each task that runs or tests another major part's finished work (``after_parts``)
    wait for that part's last tasks, once it is split; every other task builds against what the
    part provides and starts at once. Only adds waits, so the lead's own changes stay."""
    by_id = {part.id: part for part in plan.parts}
    last = {part.id: last_tasks(plan, part) for part in plan.parts if not part.parent_id and part.status == "ready"}
    for node in plan.nodes:
        top = _top(plan, node.part_id)
        for waited in top.dependencies if top else []:
            if waited in last and waited in node.after_parts and by_id[waited].parent_id is None:
                node.dependencies += [item for item in last[waited] if item not in node.dependencies]


def waits_on_unsplit(plan: TaskPlan, node: TaskNode) -> bool:
    """Whether ``node``'s major part waits for one not split into tasks yet, whose tasks it will wait for."""
    by_id = {part.id: part for part in plan.parts}
    top = _top(plan, node.part_id)
    return bool(top) and any(by_id[item].status != "ready" for item in top.dependencies
                             if item in by_id and item in node.after_parts)


def _path(tree: Tree, part_id: str) -> list[PlanPart]:
    path = []
    item: Optional[str] = part_id
    while item is not None:
        path.insert(0, tree.parts[item])
        item = tree.parent[item]
    return path

"""Planning a task top-down (docs/design/plan-parts.md). In its own conversation, the lead first splits
the request into major parts (大项). It then splits each part, level by level, into smaller parts or
tasks, until every piece is a task one model can do alone. The tasks are the plan's steps, and only
they run. The parts group the tasks: the lead sees them, a step's prompt names them, and the
progress view shows them.

A dependency between parts becomes a dependency on the waited-for part's last tasks, so the
executor sees an ordinary DAG of tasks."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Callable, Optional
from uuid import uuid4

from .models import PlanPart, TaskNode, TaskPlan
from .planner import WORK_RULES, _extract_json, scope_problems, task_node

# Levels of parts. Every child of a part at the deepest level must be a task.
MAX_DEPTH = 3
MAX_CHILDREN = 8
MAX_TASKS = 60
# How many times a refinement with tasks that are not small and clear goes back to the lead.
ROUNDS = 2

TASK_RULES = """A task is what one model does in one go. Every task must be:
- independent: its model sees only the task's instructions, the files it names and what the tasks
  it depends on provide, not this plan, the request or the rest of the project; so its instructions
  say everything it needs to know;
- clear: one concrete result, said exactly, with nothing left to guess;
- small: about 10-15 tool turns for a capable model, so it never has to read through much code;
- checkable on its own: acceptance criteria a reviewer can check against its result and files alone.
"""

OUTLINE_PROMPT = (
    "You are planning a task for an autonomous multi-model executor. Plan it top-down, level by "
    "level, in this conversation:\n"
    "1. Now: split the whole request into its major parts (1-" + str(MAX_CHILDREN) + "). A part is one "
    "area of the result with its own goal, for example the data layer, an API, a page, the tests, "
    "a chapter of a report. Parts that do not wait for each other run at the same time.\n"
    "2. Then you will be asked about each part in turn, to split it into smaller parts or tasks, "
    "until every piece is a task.\n\n"
    + TASK_RULES + "\n" + WORK_RULES
    + "\nReply now with only a JSON object, no markdown fences:\n"
    '{"summary": "short objective", "parts": [{"key": "p1", "title": "short title", '
    '"goal": "what this part delivers", "acceptance": "how to tell it is done", '
    '"dependencies": ["keys of earlier parts it needs finished first"]}]}\n\nUser request:\n'
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
    '"estimated_input_tokens": 1500, "estimated_output_tokens": 800}'
)

Say = Callable[[str], Awaitable[str]]


def refine_message(part: PlanPart, path: list[str], depth: int, room: int) -> str:
    """Ask the lead to split ``part``, which sits ``depth`` levels down."""
    deepest = depth >= MAX_DEPTH
    kinds = ("This is the deepest level: every child must be a task." if deepest else
             "Each child is a task or a smaller part. Make a child a task as soon as it is independent, "
             "clear, small and checkable on its own; make it a part only if it still needs splitting.")
    return (
        f"Split part \"{part.title}\" ({' › '.join(path)}).\n"
        f"Goal: {part.goal or part.title}\n"
        + (f"Done when: {part.acceptance}\n" if part.acceptance else "")
        + f"{kinds}\n"
        f"Use 1-{MAX_CHILDREN} children; the whole plan may have {room} more tasks. Dependencies name "
        "earlier children of this part only, because whatever this part waits for is already arranged. "
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
                self.tasks[node.id] = node
                item = node.id
                self.checks[item] = [ids[key] for key in raw["verifies"]]
            else:
                part = PlanPart(id=uuid4().hex[:8], title=str(raw.get("title") or raw["key"])[:120],
                                goal=str(raw.get("goal") or "").strip()[:1000],
                                acceptance=str(raw.get("acceptance") or "").strip()[:1000],
                                parent_id=parent, dependencies=waits)
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
            if not entry["node"].instructions:
                raise ValueError(f"task {key} needs instructions")
        seen.add(key)
        checked.append(entry)
    if (tasks := sum(item["kind"] == "task" for item in checked)) > room:
        raise ValueError(f"{tasks} tasks, but the plan may have only {room} more; merge some")
    return checked


def task_problems(children: list[dict]) -> list[str]:
    """Why some tasks of a split are not yet independent, clear and small, to send back."""
    nodes = [item["node"] for item in children if item["kind"] == "task"]
    problems = scope_problems(TaskPlan(summary="", nodes=nodes))
    problems += [f"{node.title}: no acceptance_criteria; say how a reviewer can tell it is done"
                 for node in nodes if not node.acceptance_criteria]
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


async def plan_in_parts(say: Say, opening: str) -> TaskPlan:
    """Plan top-down through ``say`` (one message to the lead, its reply back): the major parts
    first, then each part in turn, depth first. Raises ValueError if the lead's replies cannot be
    turned into a plan."""
    tree = Tree()

    def outline(payload: dict):
        return str(payload.get("summary") or "").strip(), siblings(payload.get("parts"), False, parts_only=True)

    summary, parts = await _ask(say, opening, outline)
    waiting = tree.add(parts, None)
    while waiting:
        part = waiting.pop(0)
        depth = tree.depth(part.id)
        room = MAX_TASKS - len(tree.tasks)
        path = [item.title for item in _path(tree, part.id)]

        def read(payload: dict, depth=depth, room=room):
            return siblings(payload.get("children"), depth >= MAX_DEPTH, room=room)

        children = await _ask(say, refine_message(part, path, depth, room), read)
        for _ in range(ROUNDS):
            if not (problems := task_problems(children)):
                break
            # Too broad or vague a task makes its model explore or guess; the lead cuts it up.
            try:
                children = await _ask(say, (
                    f"Some tasks of \"{part.title}\" are not yet small and clear:\n- " + "\n- ".join(problems)
                    + "\nReply with the whole corrected split of this part as the JSON object described, "
                    "and nothing else."), read)
            except (ValueError, TypeError, KeyError):
                break  # keep the split it gave before
        # Depth first: this part's own parts are split before the next part at its level.
        waiting[0:0] = tree.add(children, part.id)
    if not tree.tasks:
        raise ValueError("the plan has no tasks")
    return tree.plan(summary or "Autonomous task plan")


def _path(tree: Tree, part_id: str) -> list[PlanPart]:
    path = []
    item: Optional[str] = part_id
    while item is not None:
        path.insert(0, tree.parts[item])
        item = tree.parent[item]
    return path

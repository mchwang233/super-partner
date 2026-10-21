"""The lead plans top-down. Before the task runs, it splits the request into major parts by
function, each with the contract it provides. Once the task runs, every part is split at the same
time, level by level, until each piece is a task that is independent, clear, small and checkable on
its own."""
import asyncio
import json

from super_partner import outline, supervisor
from super_partner.executor import Executor
from super_partner.models import (
    ExecutionRun,
    PlanPart,
    RunStatus,
    SupervisorSession,
    TaskNode,
    TaskPlan,
)
from super_partner.planner import autonomous_plan
from super_partner.providers import ProviderResult
from super_partner.quality import node_prompt
from super_partner.replan import change_plan
from test_leading import MODELS as RUN_MODELS
from test_leading import decision
from test_replan import CONFIGURED, MODELS, plan, step
from test_supervisor import CONFIGURED as LEADS_CONFIGURED
from test_supervisor import MODELS as LEAD_MODELS
from test_supervisor import Leads


def part(key, title, deps=(), **fields):
    return {"key": key, "kind": "part", "title": title, "goal": f"{title}的目标", "acceptance": "可验收",
            "dependencies": list(deps), **fields}


def task(key, title, deps=(), **fields):
    return {"key": key, "kind": "task", "title": title, "instructions": f"做{title}", "capability": "coding",
            "acceptance_criteria": "测试通过", "files": [f"{key}.py"], "dependencies": list(deps), **fields}


def outline_reply(*parts):
    return json.dumps({"summary": "待办应用", "parts": list(parts)})


def split(*children):
    return json.dumps({"children": list(children)})


BACKEND = part("api", "后端", provides="GET /todos -> [{id, title, done}]", size=3)
FRONTEND = part("ui", "前端", ["api"], provides="index.html", size=2, capability="coding")


async def outlined(*parts):
    leads = Leads([outline_reply(*parts)])
    planned = await autonomous_plan("写一个待办应用", LEAD_MODELS, LEADS_CONFIGURED, leads, session=SupervisorSession())
    return planned, leads


async def refine(planned, title, *replies):
    """Split major part ``title`` with ``replies``; returns the messages its planner was sent."""
    leads = Leads(list(replies))
    session = SupervisorSession(model_id="cli:codex")

    async def say(text):
        return (await leads.converse(session, text)).text

    target = next(item for item in planned.parts if item.title == title)
    parts, nodes = await outline.refine(say, planned, target, outline.part_opening(planned, target))
    planned.parts += parts
    planned.nodes += nodes
    target.status = "ready"
    outline.link(planned)
    return [message for _, _, message in leads.messages]


async def test_before_it_runs_a_task_is_planned_as_major_parts_by_function():
    planned, leads = await outlined(BACKEND, FRONTEND)
    assert planned.nodes == [] and planned.request.startswith("写一个待办应用")
    assert [(item.title, item.status, item.size, item.provides) for item in planned.parts] == [
        ("后端", "planned", 3, "GET /todos -> [{id, title, done}]"), ("前端", "planned", 2, "index.html")]
    assert planned.parts[1].dependencies == [planned.parts[0].id]
    asked = leads.messages[0][2]
    assert "By function, not by phase" in asked and "includes its own tests" in asked and '"provides"' in asked
    assert "各大项同时细化" in planned.planning_note


async def test_a_major_part_is_split_level_by_level_and_waits_become_task_waits():
    planned, _ = await outlined(BACKEND, FRONTEND)
    # The front end is split first. Its page builds against what the back end provides and can start
    # at once; its check runs against the finished back end, so it says so and waits for it.
    front = await refine(planned, "前端", split(task("page", "页面"), task("check", "检查", ["page"], verifies=["page"],
                                                                         files=[], reads=["page.py"], after_parts=["后端"])))
    page, check = planned.nodes
    assert page.after_parts == [] and check.after_parts == [planned.parts[0].id]
    assert not outline.waits_on_unsplit(planned, page) and outline.waits_on_unsplit(planned, check)
    back = await refine(planned, "后端", split(part("store", "存储"), task("routes", "接口", ["store"])),
                        split(task("model", "数据模型"), task("db", "数据库读写", ["model"])))
    titles = {item.id: item.title for item in [*planned.nodes, *planned.parts]}
    assert [(node.title, titles[node.part_id]) for node in planned.nodes] == [
        ("页面", "前端"), ("检查", "前端"), ("数据模型", "存储"), ("数据库读写", "存储"), ("接口", "后端")]
    # Once both are split, only the check waits for the back end's last task.
    assert {node.title: [titles[item] for item in node.dependencies] for node in planned.nodes} == {
        "页面": [], "检查": ["页面", "接口"], "数据模型": [], "数据库读写": ["数据模型"], "接口": ["数据库读写"]}
    assert not outline.waits_on_unsplit(planned, check)
    # The front end's planner heard what it waits for and how to wait.
    assert '"后端" (provides: GET /todos' in front[0] and "after_parts" in front[0]
    # Each planner sees the outline with every part's contract, and its own part; depth first.
    assert "Outline of the task" in front[0] and "provides: GET /todos" in front[0] and "Your part: 前端" in front[0]
    assert 'Split part "后端" (后端)' in back[0] and back[1].startswith('Split part "存储" (后端 › 存储)')


async def test_the_deepest_level_must_be_tasks(monkeypatch):
    monkeypatch.setattr(outline, "MAX_DEPTH", 1)
    planned, _ = await outlined(part("p", "全部"))
    messages = await refine(planned, "全部", split(part("s", "还想再拆")), split(task("t", "做完")))
    assert [node.title for node in planned.nodes] == ["做完"]
    assert "every child must be a task" in messages[0] and '"kind": "part"' not in messages[0]
    assert "not a valid plan" in messages[1] and "deepest level" in messages[1]


async def test_vague_tasks_go_back_to_the_planner():
    planned, _ = await outlined(part("p", "全部"))
    messages = await refine(planned, "全部", split(task("t", "随便做做", acceptance_criteria="")), split(task("t", "做完")))
    assert "随便做做: no acceptance_criteria" in messages[1]
    assert [node.title for node in planned.nodes] == ["做完"]


def test_parallel_tasks_are_all_waited_for():
    tree = outline.Tree()
    [first, second] = tree.add(outline.siblings([part("a", "A"), part("b", "B", ["a"])], False), None)
    tree.add(outline.siblings([task("x", "X"), task("y", "Y")], False), first.id)
    tree.add(outline.siblings([task("z", "Z")], False), second.id)
    result = tree.plan("s")
    ids = {node.title: node.id for node in result.nodes}
    assert result.nodes[-1].dependencies == [ids["X"], ids["Y"]]


def test_a_split_names_only_earlier_siblings_and_stays_within_the_plan_size():
    for children, error in [
        ([task("a", "A", ["b"]), task("b", "B")], "earlier items"),
        ([task("a", "A"), task("b", "B", verifies=["a"])], "among its dependencies"),
        ([task("a", "A", instructions="")], "needs instructions"),
    ]:
        try:
            outline.siblings(children, False)
        except ValueError as exc:
            assert error in str(exc)
        else:
            raise AssertionError(error)
    try:
        outline.siblings([task("a", "A"), task("b", "B")], False, room=1)
    except ValueError as exc:
        assert "merge some" in str(exc)
    else:
        raise AssertionError("room")


class Splitters:
    """Planners answer by the part they are asked about; steps succeed; the lead says done."""

    def __init__(self, splits):
        self.splits, self.work, self.lead = splits, [], []

    def configured_models(self, ids):
        return set(ids)

    async def converse(self, session, prompt, spend_ok=None, timeout=600):
        session.session_id = session.session_id or f"{session.model_id}-{len(self.lead)}"
        self.lead.append(prompt)
        if "EVENT: every step has finished" in prompt:
            return ProviderResult(decision("done"), 0, 0, session.model_id)
        title = next(name for name in self.splits if f'Split part "{name}"' in prompt)
        await asyncio.sleep(0.01)
        return ProviderResult(self.splits[title], 0, 0, session.model_id)

    async def complete(self, model_id, prompt, workspace=None, **kwargs):
        self.work.append(prompt.split("\n", 1)[0])
        return ProviderResult("做完了", 0, 0, model_id)


def outlined_run(database):
    parts = [PlanPart(id="api", title="后端", goal="接口", status="planned", provides="GET /todos"),
             PlanPart(id="ui", title="前端", goal="页面", status="planned", dependencies=["api"])]
    database.save_run(ExecutionRun(
        id="o", prompt="写一个待办应用", status=RunStatus.QUEUED, budget_usd=1,
        supervisor=SupervisorSession(model_id="cli:claude", session_id="lead-1"),
        plan=TaskPlan(summary="待办应用", nodes=[], parts=parts, request="写一个待办应用")))


async def test_a_running_task_splits_its_parts_at_once_and_runs_their_tasks_in_order(database):
    outlined_run(database)
    team = Splitters({"后端": split(task("routes", "接口")), "前端": split(task("page", "页面", after_parts=["后端"]))})
    executor = Executor(database, team, RUN_MODELS)
    await executor.execute("o")
    run = database.get_run("o")
    assert run.status == RunStatus.SUCCEEDED and [item.status for item in run.plan.parts] == ["ready", "ready"]
    # The estimate counts the steps the parts were split into.
    assert run.plan.estimate and run.plan.estimate.base_usd >= sum(node.estimated_cost_usd for node in run.plan.nodes)
    # Both parts were asked about before either answered, and the page waited for the API.
    assert ['Split part "后端"' in team.lead[0], 'Split part "前端"' in team.lead[1]] == [True, True]
    assert team.work == ["Task: 做接口", "Task: 做页面"]
    assert all(node.model_id for node in run.plan.nodes)
    events = [event.kind for event in database.events("o")]
    assert events.count("part.refined") == 2


async def test_a_part_that_cannot_be_split_pauses_the_task_until_it_is_continued(database):
    outlined_run(database)
    team = Splitters({"后端": "我拆不了", "前端": split(task("page", "页面", after_parts=["后端"]))})
    executor = Executor(database, team, RUN_MODELS)
    await executor.execute("o")
    run = database.get_run("o")
    failed = run.plan.parts[0]
    assert (run.status, failed.status) == (RunStatus.PAUSED, "failed")
    assert "大项「后端」没能细化成任务" in run.error and team.work == []
    team.splits["后端"] = split(task("routes", "接口"))
    run.status = RunStatus.QUEUED
    database.save_run(run)
    await executor.execute("o")
    assert database.get_run("o").status == RunStatus.SUCCEEDED and team.work == ["Task: 做接口", "Task: 做页面"]


def parted():
    current = plan(step("a"), step("b"))
    current.plan.parts = [PlanPart(id="p", title="后端", goal="提供接口"), PlanPart(id="q", title="存储", parent_id="p"),
                          PlanPart(id="u", title="前端")]
    current.plan.nodes[0].part_id, current.plan.nodes[1].part_id = "q", "u"
    return current


def test_a_step_hears_where_it_sits_and_the_lead_sees_the_parts():
    current = parted()
    prompt, _ = node_prompt(current.plan.nodes[0], current, 4000)
    assert "Part of: 后端 › 存储" in prompt
    assert "- [a] A in [q] 后端 › 存储: pending" in supervisor._steps(current)


def test_a_step_added_while_running_joins_a_part():
    current = parted()
    change_plan(current, {"add": [{"key": "n", "title": "补测试", "instructions": "写测试"},
                                  {"key": "m", "title": "补页面", "instructions": "写页面", "part": "u"},
                                  {"key": "o", "title": "别处", "instructions": "写", "part": "nope"}]},
                list(MODELS.values()), CONFIGURED, deciding="a")
    assert [(node.title, node.part_id) for node in current.plan.nodes[2:]] == [
        ("补测试", "q"), ("补页面", "u"), ("别处", "q")]


def test_a_plan_without_parts_is_unchanged():
    flat = TaskPlan(summary="s", nodes=[TaskNode(id="a", title="A", instructions="i", capability="coding")])
    assert flat.parts == [] and flat.part_path(None) == [] and flat.part_path("missing") == []
    assert flat.unsplit_cost() == 0


def test_a_part_not_yet_split_is_estimated_from_the_tasks_it_expects():
    from super_partner.routing import part_estimate

    def cost(size):
        return part_estimate(PlanPart(id="p", title="后端", size=size, status="planned"), False,
                             list(RUN_MODELS.values()), {"x:api"})

    assert cost(1) > 0 and abs(cost(3) - 3 * cost(1)) < 1e-6
    later = TaskPlan(summary="s", nodes=[], parts=[PlanPart(id="p", title="后端", status="planned", estimated_cost_usd=0.5),
                                                   PlanPart(id="q", title="前端", estimated_cost_usd=0.2)])
    assert later.unsplit_cost() == 0.5



async def test_a_task_that_only_needs_a_contract_starts_before_the_part_it_builds_on(database):
    outlined_run(database)
    order = []

    class Slow(Splitters):
        async def converse(self, session, prompt, spend_ok=None, timeout=600):
            if 'Split part "后端"' in prompt:
                await asyncio.sleep(0.2)  # the back end takes long to split
            return await super().converse(session, prompt, spend_ok, timeout)

        async def complete(self, model_id, prompt, workspace=None, **kwargs):
            order.append(prompt.split("\n", 1)[0])
            return await super().complete(model_id, prompt, workspace, **kwargs)

    team = Slow({"后端": split(task("routes", "接口")), "前端": split(task("page", "页面"))})
    await Executor(database, team, RUN_MODELS).execute("o")
    # The page started while the back end was still being split.
    assert order[0] == "Task: 做页面" and database.get_run("o").status == RunStatus.SUCCEEDED


def test_a_long_line_of_waits_goes_back_even_with_a_task_beside_it():
    """Not only a split whose tasks all wait one after another: any line of four, in any order."""
    near = outline.siblings([task("a", "A"), task("b", "B", ["a"]), task("x", "X"), task("c", "C", ["b"]),
                             task("d", "D", ["c"])], False)
    [problem] = [item for item in outline.task_problems(near) if "form one chain" in item]
    assert "A → B → C → D" in problem


def test_a_part_split_into_far_more_tasks_than_expected_goes_back():
    six = outline.siblings([task(key, key.upper()) for key in "abcdef"], False)
    assert any("expected to take about 3" in problem for problem in outline.task_problems(six, expected=3))
    assert not any("expected" in problem for problem in outline.task_problems(six, expected=4))
    assert not any("expected" in problem for problem in outline.task_problems(six))


def test_a_part_split_into_one_long_chain_goes_back():
    chain = outline.siblings([task("a", "A"), task("b", "B", ["a"]), task("c", "C", ["b"]), task("d", "D", ["c"])], False)
    assert any("form one chain" in problem for problem in outline.task_problems(chain))
    fanned = outline.siblings([task("a", "A"), task("b", "B", ["a"]), task("c", "C", ["a"]), task("d", "D", ["b", "c"])], False)
    assert not any("form one chain" in problem for problem in outline.task_problems(fanned))

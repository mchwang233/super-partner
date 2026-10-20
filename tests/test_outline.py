"""The lead plans top-down: major parts first, then each part split level by level until every
piece is a task that is independent, clear, small and checkable on its own."""
import json

from super_partner import outline, supervisor
from super_partner.models import PlanPart, SupervisorSession, TaskNode, TaskPlan
from super_partner.planner import autonomous_plan
from super_partner.quality import node_prompt
from super_partner.replan import change_plan
from test_replan import CONFIGURED, MODELS, plan, step
from test_supervisor import CONFIGURED as LEADS_CONFIGURED
from test_supervisor import MODELS as LEAD_MODELS
from test_supervisor import Leads


def part(key, title, deps=()):
    return {"key": key, "kind": "part", "title": title, "goal": f"{title}的目标", "acceptance": "可验收",
            "dependencies": list(deps)}


def task(key, title, deps=(), **fields):
    return {"key": key, "kind": "task", "title": title, "instructions": f"做{title}", "capability": "coding",
            "acceptance_criteria": "测试通过", "files": [f"{key}.py"], "dependencies": list(deps), **fields}


def outline_reply(*parts):
    return json.dumps({"summary": "待办应用", "parts": list(parts)})


def split(*children):
    return json.dumps({"children": list(children)})


async def test_the_lead_splits_major_parts_level_by_level_into_tasks():
    leads = Leads([
        outline_reply(part("api", "后端"), part("ui", "前端", ["api"])),
        split(part("store", "存储"), task("routes", "接口", ["store"])),
        split(task("model", "数据模型"), task("db", "数据库读写", ["model"])),
        split(task("page", "页面"), task("check", "检查", ["page"], verifies=["page"], files=[], reads=["page.py"])),
    ])
    result = await autonomous_plan("写一个待办应用", LEAD_MODELS, LEADS_CONFIGURED, leads, session=SupervisorSession())
    titles = {item.id: item.title for item in [*result.nodes, *result.parts]}
    assert [(item.title, titles.get(item.parent_id)) for item in result.parts] == [
        ("后端", None), ("存储", "后端"), ("前端", None)]
    # Only tasks run, in the order they were planned; each knows its innermost part.
    assert [(node.title, titles[node.part_id]) for node in result.nodes] == [
        ("数据模型", "存储"), ("数据库读写", "存储"), ("接口", "后端"), ("页面", "前端"), ("检查", "前端")]
    # Waiting for a part means waiting for its last tasks.
    assert {node.title: [titles[item] for item in node.dependencies] for node in result.nodes} == {
        "数据模型": [], "数据库读写": ["数据模型"], "接口": ["数据库读写"], "页面": ["接口"], "检查": ["页面", "接口"]}
    assert [titles[item] for item in result.nodes[-1].verifies] == ["页面"]
    # Depth first: a part's own parts are split before the next major part.
    messages = [message for _, _, message in leads.messages]
    assert "major parts" in messages[0] and "independent" in messages[0]
    assert messages[1].startswith('Split part "后端" (后端)') and '"kind": "part"' in messages[1]
    assert messages[2].startswith('Split part "存储" (后端 › 存储)')
    assert messages[3].startswith('Split part "前端"')
    assert "逐级拆分" in result.planning_note


async def test_the_deepest_level_must_be_tasks(monkeypatch):
    monkeypatch.setattr(outline, "MAX_DEPTH", 1)
    leads = Leads([outline_reply(part("p", "全部")), split(part("s", "还想再拆")), split(task("t", "做完"))])
    result = await autonomous_plan("小事", LEAD_MODELS, LEADS_CONFIGURED, leads, session=SupervisorSession())
    assert [node.title for node in result.nodes] == ["做完"]
    asked, refused = leads.messages[1][2], leads.messages[2][2]
    assert "every child must be a task" in asked and '"kind": "part"' not in asked
    assert "not a valid plan" in refused and "deepest level" in refused


async def test_vague_tasks_go_back_to_the_lead():
    vague = task("t", "随便做做", acceptance_criteria="")
    leads = Leads([outline_reply(part("p", "全部")), split(vague), split(task("t", "做完"))])
    result = await autonomous_plan("小事", LEAD_MODELS, LEADS_CONFIGURED, leads, session=SupervisorSession())
    assert "随便做做: no acceptance_criteria" in leads.messages[2][2]
    assert [node.title for node in result.nodes] == ["做完"]


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

"""The platform engineer changes Super Partner itself: a component is planned, approved, built in a
copy, checked, approved again and applied; a repair applies once checked; the lead may ask for a
component, and hears how it went."""
import asyncio
import subprocess

import pytest
from super_partner import engineer, supervisor
from super_partner import platform_repair as platform
from super_partner.config import settings
from super_partner.db import Database
from super_partner.engineer import EngineerError, commit, read_reply
from super_partner.executor import Executor
from super_partner.models import NodeStatus, RunStatus
from super_partner.providers import ProviderResult
from test_leading import MODELS, Team, decision
from test_platform_repair import edit_candidate, passing_check, source_checkout  # noqa: F401

PLAN = "- 目标: 加一个导出按钮\n- 改动: frontend/x.tsx\n- 测试: 导出测试\n- 风险: 无\nSTATUS: PLAN"


class Engineer:
    """Plans with PLAN; builds by editing the copy (edit_candidate)."""

    def __init__(self, plan=PLAN):
        self.plan, self.prompts = plan, []

    def configured_models(self, ids):
        return set(ids)

    async def complete(self, model_id, prompt, workspace=None, **kwargs):
        self.prompts.append((prompt, workspace))
        assert kwargs["repair"] is False
        if prompt.startswith("[[super-partner:platform-engineer:plan]]"):
            return ProviderResult(self.plan, 0, 0, model_id)
        edit_candidate({"folder": str(workspace.parent)})
        return ProviderResult("改了 api.py，加了回归测试。\nSTATUS: FIXED", 0, 0, model_id)


@pytest.fixture
def setup(tmp_path, source_checkout, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "platform_root", source_checkout)
    monkeypatch.setattr(settings, "platform_self_repair", True)
    monkeypatch.setattr(platform.PlatformRepairs, "_check_command", lambda self, *a, **k: passing_check())
    guide = source_checkout / "skills/platform-engineer/SKILL.md"
    guide.parent.mkdir(parents=True)
    guide.write_text("---\nname: platform-engineer\n---\n# Adding a component to Super Partner\n")
    activated = []

    def build(providers=None, db=None):
        executor = Executor(db or Database(tmp_path / "test.db"), providers or Engineer(), MODELS)

        async def capture(job):
            activated.append(job)

        monkeypatch.setattr(executor, "_activate_platform", capture)
        return executor

    return build, activated, source_checkout


async def idle(executor):
    while executor.engineer_task and not executor.engineer_task.done():
        await asyncio.sleep(0.01)
    await asyncio.sleep(0)


async def test_a_component_is_planned_approved_built_checked_and_applied_only_when_approved(setup):
    build, activated, root = setup
    providers = Engineer()
    executor = build(providers)
    job = executor.engineer_request("给结果页加一个导出按钮")
    await idle(executor)
    job = executor.db.get_engineer_job(job.id)
    assert job.status == "plan_ready" and "导出按钮" in job.plan and "STATUS" not in job.plan
    # Planning read a copy, which is gone, and edited nothing.
    planned_in = providers.prompts[0][1]
    assert planned_in != root and not planned_in.exists()
    assert "# Adding a component to Super Partner" in providers.prompts[0][0]
    assert "broken" in (root / "backend/super_partner/api.py").read_text()

    executor.engineer_approve(job.id)
    await idle(executor)
    job = executor.db.get_engineer_job(job.id)
    assert job.status == "ready" and "+VALUE = 'fixed'" in job.diff
    assert "tests/test_new_regression.py" in job.files and job.checks
    assert "Approved plan:" in providers.prompts[-1][0] and "导出按钮" in providers.prompts[-1][0]
    assert not activated and "broken" in (root / "backend/super_partner/api.py").read_text()

    executor.engineer_approve(job.id)
    job = executor.db.get_engineer_job(job.id)
    await asyncio.sleep(0)
    assert job.status == "applying" and executor.platform_pending["engineer_job_id"] == job.id
    assert [item["id"] for item in activated] == [job.repair_id]


async def test_an_applied_change_is_committed_and_the_job_ends(setup, monkeypatch):
    build, _, _ = setup
    executor = build()
    job = executor.engineer_request("修复：状态显示错误", kind="repair")
    await idle(executor)
    job = executor.db.get_engineer_job(job.id)
    # A repair needs no approval: once checked it is applied.
    assert job.status == "applying" and executor.platform_pending
    commits = []
    monkeypatch.setattr(engineer, "commit", lambda root, files, message: commits.append((files, message)) or "abc12345ff")
    record = dict(executor.platform_pending, status="activated")
    executor._finish_platform(record)
    job = executor.db.get_engineer_job(job.id)
    assert job.status == "applied" and job.commit == "abc12345ff" and job.finished_at
    assert commits[0][0] == job.files and "platform engineer" in commits[0][1]
    assert executor.platform_pending is None


async def test_the_user_can_send_a_plan_back_and_give_a_job_up(setup):
    build, _, _ = setup
    providers = Engineer()
    executor = build(providers)
    job = executor.engineer_request("加一个导出按钮")
    await idle(executor)
    executor.engineer_reply(job.id, "导出成 CSV，不要 Excel")
    await idle(executor)
    prompt = providers.prompts[-1][0]
    assert "导出成 CSV" in prompt and "Your previous plan" in prompt
    assert executor.db.get_engineer_job(job.id).status == "plan_ready"
    job = await executor.engineer_reject(job.id)
    assert job.status == "rejected"
    with pytest.raises(EngineerError):
        executor.engineer_approve(job.id)


async def test_a_change_made_against_older_source_is_redone_instead_of_overwriting(setup):
    build, activated, root = setup
    executor = build()
    job = executor.engineer_request("加一个导出按钮")
    await idle(executor)
    executor.engineer_approve(job.id)
    await idle(executor)
    first = executor.db.get_engineer_job(job.id).repair_id
    (root / "backend/super_partner/api.py").write_text("VALUE = 'broken'\n# someone else's edit\n")
    executor.engineer_approve(job.id)
    await idle(executor)
    job = executor.db.get_engineer_job(job.id)
    assert not activated and job.status == "ready" and job.repair_id != first
    assert any("源码在这期间有了更新" in item.text for item in job.messages)


async def test_the_engineer_asks_when_a_choice_is_the_users(setup):
    build, _, _ = setup
    executor = build(Engineer(plan='要接哪家？\nQUESTION: 接入哪个厂商？\nOPTIONS: ["DeepSeek", "Kimi"]\nSTATUS: NEEDS_INPUT'))
    job = executor.engineer_request("接入一个新的 API 厂商")
    await idle(executor)
    job = executor.db.get_engineer_job(job.id)
    assert (job.status, job.question, job.choices) == ("needs_input", "接入哪个厂商？", ["DeepSeek", "Kimi"])


async def test_retrospective_platform_changes_apply_automatically_only_after_checks(setup):
    build, activated, _ = setup
    executor = build()
    job = executor.engineer_request("改进拆分规则并增加验证", origin="retrospective", auto_apply=True,
                                    improvement_id="lesson")
    await idle(executor)
    job = executor.db.get_engineer_job(job.id)
    assert job.status == "applying" and job.checks and job.plan
    assert len(activated) == 1 and activated[0]["status"] == "validated"


async def test_automatic_platform_change_never_applies_a_failed_check(setup, monkeypatch):
    build, activated, _ = setup
    executor = build()

    async def fail(record):
        raise platform.RepairError("regression still fails")

    monkeypatch.setattr(executor.platform, "check", fail)
    job = executor.engineer_request("修正规则", origin="retrospective", auto_apply=True)
    await idle(executor)
    assert executor.db.get_engineer_job(job.id).status == "failed" and not activated


async def test_automatic_platform_change_needs_a_valid_plan(setup):
    build, activated, _ = setup
    executor = build(Engineer(plan="I cannot make a plan"))
    job = executor.engineer_request("改进规则", origin="retrospective", auto_apply=True)
    await idle(executor)
    assert executor.db.get_engineer_job(job.id).status == "failed" and not activated


def test_replies_are_read_with_their_status_and_question():
    assert read_reply(PLAN)[1] == "plan" and read_reply(PLAN)[0].endswith("风险: 无")
    assert read_reply("改好了\nSTATUS: FIXED")[:2] == ("改好了", "fixed")


def test_requests_need_an_available_engineer_and_words(tmp_path, monkeypatch):
    executor = Executor(Database(tmp_path / "test.db"), Engineer(), MODELS)
    with pytest.raises(EngineerError, match="不可用"):
        executor.engineer_request("加一个按钮")


def test_commit_takes_only_the_changed_files(tmp_path):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], capture_output=True, text=True, check=True).stdout

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "a.py").write_text("a = 1\n")
    (tmp_path / "staged.py").write_text("s = 1\n")
    git("add", "a.py", "staged.py")
    git("commit", "-qm", "start")
    (tmp_path / "a.py").write_text("a = 2\n")
    (tmp_path / "new.py").write_text("n = 1\n")
    (tmp_path / "staged.py").write_text("s = 2\n")
    git("add", "staged.py")
    sha = commit(tmp_path, ["a.py", "new.py"], "平台改动")
    assert git("rev-parse", "HEAD").strip() == sha
    assert sorted(git("show", "--name-only", "--format=", "HEAD").split()) == ["a.py", "new.py"]
    # The user's own staged change stays staged, outside the commit.
    assert git("diff", "--cached", "--name-only").split() == ["staged.py"]


def test_a_component_request_needs_to_say_what_to_add():
    assert supervisor.parse_decision('{"action": "request_component", "instructions": "加一个 PDF 工具"}', set())
    assert supervisor.parse_decision('{"action": "request_component"}', set()) is None


async def test_the_lead_asks_for_a_component_and_decides_again_when_it_is_done(database, setup, monkeypatch):
    build, _, _ = setup

    builder = Engineer()

    class LeadAndEngineer(Team):
        async def complete(self, model_id, prompt, workspace=None, **kwargs):
            if prompt.startswith("[[super-partner:platform-engineer:"):
                return await builder.complete(model_id, prompt, workspace, **kwargs)
            return await super().complete(model_id, prompt, workspace, **kwargs)

    team = LeadAndEngineer([decision("request_component", instructions="给执行模型加一个读 PDF 的工具"),
                            decision("unable", user_action="换成文本格式的资料")], fails=10)
    executor = build(team, database)
    await executor.execute("r")
    run = database.get_run("r")
    assert run.status == RunStatus.PAUSED and run.engineer_job_id
    assert run.decisions[0].engineer_job_id == run.engineer_job_id
    await idle(executor)
    job = database.get_engineer_job(run.engineer_job_id)
    assert (job.origin, job.status, job.run_id) == ("lead", "plan_ready", "r")
    assert "The lead of a running task asks for it" in builder.prompts[0][0]
    assert "读 PDF 的工具" in builder.prompts[0][0]
    await executor.engineer_reject(job.id)
    for _ in range(100):
        if database.get_run("r").plan.nodes[0].status == NodeStatus.UNABLE:
            break
        await asyncio.sleep(0.01)
    run = database.get_run("r")
    assert "EVENT: the component you asked for" in team.lead[-1] and "gave it up" in team.lead[-1]
    assert run.engineer_job_id is None and run.plan.nodes[0].status == NodeStatus.UNABLE
    assert [item.event for item in run.decisions] == ["attempt_failed", "engineer_done"]


async def test_a_platform_repair_the_lead_asked_for_goes_back_to_it_once_applied(database, setup, monkeypatch):
    build, _, _ = setup
    executor = build(Team([]), database)
    run = database.get_run("r")
    from super_partner.models import Diagnosis

    run.status, run.maintenance_id = RunStatus.PAUSED, "repair1"
    run.plan.nodes[0].status = NodeStatus.PENDING
    run.diagnoses = [Diagnosis(id="d", trigger="lead_repair", node_id="a", decision_id="x", status="done",
                               repair_id="repair1")]
    database.save_run(run)
    asked = []

    async def after_repair(run_id, node_id, diagnosis_id, verdict, outcome):
        asked.append((run_id, node_id, diagnosis_id, database.get_run(run_id).diagnoses[0].verdict))
        return True

    monkeypatch.setattr(executor, "_after_repair", after_repair)
    monkeypatch.setattr(executor, "start", lambda run_id: asked.append(("started", run_id)))
    executor._finish_platform({"id": "repair1", "status": "activated", "run_id": "r", "files": ["backend/x.py"]})
    await asyncio.sleep(0.05)
    # The lead heard first; only after its decision does the task go on.
    assert asked == [("r", "a", "d", "platform_fixed"), ("started", "r")]


async def test_the_api_lists_jobs_and_refuses_what_the_engineer_cannot_take(task_api):
    _, client = task_api
    overview = (await client.get("/api/engineer")).json()
    assert overview["available"] is False and overview["jobs"] == []
    refused = await client.post("/api/engineer/jobs", json={"request": "加一个按钮"})
    assert refused.status_code == 409 and "不可用" in refused.json()["detail"]
    assert (await client.post("/api/engineer/jobs/nope/approve")).status_code == 409


def test_a_call_for_the_engineer_is_read_from_a_chat_answer_and_a_decision():
    text, call = supervisor.split_engineer('沙箱连不到宿主机的许可证端口，我请平台工程师来修。\n'
                                           'ENGINEER: {"kind": "repair", "request": "放行许可证端口 26585"}')
    assert text == "沙箱连不到宿主机的许可证端口，我请平台工程师来修。"
    assert call == {"kind": "repair", "request": "放行许可证端口 26585"}
    assert supervisor.split_engineer("没有要找平台工程师的事")[1] is None
    made = supervisor.parse_decision('{"action": "accept", "reason": "代码写好了", '
                                     '"engineer": {"kind": "component", "request": "加 EDA 许可证检查"}}', set())
    assert made["engineer"] == {"kind": "component", "request": "加 EDA 许可证检查"}


async def test_the_lead_brings_the_engineer_into_the_chat_and_hears_how_it_goes(database, setup):
    build, _, _ = setup

    class Lead(Team):
        async def converse(self, session, prompt, spend_ok=None, timeout=600):
            self.lead.append(prompt)
            return ProviderResult('是沙箱的问题。\nENGINEER: {"kind": "repair", "request": "让步骤沙箱能连宿主机许可证端口"}',
                                  0, 0, session.model_id)

    team = Lead([])
    executor = build(team, database)
    monkey_kick = executor._engineer_kick
    executor._engineer_kick = lambda: None  # keep the job queued for the test
    assert executor.start_lead_chat("r", "应该是你沙箱的问题，请平台工程师修")
    await executor.lead_tasks["r"]
    run = database.get_run("r")
    reply = run.lead_messages[-1]
    assert reply.text.startswith("是沙箱的问题。") and "ENGINEER:" not in reply.text and "已请平台工程师加入" in reply.text
    job = database.get_engineer_job(reply.engineer_job_id)
    assert (job.origin, job.kind, job.run_id, job.status) == ("lead", "repair", "r", "queued")
    assert "ENGINEER: {" in team.lead[0]  # it was told how to bring the engineer in
    # Its next message to the lead starts with what the engineer did, once.
    job.status = "applied"
    database.save_engineer_job(job)
    await executor.ask_lead("r", "还好吗")
    await executor.ask_lead("r", "现在呢")
    assert team.lead[1].startswith("UPDATE from the platform engineer") and '"让步骤沙箱能连宿主机许可证端口": applied' in team.lead[1]
    assert not team.lead[2].startswith("UPDATE")
    executor._engineer_kick = monkey_kick


async def test_a_decision_can_bring_the_engineer_in_without_holding_the_step(database, setup):
    build, _, _ = setup
    team = Team([decision("accept", engineer={"kind": "repair", "request": "步骤沙箱连不到许可证服务"})])
    executor = build(team, database)
    executor._engineer_kick = lambda: None
    await executor.execute("r")
    run = database.get_run("r")
    assert run.status == RunStatus.SUCCEEDED and run.engineer_job_id is None
    job = database.get_engineer_job(run.decisions[0].engineer_job_id)
    assert (job.origin, job.node_id, job.request) == ("lead", "a", "步骤沙箱连不到许可证服务")


async def test_the_user_brings_the_engineer_into_a_task(task_api):
    from super_partner.models import ExecutionRun, TaskPlan

    api, client = task_api
    assert (await client.post("/api/tasks/nope/engineer", json={"request": "修沙箱"})).status_code == 404
    api.db.save_run(ExecutionRun(id="t", prompt="p", status=RunStatus.PAUSED, budget_usd=1,
                                 plan=TaskPlan(summary="s", nodes=[])))
    refused = await client.post("/api/tasks/t/engineer", json={"request": "修沙箱", "kind": "repair"})
    assert refused.status_code == 409 and "不可用" in refused.json()["detail"]
    api.executor.engineer_available = lambda: True
    api.executor._engineer_kick = lambda: None
    joined = await client.post("/api/tasks/t/engineer", json={"request": "让步骤沙箱能连许可证端口", "kind": "repair"})
    assert joined.status_code == 201 and joined.json()["run_id"] == "t" and joined.json()["origin"] == "user"
    listed = (await client.get("/api/engineer", params={"run_id": "t"})).json()["jobs"]
    assert [job["id"] for job in listed] == [joined.json()["id"]]
    assert (await client.get("/api/engineer", params={"run_id": "other"})).json()["jobs"] == []

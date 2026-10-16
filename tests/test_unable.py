import json

from super_partner import quality
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import ExecutionRun, NodeStatus, RunStatus, TaskNode, TaskPlan
from super_partner.providers import ProviderResult
from test_diagnosis import MODELS, ORIGIN

DEPLOY = "部署脚本 deploy.sh 已就绪，配置也写好了。\nUNABLE: 沙箱不能联网，没有实际部署到服务器；允许联网并提供服务器凭据后可以部署。"


def node(node_id, title, deps=()):
    return TaskNode(id=node_id, title=title, instructions=title, capability="coding", model_id="x:api", dependencies=list(deps))


def deploy_run(database: Database) -> None:
    nodes = [node("build", "构建网站"), node("deploy", "部署上线", ["build"]),
             node("check", "检查线上页面", ["deploy"]), node("docs", "写使用说明")]
    database.save_run(ExecutionRun(id="r", prompt="做一个网站并部署", status=RunStatus.QUEUED, budget_usd=5,
                                   use_cli=True, plan=TaskPlan(summary="网站", nodes=nodes)))


class Workers:
    """Deploying needs the internet; everything else works. ``review`` answers reviews."""

    def __init__(self, deploy=DEPLOY, review=None, diagnosis=None):
        self.deploy, self.review, self.diagnosis = deploy, review, diagnosis
        self.calls, self.prompts = [], []

    def configured_models(self, ids):
        return set(ids)

    async def complete(self, model_id, prompt, workspace=None, **kwargs):
        if kwargs.get("repair"):
            self.calls.append("diagnose")
            return ProviderResult(self.diagnosis, 0, 0, model_id)
        if prompt.startswith(quality.REVIEW_MARKER):
            return ProviderResult(json.dumps(self.review(prompt)), 0, 0, model_id)
        self.prompts.append(prompt)
        title = prompt.split("\n", 1)[0].removeprefix("Task: ")
        self.calls.append(title)
        return ProviderResult(self.deploy if title == "部署上线" else f"{title}完成", 0, 0, model_id)


def configure(tmp_path, monkeypatch, review=False, diagnose=False):
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "review_nodes", review)
    monkeypatch.setattr(settings, "auto_diagnose", diagnose)
    database = Database(tmp_path / "test.db")
    deploy_run(database)
    return database


async def test_a_step_that_cannot_be_done_ends_the_task_incomplete_with_its_reason(tmp_path, monkeypatch):
    database = configure(tmp_path, monkeypatch)
    workers = Workers()
    await Executor(database, workers, MODELS).execute("r")

    run = database.get_run("r")
    nodes = {item.id: item for item in run.plan.nodes}
    assert run.status == RunStatus.INCOMPLETE
    assert {k: v.status for k, v in nodes.items()} == {
        "build": NodeStatus.SUCCEEDED, "deploy": NodeStatus.UNABLE, "check": NodeStatus.BLOCKED, "docs": NodeStatus.SUCCEEDED}
    # One honest attempt: retrying cannot bring the network back.
    assert workers.calls.count("部署上线") == 1 and "检查线上页面" not in workers.calls
    assert "沙箱不能联网" in nodes["deploy"].unable_reason and nodes["deploy"].attempts[-1].outcome == "unable"
    assert run.final_result.startswith("## 未能完成") and "沙箱不能联网" in run.final_result
    assert "「检查线上页面」" in run.final_result and "写使用说明完成" in run.final_result
    assert "部署上线" in run.error
    # Every worker is told not to fake what it cannot do.
    assert all("UNABLE:" in prompt for prompt in workers.prompts)


async def test_a_reviewer_does_not_pass_work_that_only_prepared_the_outcome(tmp_path, monkeypatch):
    database = configure(tmp_path, monkeypatch, review=True)

    def review(prompt):
        if "部署脚本" in prompt:
            return {"passed": True, "reason": "脚本写得不错", "unable": "没有联网，网站实际没有部署"}
        return {"passed": True, "reason": "ok"}

    workers = Workers(deploy="部署脚本 deploy.sh 已就绪，联网后运行即可。", review=review)
    await Executor(database, workers, MODELS).execute("r")
    run = database.get_run("r")
    deploy = next(item for item in run.plan.nodes if item.id == "deploy")
    assert run.status == RunStatus.INCOMPLETE and deploy.status == NodeStatus.UNABLE
    assert deploy.review_passed is False and "实际没有部署" in deploy.unable_reason
    assert "only prepared" in quality.review_prompt(deploy, "x", [], "", False)


async def test_the_diagnosis_tries_to_lift_the_limit_and_its_verdict_is_kept(tmp_path, monkeypatch):
    database = configure(tmp_path, monkeypatch, diagnose=True)
    blocked = ("服务器凭据不在这台机器上，无法代为部署。\nUSER_ACTION: CREDENTIALS: 提供部署服务器的 SSH 密钥\nSTATUS: BLOCKED")
    workers = Workers(diagnosis=blocked)
    await Executor(database, workers, MODELS).execute("r")
    run = database.get_run("r")
    deploy = next(item for item in run.plan.nodes if item.id == "deploy")
    # Not paused waiting for the user: the task ends, saying what is missing.
    assert run.status == RunStatus.INCOMPLETE and run.waiting_diagnosis_id is None
    assert "需要你处理：CREDENTIALS: 提供部署服务器的 SSH 密钥" in deploy.unable_reason
    assert "服务器凭据不在这台机器上" in deploy.unable_reason
    assert workers.calls.count("diagnose") == 1 and workers.calls.count("部署上线") == 1


async def test_a_wrong_claim_of_inability_is_retried(tmp_path, monkeypatch):
    database = configure(tmp_path, monkeypatch, diagnose=True)
    workers = Workers(diagnosis="部署只是写入本地目录，不需要网络。\nNEXT_STEP: 直接复制到 public/\nSTATUS: RETRY")
    replies = iter([DEPLOY, "已复制到 public/"])
    original = workers.complete

    async def complete(model_id, prompt, workspace=None, **kwargs):
        workers.deploy = next(replies) if prompt.startswith("Task: 部署上线") else workers.deploy
        return await original(model_id, prompt, workspace, **kwargs)

    workers.complete = complete
    await Executor(database, workers, MODELS).execute("r")
    run = database.get_run("r")
    assert run.status == RunStatus.SUCCEEDED
    deploy = next(item for item in run.plan.nodes if item.id == "deploy")
    assert [a.outcome for a in deploy.attempts] == ["unable", "passed"]
    assert "直接复制到 public/" in [p for p in workers.prompts if p.startswith("Task: 部署上线")][-1]


async def test_an_incomplete_task_can_be_resumed_or_changed_once_the_limit_is_lifted(task_api, monkeypatch):
    api, client = task_api
    monkeypatch.setattr(settings, "auto_diagnose", False)
    monkeypatch.setattr(settings, "review_nodes", False)
    deploy_run(api.db)
    executor = Executor(api.db, Workers(), MODELS)
    monkeypatch.setattr(api, "executor", executor)
    monkeypatch.setattr(api, "models", MODELS)
    monkeypatch.setattr(api.providers, "configured_models", lambda ids: set(ids))
    await executor.execute("r")
    assert api.db.get_run("r").status == RunStatus.INCOMPLETE

    started = []
    monkeypatch.setattr(executor, "start", lambda run_id: started.append(run_id))
    response = await client.post("/api/tasks/r/resume", json={}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    run = api.db.get_run("r")
    nodes = {item.id: item for item in run.plan.nodes}
    assert nodes["deploy"].status == nodes["check"].status == NodeStatus.PENDING
    assert "上次这一步无法完成" in nodes["deploy"].feedback and nodes["deploy"].unable_reason is None
    assert nodes["build"].status == NodeStatus.SUCCEEDED and started == ["r"]


def test_a_step_is_told_which_work_belongs_to_other_steps():
    from super_partner.models import ExecutionRun as Run

    nodes = [node("build", "构建网站"), node("deploy", "部署上线", ["build"])]
    run = Run(id="r", prompt="p", status=RunStatus.RUNNING, budget_usd=1, plan=TaskPlan(summary="s", nodes=nodes))
    prompt, _ = quality.node_prompt(nodes[0], run, 4000)
    assert "Other steps of this task, not yours: 部署上线" in prompt and "Judge only this step" in prompt
    assert "only this task" in quality.review_prompt(nodes[0], "x", [], "", False)

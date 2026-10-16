"""The task's lead keeps one conversation from planning to the end of the task."""
import json
import os
from datetime import datetime, timezone

import httpx
import pytest
from super_partner import supervisor
from super_partner.cli_channels import CliChannelRegistry, SessionLost
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import (
    CapabilityScore,
    ExecutionRun,
    ModelProfile,
    RunStatus,
    SupervisorSession,
    TaskNode,
    TaskPlan,
)
from super_partner.planner import autonomous_plan
from super_partner.providers import ProviderRegistry, ProviderResult
from super_partner.quota import ModelUnavailable


def unavailable(model_id):
    return ModelUnavailable(model_id, datetime.now(timezone.utc), "额度用完")

PLAN = json.dumps({"summary": "待办网页", "nodes": [
    {"key": "a", "title": "页面", "instructions": "写页面", "capability": "coding", "difficulty": "normal"},
    {"key": "b", "title": "检查", "instructions": "检查页面", "capability": "coding", "difficulty": "easy",
     "dependencies": ["a"]}]})


def model(model_id, planning, channel="api"):
    return ModelProfile(id=model_id, provider=model_id.split(":")[0], display_name=model_id, channel=channel,
                        context_window=1_000_000, input_per_million=1, output_per_million=1,
                        execution_priority=0 if channel == "cli" else 100,
                        scores=CapabilityScore(coding=90, planning=planning, reliability=90))


MODELS = [model("x:api", 99), model("cli:claude", 90, "cli"), model("cli:codex", 92, "cli"),
          model("cli:gemini", 99, "cli"), model("mock:local", 100)]
CONFIGURED = {item.id for item in MODELS}


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")


def test_leads_are_cli_conversations_first_then_api_models():
    assert [item.id for item in supervisor.candidates(MODELS, CONFIGURED)] == ["cli:codex", "cli:claude", "x:api"]


class Leads:
    """Each model keeps conversations by id; ``fail`` maps a model to the error it raises."""

    def __init__(self, replies=None, fail=None):
        self.replies, self.fail = list(replies or []), dict(fail or {})
        self.messages = []

    def configured_models(self, ids):
        return set(ids)

    async def converse(self, session, prompt, spend_ok=None, timeout=600):
        self.messages.append((session.model_id, session.session_id, prompt))
        if error := self.fail.pop(session.model_id, None):
            raise error
        session.session_id = session.session_id or f"{session.model_id}-conversation"
        session.calls += 1
        return ProviderResult(self.replies.pop(0) if self.replies else "好的", 0, 0, session.model_id)


async def test_the_lead_plans_in_the_conversation_it_keeps():
    session = SupervisorSession()
    leads = Leads(["我先想想……", PLAN])
    plan = await autonomous_plan("写一个待办网页", MODELS, CONFIGURED, leads, session=session)
    assert [node.title for node in plan.nodes] == ["页面", "检查"] and plan.planner_model_id == "cli:codex"
    assert (session.model_id, session.session_id) == ("cli:codex", "cli:codex-conversation")
    # It was told it leads the task, and asked again in the same conversation for a valid plan.
    (_, first_id, first), (_, second_id, second) = leads.messages
    assert first.startswith(supervisor.ROLE) and "写一个待办网页" in first
    assert first_id is None and second_id == "cli:codex-conversation" and "not a valid plan" in second


async def test_without_a_lead_the_usual_planner_plans():
    session = SupervisorSession()

    class Planner(Leads):
        async def complete(self, model_id, prompt, *args, **kwargs):
            return ProviderResult(PLAN, 0, 0, model_id)

    leads = Planner(fail={"cli:codex": unavailable("cli:codex"),
                          "cli:claude": unavailable("cli:claude"),
                          "x:api": unavailable("x:api")})
    plan = await autonomous_plan("写一个待办网页", MODELS, CONFIGURED, leads, session=session)
    assert plan.nodes and session.model_id is None


def task(database, lead=None):
    run = ExecutionRun(id="r", prompt="写一个待办网页", status=RunStatus.RUNNING, budget_usd=1, supervisor=lead,
                       plan=TaskPlan(summary="待办网页", nodes=[TaskNode(id="a", title="页面", instructions="写页面",
                                                                       capability="coding")]))
    database.save_run(run)
    return run


async def test_asking_continues_the_saved_conversation(tmp_path):
    database = Database(tmp_path / "test.db")
    task(database, SupervisorSession(model_id="cli:claude", session_id="s1", cwd=str(tmp_path / "lead")))
    leads = Leads(["继续第二步"])
    executor = Executor(database, leads, {item.id: item for item in MODELS})
    assert await executor.ask_lead("r", "第一步通过了，接下来？") == "继续第二步"
    assert leads.messages == [("cli:claude", "s1", "第一步通过了，接下来？")]
    assert database.get_run("r").supervisor.calls == 1
    assert not [e for e in database.events("r") if e.kind == "run.lead_changed"]


async def test_a_lost_conversation_is_rebuilt_from_the_task_record(tmp_path):
    database = Database(tmp_path / "test.db")
    task(database, SupervisorSession(model_id="cli:claude", session_id="gone", cwd=str(tmp_path / "lead")))
    leads = Leads(fail={"cli:claude": SessionLost("No conversation found with session ID: gone")})
    executor = Executor(database, leads, {item.id: item for item in MODELS})
    await executor.ask_lead("r", "第一步通过了，接下来？")
    _lost, rebuilt = leads.messages
    # Same model, new conversation, briefed on the task first.
    assert rebuilt[0] == "cli:claude" and rebuilt[1] is None
    assert "taking over" in rebuilt[2] and "写一个待办网页" in rebuilt[2] and rebuilt[2].endswith("第一步通过了，接下来？")
    lead = database.get_run("r").supervisor
    assert lead.session_id == "cli:claude-conversation" and lead.previous == ["cli:claude"]


async def test_an_unavailable_lead_hands_the_task_to_the_next(tmp_path):
    database = Database(tmp_path / "test.db")
    task(database, SupervisorSession(model_id="cli:codex", session_id="s1", cwd=str(tmp_path / "lead")))
    leads = Leads(fail={"cli:codex": unavailable("cli:codex")})
    executor = Executor(database, leads, {item.id: item for item in MODELS})
    await executor.ask_lead("r", "接下来？")
    assert database.get_run("r").supervisor.model_id == "cli:claude"
    event = [e for e in database.events("r") if e.kind == "run.lead_changed"][-1]
    assert (event.data["model_id"], event.data["previous_model_id"]) == ("cli:claude", "cli:codex")


async def test_an_api_lead_is_sent_its_conversation_and_kept_within_bounds(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    sent = []

    def respond(request):
        sent.append(json.loads(request.content)["messages"][0]["content"])
        return httpx.Response(200, json={"choices": [{"message": {"content": f"回答{len(sent)}"},
                                                      "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    registry = ProviderRegistry()
    registry.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    session = SupervisorSession(model_id="deepseek:deepseek-flash")
    try:
        await registry.converse(session, "规划")
        await registry.converse(session, "第一步通过了")
    finally:
        await registry.client.aclose()
    assert sent[0] == "规划" and "[USER]\n规划\n\n[ASSISTANT]\n回答1" in sent[1] and sent[1].endswith("第一步通过了")
    session.history += [{"role": "user", "content": "x" * 200_000}, {"role": "assistant", "content": "ok"}] * 2
    supervisor.trim(session)
    assert session.history[0]["content"] == "规划" and session.history[2]["content"].startswith("[Earlier events")
    assert sum(len(item["content"]) for item in session.history) <= supervisor.MAX_HISTORY_CHARS + 200


def fake_cli(tmp_path, monkeypatch, name, script):
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(script)
    path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{path.parent}:{os.environ['PATH']}")


async def test_claude_and_codex_continue_their_own_conversations(tmp_path, monkeypatch):
    log = tmp_path / "args.log"
    fake_cli(tmp_path, monkeypatch, "claude", f'#!/bin/sh\necho "$@" >> {log}\ncat > /dev/null\n'
             'echo \'{"result": "好的", "session_id": "c-1"}\'\n')
    fake_cli(tmp_path, monkeypatch, "codex", f'#!/bin/sh\necho "$@" >> {log}\ncat > /dev/null\n'
             'echo \'{"type":"thread.started","thread_id":"x-9"}\'\n'
             'echo \'{"type":"item.completed","item":{"type":"agent_message","text":"收到"}}\'\n')
    cli = CliChannelRegistry()
    text, sid = await cli.converse("cli:claude", "规划", tmp_path)
    assert text == "好的" and sid == "c-1"
    await cli.converse("cli:claude", "接下来？", tmp_path, "c-1")
    assert await cli.converse("cli:codex", "规划", tmp_path) == ("收到", "x-9")
    await cli.converse("cli:codex", "接下来？", tmp_path, "x-9")
    first, second, third, fourth = log.read_text().splitlines()
    assert "--session-id" in first and "--resume c-1" in second and "Edit,Write,NotebookEdit" in first
    assert "--sandbox read-only" in third and "exec resume x-9" in fourth


async def test_a_conversation_the_cli_no_longer_has_is_reported_as_lost(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, "claude", "#!/bin/sh\necho 'No conversation found with session ID: c-1' >&2\nexit 1\n")
    with pytest.raises(SessionLost):
        await CliChannelRegistry().converse("cli:claude", "接下来？", tmp_path, "c-1")

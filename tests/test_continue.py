"""A step's next attempt continues where the last one stopped instead of reading the project again."""
import json
from datetime import timedelta

import httpx
import pytest
from super_partner import progress, quality
from super_partner.agent import Workspace, project_map
from super_partner.budget import BudgetExceeded
from super_partner.executor import Executor
from super_partner.models import NodeAttempt, Outcome, ToolCall
from super_partner.providers import NOT_RUN, Conversation, ProviderRegistry
from test_leading import MODELS
from test_progress import READ, reply

FLASH = "deepseek:deepseek-flash"


def test_the_project_map_lists_files_with_their_lengths(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("a\nb\nc\n")
    (tmp_path / "shots").mkdir()
    (tmp_path / "shots" / "a.png").write_bytes(b"\x89PNG")
    for hidden in (".tmp-verify", "node_modules"):
        (tmp_path / hidden).mkdir()
        (tmp_path / hidden / "x.txt").write_text("x")
    text = project_map(tmp_path)
    assert "src/app.py (3 lines)" in text and "shots/: 1 binary files (.png)" in text
    assert "x.txt" not in text


async def test_a_long_file_is_read_whole_in_one_call(tmp_path):
    (tmp_path / "app.js").write_text("".join(f"line {i} " + "x" * 30 + "\n" for i in range(2500)))
    text = await Workspace(tmp_path).call("read_file", {"path": "app.js"})
    assert "line 2499" in text and "[lines" not in text


class Api:
    """A DeepSeek stand-in: answers in turn from ``replies`` and records each request's messages."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    def __call__(self, request):
        self.requests.append(json.loads(request.content)["messages"])
        status, message = self.replies.pop(0)
        return httpx.Response(status, json=reply(message) if status == 200 else {"error": "bad"})


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    registry = ProviderRegistry()

    async def use(*replies):
        handler = Api(*replies)
        registry.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return handler
    yield registry, use


async def test_the_next_attempt_continues_the_same_conversation(tmp_path, api):
    registry, use = api
    calls = await use((200, {"content": None, "tool_calls": [READ]}), (200, {"content": "第一次做完了"}),
                      (200, {"content": "按要求改好了"}))
    conversation = Conversation()
    await registry.complete(FLASH, "写一个工具", tmp_path, act=True, conversation=conversation)
    conversation.followup = "验证没通过：补上空文件的处理"
    result = await registry.complete(FLASH, "写一个工具（重新开始）", tmp_path, act=True, conversation=conversation)
    sent = calls.requests[-1]
    assert result.text == "按要求改好了"
    assert [m["role"] for m in sent] == ["user", "assistant", "tool", "assistant", "user"]
    assert sent[3]["content"] == "第一次做完了" and sent[4]["content"] == "验证没通过：补上空文件的处理"
    assert "重新开始" not in json.dumps(sent, ensure_ascii=False)


async def test_calls_that_never_ran_are_closed_before_continuing(tmp_path, api):
    registry, use = api
    calls = await use((200, {"content": None, "tool_calls": [READ]}), (200, {"content": "继续做完了"}))
    checks = iter([True, False])  # the budget runs out right after the model asked for a tool
    conversation = Conversation()
    with pytest.raises(BudgetExceeded):
        await registry.complete(FLASH, "写一个工具", tmp_path, act=True, conversation=conversation,
                                spend_ok=lambda *_: next(checks, True))
    conversation.followup = "预算已追加，接着做"
    await registry.complete(FLASH, "写一个工具", tmp_path, act=True, conversation=conversation)
    sent = calls.requests[-1]
    assert [m["role"] for m in sent] == ["user", "assistant", "tool", "user"] and sent[2]["content"] == NOT_RUN


async def test_a_conversation_the_provider_refuses_starts_afresh(tmp_path, api):
    registry, use = api
    calls = await use((200, {"content": "做完了"}), (400, None), (200, {"content": "重新做完了"}))
    conversation = Conversation()
    await registry.complete(FLASH, "写一个工具", tmp_path, act=True, conversation=conversation)
    conversation.followup = "再改一下"
    result = await registry.complete(FLASH, "写一个工具（完整说明）", tmp_path, act=True, conversation=conversation)
    assert result.text == "重新做完了"
    assert len(calls.requests[-1]) == 1 and "完整说明" in calls.requests[-1][0]["content"]


def attempt(model_id="x:api", outcome=Outcome.REJECTED, reason="缺少 --verbose", **fields):
    return NodeAttempt(model_id=model_id, outcome=outcome, reason=reason, turns=3, tool_calls=[
        ToolCall(turn=1, tool="read_file", detail="app.py (from line 1)"),
        ToolCall(turn=2, tool="edit_file", detail="app.py"),
        ToolCall(turn=3, tool="run_command", detail="pytest -q", ok=False),
    ], **fields)


def test_a_fresh_attempt_is_told_what_the_last_one_did(database):
    run = database.get_run("r")
    node = run.plan.nodes[0]
    node.attempts.append(attempt())
    prompt, _ = quality.node_prompt(node, run, 4000)
    assert "Files it wrote or edited (still in the working directory): app.py" in prompt
    assert "failed: pytest -q" in prompt and "Files it read" not in prompt  # app.py was also edited


def test_a_step_continues_only_where_that_still_makes_sense(database):
    executor = Executor(database, None, MODELS)
    run = database.get_run("r")
    node = run.plan.nodes[0]
    node.model_id = "x:api"

    def held():
        executor.conversations[run.id, node.id] = Conversation(model_id="x:api", state={"history": [{"role": "user"}]})
        return executor._conversation(run, node).followup

    node.attempts = [attempt()]
    assert "continues in a new attempt" in held() and "缺少 --verbose" in held()
    node.attempts = [attempt(reason=progress.STOPPED + "一直在重复同一条命令")]
    assert held() is None
    node.attempts = [attempt(outcome=Outcome.SUPERSEDED)]
    assert held() is None
    node.attempts, node.model_id = [attempt()], "cli:claude"
    assert held() is None
    node.model_id = "x:api"
    upstream = run.plan.nodes[0].model_copy(update={"id": "up", "attempts": [
        attempt(finished_at=node.attempts[-1].finished_at + timedelta(minutes=1))]})
    run.plan.nodes.append(upstream)
    node.dependencies = ["up"]
    assert held() is None

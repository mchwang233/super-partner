"""Output caps and truncation for API models: a reasoning model that spends a small cap on
thinking must not look like one that finished without doing anything."""
import asyncio
import json

import httpx
import pytest
from super_partner.budget import ProviderError
from super_partner.config import settings
from super_partner.models import ModelProfile
from super_partner.providers import ProviderRegistry


def registry_with(replies: list, sent: list[dict]) -> ProviderRegistry:
    replies = iter(replies)

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        reply = next(replies)
        return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)

    registry = ProviderRegistry()
    registry.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return registry


def deepseek(message: dict, finish_reason: str) -> dict:
    return {"choices": [{"message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4096}}


WRITE_CALL = {"id": "c1", "type": "function", "function": {
    "name": "Write", "arguments": json.dumps({"file_path": "mail.py", "content": "ok"})}}


@pytest.mark.parametrize("provider", ["deepseek", "anthropic", "google"])
@pytest.mark.parametrize("tool", ["Write", "Read"])
@pytest.mark.parametrize("requested", [3, 4])
async def test_hard_tool_cap_counts_each_batch_item_before_side_effects(tmp_path, monkeypatch, provider, tool, requested):
    from artifact_helpers import contract_node, setup_executor
    from super_partner.agent import Workspace
    from super_partner.budget import StepLimitExceeded
    from super_partner.providers import Conversation
    from super_partner.secrets import PROVIDER_ENV

    monkeypatch.setenv(PROVIDER_ENV[provider], "local-test")
    node = contract_node(contract={"max_turns": 3})
    executor, db, _ = setup_executor(tmp_path, monkeypatch, [node])
    calls = [(str(i), tool, {"file_path": f"tool-{i}.txt", "content": f"written {i}"}) for i in range(requested)]
    if tool == "Read":
        for _, _, args in calls[1:]:  # a failed first read still consumes one invocation
            (tmp_path / args["file_path"]).write_text("input")
    if provider == "anthropic":
        response = anthropic_reply([{"type": "tool_use", "id": key, "name": name, "input": args}
                                    for key, name, args in calls], "tool_use")
        done = anthropic_reply([{"type": "text", "text": "done"}])
    elif provider == "google":
        response = {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": name, "args": args}} for _, name, args in calls]}}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}
        done = {"candidates": [{"content": {"parts": [{"text": "done"}]}}]}
    else:
        response = deepseek({"tool_calls": [
            {"id": key, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for key, name, args in calls]}, "tool_calls")
        done = deepseek({"content": "done"}, "stop")
    sent, invoked, finished = [], [], []
    registry = registry_with([response, done], sent)
    api = executor.model_lookup["test:worker"].model_copy(update={"id": f"{provider}:worker", "provider": provider})
    registry.models = executor.model_lookup = {api.id: api}
    executor.providers = registry
    run = db.get_run("artifacts")
    node = run.plan.nodes[0]
    node.model_id = api.id
    db.save_run(run)
    original_run = Workspace.run

    async def observed_tool(self, name, args):
        invoked.append(args["file_path"])
        await asyncio.sleep(0.01)  # admitted read siblings must settle before a rejection returns
        result = await original_run(self, name, args)
        finished.append(args["file_path"])
        return result

    monkeypatch.setattr(Workspace, "run", observed_tool)
    conversation, log = Conversation(), []
    try:
        call = executor._complete(run, node, api.id, "work", tmp_path, act=True, tool_log=log,
                                  tool_trace=tmp_path / "trace.jsonl", conversation=conversation)
        if requested > 3:
            with pytest.raises(StepLimitExceeded, match="工具调用上限"):
                await call
            assert any("工具未执行" in output for _, _, output in conversation.state["results"])
        else:
            assert (await call).turn_limit
    finally:
        await registry.client.aclose()
    assert len(invoked) == len(finished) == len(log) == 3
    assert "tool-3.txt" not in invoked
    if tool == "Read":
        assert any(not item["ok"] for item in log)
    if tool == "Write":
        assert all((tmp_path / f"tool-{i}.txt").read_text() == f"written {i}" for i in range(3))
        assert not (tmp_path / "tool-3.txt").exists()
    saved = db.get_run(run.id).plan.nodes[0]
    assert saved.contract_tool_calls == 3 and saved.contract_turns == 1
    assert len(sent) == 1  # no extra paid summary at either cap


@pytest.mark.parametrize("model_id,act", [("mock:local", True), ("deepseek:worker", False)])
async def test_direct_constrained_provider_call_cannot_bypass_channel_admission(tmp_path, monkeypatch, model_id, act):
    from artifact_helpers import contract_node, setup_executor
    from super_partner.contracts import ArtifactBlocked

    executor, db, _ = setup_executor(tmp_path, monkeypatch, [contract_node()])
    registry = ProviderRegistry()
    executor.providers = registry
    run = db.get_run("artifacts")
    started = []

    async def cli(*args, **kwargs):
        started.append(True)
        pytest.fail("CLI should not start")

    monkeypatch.setattr(registry.cli, "complete", cli)
    try:
        with pytest.raises(ArtifactBlocked, match="通道"):
            await executor._complete(run, run.plan.nodes[0], model_id, "work", tmp_path, act=act,
                                     tool_trace=tmp_path / "trace.jsonl")
    finally:
        await registry.client.aclose()
    assert started == []
    node = db.get_run(run.id).plan.nodes[0]
    assert node.contract_turns == node.contract_tool_calls == 0


async def test_a_membership_cli_may_take_a_contract_step(tmp_path, monkeypatch):
    # It cannot count tool calls; time and progress checks bound it instead (Executor._timed_watch).
    from artifact_helpers import contract_node, setup_executor

    executor, db, _ = setup_executor(tmp_path, monkeypatch, [contract_node()])
    registry = ProviderRegistry()
    executor.providers = registry
    run = db.get_run("artifacts")
    started = []

    async def cli(*args, **kwargs):
        started.append(True)
        return "done"

    monkeypatch.setattr(registry.cli, "complete", cli)
    try:
        await executor._complete(run, run.plan.nodes[0], "cli:codex", "work", tmp_path, act=True,
                                 tool_trace=tmp_path / "trace.jsonl")
    finally:
        await registry.client.aclose()
    assert started == [True]


class Reserving:
    """A spend check whose reservation caps output, as the task budget does."""

    def __init__(self, cap: int):
        self.cap, self.asked = cap, []

    def __call__(self, *_):
        return True

    async def reserve(self, model_id, payload, maximum):
        self.asked.append(maximum)
        return min(maximum, self.cap)


async def test_requests_ask_for_the_models_own_output_ceiling_and_the_budget_can_lower_it(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setattr(settings, "api_agent_max_output_tokens", 32768)
    sent = []
    done = deepseek({"content": "done"}, "stop")
    registry = registry_with([done] * 4, sent)
    flash = ModelProfile(id="deepseek:deepseek-flash", provider="deepseek", display_name="Flash", max_output_tokens=393216)
    registry.models = {flash.id: flash}
    budget = Reserving(cap=2000)
    try:
        await registry.complete("deepseek:deepseek-flash", "work", tmp_path, act=True)
        await registry.complete("deepseek:deepseek-flash", "work", tmp_path, act=True, spend_ok=budget)
        await registry.complete("deepseek:deepseek-flash", "plain")
        # A model whose ceiling the catalog does not give gets the configured cap.
        await registry.complete("deepseek:deepseek-chat", "work", tmp_path, act=True)
    finally:
        await registry.client.aclose()
    assert sent[0]["max_tokens"] == 393216
    assert budget.asked == [393216] and sent[1]["max_tokens"] == 2000
    assert sent[2]["max_tokens"] == 393216 and "tools" not in sent[2]
    assert sent[3]["max_tokens"] == 32768


async def test_a_length_cut_turn_without_tool_calls_is_an_error_not_an_empty_success(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    sent = []
    registry = registry_with([deepseek({"content": "", "reasoning_content": "thinking..."}, "length")], sent)
    try:
        with pytest.raises(ProviderError, match="截断") as caught:
            await registry.complete("deepseek:deepseek-flash", "work", tmp_path, act=True)
    finally:
        await registry.client.aclose()
    assert caught.value.usage.output_tokens == 4096


async def test_thinking_written_into_the_answer_is_kept_for_the_model_but_not_returned(tmp_path, monkeypatch):
    """MiniMax puts its thinking in the content as <think>...</think>: the tool loop sends it back,
    as MiniMax asks, but the answer the platform reads (a summary, a JSON plan) leaves it out."""
    monkeypatch.setenv("MINIMAX_API_KEY", "test")
    sent = []
    registry = registry_with([
        deepseek({"content": "<think>\nwrite it\n</think>\n", "tool_calls": [WRITE_CALL]}, "tool_calls"),
        deepseek({"content": "<think>\ndone?\n</think>\n\nWrote mail.py"}, "stop"),
        deepseek({"content": '<think>plan</think>{"steps": []}'}, "stop"),
    ], sent)
    try:
        acted = await registry.complete("minimax:MiniMax-M3", "work", tmp_path, act=True)
        plain = await registry.complete("minimax:MiniMax-M3", "plan")
    finally:
        await registry.client.aclose()
    assert acted.text == "Wrote mail.py" and plain.text == '{"steps": []}'
    assert sent[1]["messages"][2]["content"] == "<think>\nwrite it\n</think>\n"


def anthropic_reply(blocks: list, stop: str = "end_turn") -> dict:
    return {"content": blocks, "stop_reason": stop,
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0}}


def breakpoints(body: dict) -> int:
    blocks = [*(body.get("system") or []), *(block for message in body["messages"]
                                              if isinstance(message["content"], list) for block in message["content"])]
    return sum("cache_control" in block for block in blocks)


async def test_anthropic_tool_loops_read_the_conversation_from_the_prompt_cache(tmp_path, monkeypatch):
    """Anthropic caches only up to blocks marked cache_control: the system prompt (with the tools
    before it) and, each turn, the newest message, so the next turn reads all of it from the cache.
    At most four marks are allowed, so older turns lose theirs."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    sent = []
    write = {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": "a.txt", "content": "x"}}
    registry = registry_with([
        anthropic_reply([write], "tool_use"), anthropic_reply([{**write, "id": "t2"}], "tool_use"),
        anthropic_reply([{"type": "text", "text": "done"}]),
    ], sent)
    try:
        result = await registry.complete("anthropic:claude-x", "work", tmp_path, act=True)
    finally:
        await registry.client.aclose()
    assert result.text == "done"
    for body in sent:
        assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}
        assert body["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
        assert breakpoints(body) == 2
    # The marks are added to what is sent, not kept in the conversation.
    assert "cache_control" not in json.dumps(sent[2]["messages"][:-1])


async def test_each_provider_is_asked_for_the_effort_in_its_own_fields(monkeypatch):
    for name in ("ZHIPU_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "MINIMAX_API_KEY"):
        monkeypatch.setenv(name, "test")
    sent = []
    done = deepseek({"content": "ok"}, "stop")
    registry = registry_with([done, done, done, anthropic_reply([{"type": "text", "text": "ok"}]),
                              {"candidates": [{"content": {"parts": [{"text": "ok"}]}}], "usageMetadata": {}}], sent)
    try:
        await registry.complete("glm:glm-5.3", "plan", effort="medium")
        await registry.complete("openai:gpt-x", "plan", effort="max")
        await registry.complete("minimax:MiniMax-M3", "plan")  # no effort asked: none sent
        await registry.complete("anthropic:claude-x", "plan", effort="none")
        await registry.complete("google:gemini-x", "plan", effort="high")
    finally:
        await registry.client.aclose()
    assert sent[0]["reasoning_effort"] == "high" and sent[1]["reasoning_effort"] == "xhigh"  # GLM: low, high or max
    assert "reasoning_effort" not in sent[2]
    assert sent[3]["output_config"] == {"effort": "low"}
    assert sent[4]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "high"}
    assert sent[4]["generationConfig"]["maxOutputTokens"] > 0


async def test_a_model_that_rejects_the_effort_answers_without_it_from_then_on(monkeypatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "test")
    sent = []
    rejected = httpx.Response(400, json={"error": {"code": "1210", "message": "该模型始终思考，不支持关闭思考；请使用 low、high 或 max。"}})
    done = deepseek({"content": "ok"}, "stop")
    registry = registry_with([rejected, done, done], sent)
    try:
        first = await registry.complete("glm:glm-x", "plan", effort="medium")
        second = await registry.complete("glm:glm-x", "plan", effort="medium")
    finally:
        await registry.client.aclose()
    assert first.text == second.text == "ok"
    assert sent[0]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in sent[1] and "reasoning_effort" not in sent[2]


async def test_the_full_trace_keeps_every_call_and_its_whole_output(tmp_path, monkeypatch):
    """The stored tool calls keep the start of the first ones; the trace keeps everything."""
    from super_partner import providers
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setattr(providers, "MAX_LOGGED_CALLS", 1)
    (tmp_path / "big.txt").write_text(("x" * 100 + "\n") * 30 + "END")
    read = {"id": "c1", "type": "function", "function": {"name": "Read", "arguments": json.dumps({"file_path": "big.txt"})}}
    sent, log, trace = [], [], tmp_path / "trace" / "n-1.jsonl"
    registry = registry_with([
        deepseek({"content": None, "tool_calls": [read, {**read, "id": "c2"}]}, "tool_calls"),
        deepseek({"content": "done"}, "stop"),
    ], sent)
    try:
        await registry.complete("deepseek:deepseek-flash", "work", tmp_path, act=True, tool_log=log, tool_trace=trace)
    finally:
        await registry.client.aclose()
    assert len(log) == 1 and "END" not in log[0]["output"]
    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    assert len(calls) == 2 and all(call["output"].endswith("END") and call["tool"] == "Read" for call in calls)


async def test_tool_calls_continue_and_reasoning_goes_back_to_deepseek(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    sent = []
    registry = registry_with([
        deepseek({"content": None, "reasoning_content": "plan", "tool_calls": [WRITE_CALL]}, "tool_calls"),
        deepseek({"content": "Wrote mail.py"}, "stop"),
    ], sent)
    try:
        result = await registry.complete("deepseek:deepseek-flash", "work", tmp_path, act=True)
    finally:
        await registry.client.aclose()
    assert result.text == "Wrote mail.py"
    assert (tmp_path / "mail.py").read_text() == "ok"
    assert sent[1]["messages"][2]["reasoning_content"] == "plan"


async def test_a_model_with_a_lower_output_ceiling_is_retried_with_a_smaller_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setattr(settings, "api_agent_max_output_tokens", 32768)
    sent = []
    rejected = httpx.Response(400, json={"error": {"message": "Invalid max_tokens value, the valid range of max_tokens is [1, 8192]"}})
    registry = registry_with([
        rejected, rejected,
        deepseek({"content": None, "tool_calls": [WRITE_CALL]}, "tool_calls"),
        deepseek({"content": "done"}, "stop"),
    ], sent)
    try:
        result = await registry.complete("deepseek:deepseek-chat", "work", tmp_path, act=True)
    finally:
        await registry.client.aclose()
    assert result.text == "done"
    assert [body["max_tokens"] for body in sent] == [32768, 16384, 8192, 8192]


async def test_anthropic_max_tokens_stop_without_tool_use_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(settings, "api_agent_max_output_tokens", 32768)
    sent = []
    registry = registry_with([{
        "content": [{"type": "thinking", "thinking": "..."}], "stop_reason": "max_tokens",
        "usage": {"input_tokens": 5, "output_tokens": 32768},
    }], sent)
    try:
        with pytest.raises(ProviderError, match="截断"):
            await registry.complete("anthropic:claude-x", "work", tmp_path, act=True)
    finally:
        await registry.client.aclose()
    assert sent[0]["max_tokens"] == 32768


async def test_deepseek_thinks_as_hard_as_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    sent = []
    done = deepseek({"content": "done"}, "stop")
    registry = registry_with([done] * 3, sent)
    plain = ModelProfile(id="deepseek:plain", provider="deepseek", display_name="Plain", supports_reasoning=False)
    registry.models = {plain.id: plain}
    try:
        await registry.complete("deepseek:deepseek-flash", "work", tmp_path, act=True)
        monkeypatch.setattr(settings, "deepseek_reasoning_effort", "none")
        await registry.complete("deepseek:deepseek-flash", "plain")
        # A model known not to reason is not sent the parameter.
        await registry.complete("deepseek:plain", "plain")
    finally:
        await registry.client.aclose()
    assert sent[0]["reasoning_effort"] == "max"
    assert sent[1]["reasoning_effort"] == "none"
    assert "reasoning_effort" not in sent[2]


def priced(model_id, cached=0.003):
    return ModelProfile(id=model_id, provider=model_id.split(":")[0], display_name=model_id, context_window=100_000,
                        input_per_million=0.15, output_per_million=0.6, cached_input_per_million=cached)


def test_input_read_from_the_prompt_cache_costs_the_cache_price():
    from super_partner.budget import token_cost

    model = priced("deepseek:deepseek-flash")
    # 10,000 input tokens of which 9,000 cached, 100 output.
    assert token_cost(model, 10_000, 100, 9_000) == pytest.approx((1_000 * 0.15 + 9_000 * 0.003 + 100 * 0.6) / 1e6)
    # Without a cache price, cached input costs the input price; cached never exceeds input.
    assert token_cost(priced("x:y", cached=None), 10_000, 0, 9_000) == pytest.approx(10_000 * 0.15 / 1e6)
    assert token_cost(model, 100, 0, 500) == pytest.approx(100 * 0.003 / 1e6)


class Recording(Reserving):
    def __init__(self):
        super().__init__(4096)
        self.recorded = []

    async def record(self, model_id, input_tokens, output_tokens, cached_tokens=0):
        self.recorded.append((input_tokens, output_tokens, cached_tokens))


async def test_each_provider_reports_its_cache_hits_and_the_budget_hears_them(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    spend = Recording()
    registry = registry_with([
        {"choices": [{"message": {"content": "好了"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 7159, "completion_tokens": 20, "prompt_cache_hit_tokens": 6912}},
        {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn",
         "usage": {"input_tokens": 300, "output_tokens": 50, "cache_read_input_tokens": 9000,
                   "cache_creation_input_tokens": 700}},
    ], [])
    try:
        ds = await registry.complete("deepseek:deepseek-flash", "hi", spend_ok=spend)
        claude = await registry.complete("anthropic:claude-x", "hi", spend_ok=spend)
    finally:
        await registry.client.aclose()
    assert (ds.input_tokens, ds.cached_input_tokens) == (7159, 6912)
    # Anthropic's input_tokens leaves out what was read from and written to the cache.
    assert (claude.input_tokens, claude.cached_input_tokens) == (10_000, 9000)
    assert spend.recorded == [(7159, 20, 6912), (10_000, 50, 9000)]


def test_openai_compatible_cache_fields():
    from super_partner.providers import cached_tokens

    assert cached_tokens({"prompt_cache_hit_tokens": 12}) == 12
    assert cached_tokens({"prompt_tokens_details": {"cached_tokens": 34}}) == 34
    assert cached_tokens({"prompt_tokens": 5}) == 0

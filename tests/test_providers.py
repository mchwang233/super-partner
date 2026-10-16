"""Output caps and truncation for API models: a reasoning model that spends a small cap on
thinking must not look like one that finished without doing anything."""
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
    "name": "write_file", "arguments": json.dumps({"path": "mail.py", "content": "ok"})}}


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
    assert sent[1]["messages"][1]["reasoning_content"] == "plan"


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

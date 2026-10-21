"""Model replies are streamed, so a model that thinks for long is not mistaken for one that hangs:
a request stops only when nothing arrives for the deadline (streaming.py, providers._post)."""
import asyncio
import json
from typing import Optional

import httpx
import pytest
from super_partner.config import settings
from super_partner.providers import ProviderRegistry
from super_partner.quota import ModelUnavailable


def sse(*payloads) -> bytes:
    return "".join(f"data: {json.dumps(item) if not isinstance(item, str) else item}\n\n" for item in payloads).encode()


def streamed(body, delay: float = 0.0, stall_after: Optional[int] = None):
    """A streamed reply sent piece by piece, ``delay`` apart; after ``stall_after`` pieces it goes silent."""
    pieces = [line + b"\n\n" for line in body.split(b"\n\n") if line]

    async def content():
        for index, piece in enumerate(pieces):
            if stall_after is not None and index == stall_after:
                await asyncio.sleep(30)
            await asyncio.sleep(delay)
            yield piece
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content())


def registry_with(replies: list, sent: list) -> ProviderRegistry:
    replies = iter(replies)

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return next(replies)

    registry = ProviderRegistry()
    registry.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return registry


WRITE = json.dumps({"file_path": "a.txt", "content": "hi"})


async def test_an_openai_compatible_stream_is_put_back_together(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    sent = []
    first = sse(
        {"choices": [{"delta": {"reasoning_content": "write "}}]},
        {"choices": [{"delta": {"reasoning_content": "it"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "Write", "arguments": WRITE[:10]}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": WRITE[10:]}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 60}},
        "[DONE]",
    )
    second = sse({"choices": [{"delta": {"content": "Wrote "}}]}, {"choices": [{"delta": {"content": "a.txt"}, "finish_reason": "stop"}]},
                 {"choices": [], "usage": {"prompt_tokens": 150, "completion_tokens": 5}}, "[DONE]")
    registry = registry_with([streamed(first), streamed(second)], sent)
    try:
        result = await registry.complete("deepseek:deepseek-flash", "work", tmp_path, act=True)
    finally:
        await registry.client.aclose()
    assert (tmp_path / "a.txt").read_text() == "hi"
    assert (result.text, result.input_tokens, result.output_tokens, result.cached_input_tokens) == ("Wrote a.txt", 250, 25, 60)
    body = json.loads(sent[0].content)
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    reply = json.loads(sent[1].content)["messages"][2]
    assert reply["reasoning_content"] == "write it" and reply["tool_calls"][0]["function"]["arguments"] == WRITE


async def test_a_long_reply_that_keeps_coming_is_not_stopped(tmp_path, monkeypatch):
    """Longer in all than the deadline, but never silent for it: a model thinking hard."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setattr(settings, "api_reply_deadline", 0.3)
    chunks = [{"choices": [{"delta": {"reasoning_content": "."}}]} for _ in range(8)]
    body = sse(*chunks, {"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}, "[DONE]")
    registry = registry_with([streamed(body, delay=0.1)], [])
    try:
        result = await registry.complete("deepseek:deepseek-flash", "plan")
    finally:
        await registry.client.aclose()
    assert result.text == "done"


async def test_a_reply_that_goes_silent_is_stopped_and_its_model_steps_aside(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setattr(settings, "api_reply_deadline", 0.2)
    body = sse({"choices": [{"delta": {"content": "start"}}]}, {"choices": [{"delta": {"content": " more"}}]})
    sent = []
    registry = registry_with([streamed(body, stall_after=1)], sent)
    try:
        with pytest.raises(ModelUnavailable, match="中途停了"):
            await asyncio.wait_for(registry.complete("deepseek:deepseek-flash", "work", tmp_path, act=True), 5)
    finally:
        await registry.client.aclose()
    assert len(sent) == 1


async def test_an_anthropic_stream_is_put_back_together_and_its_errors_count(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    write = {"file_path": "a.txt", "content": "hi"}
    first = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "plan"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t1", "name": "Write", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": json.dumps(write)[:9]}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": json.dumps(write)[9:]}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 30}},
        {"type": "message_stop"},
    )
    second = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 5, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "done"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
    )
    overloaded = sse({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
    sent = []
    registry = registry_with([streamed(first), streamed(second), streamed(overloaded)], sent)
    try:
        result = await registry.complete("anthropic:claude-x", "work", tmp_path, act=True)
        with pytest.raises(ModelUnavailable):
            await registry.complete("anthropic:claude-x", "plan")
    finally:
        await registry.client.aclose()
    assert (tmp_path / "a.txt").read_text() == "hi"
    assert (result.text, result.input_tokens, result.output_tokens) == ("done", 105, 32)
    assert json.loads(sent[0].content)["stream"] is True
    thinking = json.loads(sent[1].content)["messages"][1]["content"][0]
    assert thinking == {"type": "thinking", "thinking": "plan", "signature": "sig"}


async def test_a_gemini_stream_is_put_back_together(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    first = sse({"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"name": "LS", "args": {}}, "thoughtSignature": "s"}]}}]},
                {"candidates": [{"finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1}})
    second = sse({"candidates": [{"content": {"parts": [{"text": "Found "}]}}]},
                 {"candidates": [{"content": {"parts": [{"text": "it"}]}, "finishReason": "STOP"}],
                  "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2}})
    sent = []
    registry = registry_with([streamed(first), streamed(second)], sent)
    try:
        result = await registry.complete("google:gemini-x", "List files", tmp_path, act=True)
    finally:
        await registry.client.aclose()
    assert (result.text, result.input_tokens, result.output_tokens) == ("Found it", 7, 3)
    assert sent[0].url.path.endswith(":streamGenerateContent") and sent[0].url.params["alt"] == "sse"
    assert json.loads(sent[1].content)["contents"][1]["parts"][0]["thoughtSignature"] == "s"

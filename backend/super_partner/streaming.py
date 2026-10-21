"""Model replies as streams of server-sent events, put back together into the reply the provider
would have sent without streaming, so the code reading replies stays the same.

Streaming lets a long reply be told apart from a dead one: a model that thinks for many minutes
keeps sending data, a provider that hangs sends nothing (``providers.ProviderRegistry._post``).
"""
from __future__ import annotations

import json
from typing import Callable


class StreamError(RuntimeError):
    """The provider reported an error inside the stream (for example Anthropic's overloaded_error)."""


def events(lines: list[str]) -> list[dict]:
    """The JSON payloads of a stream's ``data:`` lines; OpenAI's closing ``[DONE]`` is not one."""
    found = []
    for line in lines:
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            found.append(payload)
    return found


def openai(chunks: list[dict]) -> dict:
    """Chat completion chunks (OpenAI and the providers compatible with it) as one completion."""
    content, reasoning, calls, order = [], [], {}, []
    finish, usage, last = None, {}, None
    for chunk in chunks:
        if chunk.get("error"):
            raise StreamError(json.dumps(chunk["error"], ensure_ascii=False)[:300])
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or choice.get("message") or {}
            if isinstance(delta.get("content"), str):
                content.append(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                reasoning.append(delta["reasoning_content"])
            for call in delta.get("tool_calls") or []:
                # Each call is sent in pieces under its index; some providers send it whole, without one.
                key = call.get("index")
                if key is None:
                    key = call.get("id") or last
                if key not in calls:
                    calls[key] = {"id": None, "type": "function", "function": {"name": "", "arguments": ""}}
                    order.append(key)
                last, slot, function = key, calls[key], call.get("function") or {}
                slot["id"] = call.get("id") or slot["id"]
                slot["function"]["name"] = slot["function"]["name"] or function.get("name") or ""
                arguments = function.get("arguments")
                if isinstance(arguments, dict):
                    slot["function"]["arguments"] = json.dumps(arguments, ensure_ascii=False)
                elif arguments:
                    slot["function"]["arguments"] += arguments
            finish = choice.get("finish_reason") or finish
    message = {"role": "assistant", "content": "".join(content) if content else None}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if calls:
        message["tool_calls"] = [{**calls[key], "id": calls[key]["id"] or f"call_{index}"}
                                 for index, key in enumerate(order)]
    return {"choices": [{"message": message, "finish_reason": finish}], "usage": usage}


def anthropic(items: list[dict]) -> dict:
    """Anthropic's message stream as one message: its content blocks, stop reason and usage."""
    blocks, partial, usage, stop = {}, {}, {}, None
    for item in items:
        kind = item.get("type")
        if kind == "error":
            error = item.get("error") or {}
            raise StreamError(f"{error.get('type', 'error')}: {error.get('message', '')}"[:300])
        if kind == "message_start":
            usage.update((item.get("message") or {}).get("usage") or {})
        elif kind == "content_block_start":
            blocks[item["index"]] = dict(item.get("content_block") or {})
        elif kind == "content_block_delta":
            block, delta = blocks.setdefault(item["index"], {}), item.get("delta") or {}
            if delta.get("type") == "input_json_delta":
                partial[item["index"]] = partial.get(item["index"], "") + (delta.get("partial_json") or "")
            for field in ("text", "thinking", "signature"):
                if field in delta:
                    block[field] = block.get(field, "") + delta[field]
        elif kind == "message_delta":
            stop = (item.get("delta") or {}).get("stop_reason") or stop
            usage.update({key: value for key, value in (item.get("usage") or {}).items() if value is not None})
    for index, text in partial.items():
        try:
            blocks[index]["input"] = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            blocks[index]["input"] = {}
    return {"content": [blocks[index] for index in sorted(blocks)], "stop_reason": stop, "usage": usage}


def gemini(chunks: list[dict]) -> dict:
    """Gemini's streamed responses as one: their parts in order (neighbouring plain text joined),
    the last finish reason and usage."""
    parts, finish, usage = [], None, {}
    for chunk in chunks:
        if chunk.get("error"):
            raise StreamError(json.dumps(chunk["error"], ensure_ascii=False)[:300])
        usage = chunk.get("usageMetadata") or usage
        for candidate in (chunk.get("candidates") or [])[:1]:
            finish = candidate.get("finishReason") or finish
            for part in (candidate.get("content") or {}).get("parts") or []:
                plain = set(part) == {"text"}
                if plain and parts and set(parts[-1]) == {"text"}:
                    parts[-1] = {"text": parts[-1]["text"] + part["text"]}
                else:
                    parts.append(dict(part))
    candidate = {"content": {"role": "model", "parts": parts}}
    if finish:
        candidate["finishReason"] = finish
    return {"candidates": [candidate], "usageMetadata": usage}


Assemble = Callable[[list[dict]], dict]

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from . import agent as agent_tools
from . import attachments as files
from .budget import BudgetExceeded, ProviderError
from .cli_channels import CLI_SPECS, CliChannelRegistry
from .config import settings
from .db import Database
from .models import Attachment
from .quality import REVIEW_MARKER
from .quota import ModelUnavailable, QuotaTracker
from .secrets import PROVIDER_ENV


@dataclass
class ProviderResult:
    text: str
    input_tokens: int
    output_tokens: int
    used_model_id: str | None = None
    accounted_cost_usd: float = 0.0
    # Turns of tool calls an acting API model took, and whether it hit its limit, so this text
    # is the progress summary it was asked for at the end rather than a finished answer.
    turns: int = 0
    turn_limit: bool = False


RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 529}
# The floor a cap the model rejects is retried down to.
PLAIN_OUTPUT_TOKENS = 4096
OUTPUT_LIMIT_REJECTED = re.compile(r"max_(?:completion_)?tokens|max_?output|output tokens", re.IGNORECASE)


# Turns before the limit at which the model is told to wrap up.
WRAP_UP_TURNS = 5
MAX_LOGGED_CALLS = 300
TURN_LIMIT_MARK = "在 {limit} 轮工具调用后仍未完成"


def _turn_note(left: int) -> str:
    """Appended to the last tool result as the turns run out."""
    if left == WRAP_UP_TURNS:
        return (f"[System notice] Only {left} turns of tool calls are left. Do not start new work: finish "
                "the change in hand, run one final check, then answer without calling a tool.")
    if left == 0:
        return ("[System notice] No turns of tool calls are left. Do not call any tool. Answer now with a "
                "progress summary (not UNABLE: unfinished work is not a limit of the environment): what is done "
                "(files, checks and their results), what is not done yet, "
                "and exactly what the next attempt should do.")
    return ""


class ProviderRegistry:
    def __init__(self, db: Optional[Database] = None, models: Optional[dict] = None) -> None:
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(settings.api_request_timeout, connect=30))
        self.cli = CliChannelRegistry()
        self.quota = QuotaTracker(db)
        # The live catalog (updated in place by catalog sync), for each model's output ceiling.
        self.models = models if models is not None else {}
        self.max_attempts = 4
        self.retry_base_delay = 1.0
        self.retry_max_delay = 30.0

    def configured_models(self, model_ids: list[str]) -> set[str]:
        """Models that can take work now: set up, and not out of quota."""
        configured = {"mock:local", *self.cli.available_model_ids()}
        for model_id in model_ids:
            provider = model_id.split(":", 1)[0]
            env_name = PROVIDER_ENV.get(provider)
            if env_name and os.getenv(env_name):
                configured.add(model_id)
        return {model_id for model_id in configured if not self.quota.exhausted(model_id)}

    async def complete(self, model_id: str, prompt: str, *args, **kwargs) -> ProviderResult:
        """A limit or exhausted upstream retry raises ModelUnavailable and excludes this
        model until its cooldown ends, so callers can switch to another."""
        if self.quota.exhausted(model_id):
            raise self.quota.error(model_id)
        try:
            return await self._complete(model_id, prompt, *args, **kwargs)
        except ModelUnavailable:
            raise
        except RuntimeError as exc:
            kind = self.quota.classify(model_id, str(exc))
            if kind:
                unavailable = self.quota.mark(model_id, str(exc), kind)
                unavailable.usage = getattr(exc, "usage", None)
                raise unavailable from exc
            raise

    async def _complete(
        self, model_id: str, prompt: str, workspace=None, cli_timeout: int = 900,
        allow_api_fallback: bool = True, attachments: tuple[Attachment, ...] | list[Attachment] = (),
        act: bool = False, spend_ok: Optional[Callable[[str, int, int], bool]] = None,
        repair: bool = False, max_turns: Optional[int] = None, tool_log: Optional[list] = None,
        network: bool = False, review_workspace: Optional[Path] = None, review_files: list[str] = (),
    ) -> ProviderResult:
        """One model call. With ``act``, an API model works on ``workspace`` through tools
        (see agent.py) until it is done; ``spend_ok(model_id, input, output)`` can stop it.
        ``repair`` lets the model's commands leave the sandbox to fix the environment;
        ``network`` lets sandboxed commands reach the internet for a task that allows it.
        ``review_workspace`` gives API reviewers only safe file reads, independently of act/repair."""
        provider, model = model_id.split(":", 1)
        attachments = list(attachments)
        if provider == "cli":
            try:
                text = await self.cli.complete(
                    model_id, prompt + files.describe(attachments, with_paths=True),
                    workspace or settings.workspace_root, timeout=cli_timeout,
                    attachments=attachments, repair=repair, **({"network": True} if network else {}),
                )
                return ProviderResult(
                    text=text, input_tokens=0, output_tokens=0, used_model_id=model_id,
                )
            except RuntimeError as exc:
                kind = self.quota.classify(model_id, str(exc))
                if kind:
                    # Availability failures go through the scheduler's ordinary model/budget
                    # constraints, including CLI-to-CLI handoff and project tool requirements.
                    raise self.quota.mark(model_id, str(exc), kind) from exc
                if not allow_api_fallback:
                    raise ProviderError(str(exc)) from exc
                fallback = CLI_SPECS[model_id].fallback_model_id
                if fallback not in self.configured_models([fallback]):
                    raise ProviderError(f"{exc}; no API fallback is configured") from exc
                result = await self.complete(
                    fallback, prompt, workspace, cli_timeout, attachments=attachments,
                    act=act, spend_ok=spend_ok, network=network,
                    review_workspace=review_workspace, review_files=review_files,
                )
                result.used_model_id = fallback
                return result
        images = [item for item in attachments if item.kind == "image"]
        prompt = prompt + files.describe(attachments, with_paths=False) + files.inline_text(attachments)
        if provider == "mock":
            # The demo model accepts work it is asked to review, so offline demos complete.
            text = (
                '{"passed": true, "reason": "Demo review: accepted."}' if REVIEW_MARKER in prompt
                else "Demo verification.\nVERDICT: PASS" if "You are verifying:" in prompt
                else f"Completed by {model_id}: {prompt[:800]}"
            )
            return ProviderResult(text=text, input_tokens=max(1, len(prompt) // 4), output_tokens=max(1, len(text) // 4), used_model_id=model_id)
        if review_workspace is not None:
            agent = agent_tools.Workspace(
                Path(review_workspace), read_only=True, readable_files=review_files,
                max_turns=max_turns or 8, log=tool_log,
            )
        else:
            agent = agent_tools.Workspace(
                Path(workspace or settings.workspace_root), host=repair, max_turns=max_turns, log=tool_log,
                network=network,
            ) if act else None
        if provider == "anthropic":
            return await self._anthropic(model, prompt, images, agent, spend_ok)
        if provider == "google":
            return await self._gemini(model, prompt, images, agent, spend_ok)
        return await self._openai_compatible(provider, model, prompt, images, agent, spend_ok)

    async def test_provider(self, provider: str) -> str:
        urls = {
            "openai": "https://api.openai.com/v1/models",
            "anthropic": "https://api.anthropic.com/v1/models",
            "deepseek": "https://api.deepseek.com/models",
            "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1/models",
            "doubao": "https://ark.cn-beijing.volces.com/api/v3/models",
            "glm": "https://open.bigmodel.cn/api/paas/v4/models",
            "minimax": "https://api.minimax.io/v1/models",
            "kimi": "https://api.moonshot.cn/v1/models",
        }
        if provider != "google" and provider not in urls:
            raise ProviderError(f"Unsupported provider: {provider}")
        key = os.getenv(PROVIDER_ENV[provider])
        if not key:
            raise ProviderError(f"No API key is configured for {provider}")
        if provider == "google":
            response = await self.client.get(
                "https://generativelanguage.googleapis.com/v1beta/models", params={"key": key},
            )
        elif provider == "anthropic":
            response = await self.client.get(
                urls[provider], headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            )
        else:
            response = await self.client.get(urls[provider], headers={"Authorization": f"Bearer {key}"})
        if response.is_error:
            raise ProviderError(
                f"Connection test failed ({response.status_code}): {response.text[:200]}"
            )
        return "Connection verified"

    async def _post(self, provider: str, url: str, **kwargs) -> httpx.Response:
        """POST with exponential backoff on rate limits, server errors, and network failures."""
        for attempt in range(1, self.max_attempts + 1):
            delay = self.retry_base_delay * 2 ** (attempt - 1)
            try:
                response = await self.client.post(url, **kwargs)
            except httpx.TransportError as exc:
                if attempt == self.max_attempts:
                    raise ProviderError(f"{provider} request failed: {exc}") from exc
            else:
                if response.status_code not in RETRYABLE_STATUS or attempt == self.max_attempts:
                    return response
                try:
                    delay = float(response.headers.get("retry-after", delay))
                except ValueError:
                    pass
            await asyncio.sleep(min(delay, self.retry_max_delay))
        raise AssertionError("unreachable")

    async def _loop(self, provider: str, model: str, prompt: str, images, agent, spend_ok, step):
        """Call the model, run the tools it asks for, and call again until it stops asking.

        ``step(state)`` makes one request and returns (text, tool_calls, input, output,
        truncated); ``tool_calls`` are (id, name, arguments) and their results go back via
        ``state``. ``truncated`` means the output hit its token cap. Without a workspace this is
        a single plain call.
        """
        model_id = f"{provider}:{model}"
        tools = agent_tools.tools_for(agent_tools.sandbox_available(), read_only=agent.read_only) if agent else []
        if agent:
            prompt = f"{agent.instructions()}\n\n{prompt}"
        state = {"prompt": prompt, "images": images, "tools": tools, "history": None, "results": None}
        total_in = total_out = turn = 0
        limit = (agent.max_turns or settings.api_agent_max_turns) if agent else 1
        try:
            # One turn past the limit: the model's chance to answer with a progress summary.
            for turn in range(1, limit + 2 if agent else 2):
                final = bool(agent) and turn > limit
                if spend_ok and not spend_ok(model_id, total_in, total_out):
                    raise BudgetExceeded("已达到任务可用的预算，模型调用被中止。")
                text, calls, used_in, used_out, truncated = await step(state)
                total_in, total_out = total_in + used_in, total_out + used_out
                if record := getattr(spend_ok, "record", None):
                    await record(model_id, used_in, used_out)
                if spend_ok and not spend_ok(model_id, total_in, total_out):
                    raise BudgetExceeded("已达到任务可用的预算，模型调用被中止。")
                if truncated and not calls and (agent or not text):
                    # A cut-off turn is not an answer; returning it would look like a model that
                    # finished without doing anything.
                    raise ProviderError(
                        f"{model_id} 的输出达到单轮上限 {state.get('limit')} 个 token 被截断"
                        "（output truncated by length），没有给出完整回答或工具调用。"
                    )
                if not calls or not agent:
                    return ProviderResult(text or "", total_in, total_out, model_id, turns=min(turn, limit), turn_limit=final)
                if final:
                    raise ProviderError(f"{model_id} {TURN_LIMIT_MARK.format(limit=limit)}，最后也没有交出进度总结。")
                results = []
                for call_id, name, args in calls:
                    output = await agent.call(name, args)
                    results.append((call_id, name, output))
                    if agent.log is not None and len(agent.log) < MAX_LOGGED_CALLS:
                        agent.log.append({"turn": turn, "tool": name, "detail": agent_tools.describe_call(name, args),
                                          "ok": not output.startswith("Error:") and (name != "run_command" or output.startswith("exit code 0"))})
                if note := _turn_note(limit - turn):
                    call_id, name, output = results[-1]
                    results[-1] = (call_id, name, f"{output}\n\n{note}")
                state["results"] = results
            raise AssertionError("unreachable")
        except (Exception, asyncio.CancelledError) as exc:
            exc.usage = ProviderResult("", total_in, total_out, model_id, turns=min(turn, limit))
            raise

    def output_ceiling(self, model_id: str) -> int:
        model = self.models.get(model_id)
        return getattr(model, "max_output_tokens", None) or settings.api_agent_max_output_tokens

    @staticmethod
    async def _limit_request(spend_ok, model_id: str, body: dict, maximum: int = PLAIN_OUTPUT_TOKENS) -> int:
        reserve = getattr(spend_ok, "reserve", None)
        return await reserve(model_id, body, maximum) if reserve else maximum

    async def _send(self, provider: str, model_id: str, url: str, body: dict, put, state, spend_ok, **kwargs):
        """POST ``body`` with an output cap set by ``put(body, limit)``.

        The cap is the model's own output ceiling from the catalog, or
        ``api_agent_max_output_tokens`` when that is unknown; a model that rejects it is
        retried with half, down to the plain cap, and later turns keep the cap that worked.
        The budget reservation can only lower it.
        """
        maximum = state.get("maximum") or self.output_ceiling(model_id)
        while True:
            state["limit"] = await self._limit_request(spend_ok, model_id, body, maximum)
            put(body, state["limit"])
            response = await self._post(provider, url, json=body, **kwargs)
            if (
                response.status_code == 400 and state["limit"] > PLAIN_OUTPUT_TOKENS
                and OUTPUT_LIMIT_REJECTED.search(response.text)
            ):
                maximum = state["maximum"] = max(PLAIN_OUTPUT_TOKENS, state["limit"] // 2)
                continue
            return response

    async def _openai_compatible(
        self, provider: str, model: str, prompt: str, images: list[Attachment] = (), agent=None, spend_ok=None,
    ) -> ProviderResult:
        urls = {
            "openai": "https://api.openai.com/v1/chat/completions",
            "deepseek": "https://api.deepseek.com/chat/completions",
            "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "doubao": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
            "glm": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            "minimax": "https://api.minimax.io/v1/chat/completions",
            "kimi": "https://api.moonshot.cn/v1/chat/completions",
        }
        url, env = urls[provider], PROVIDER_ENV[provider]

        async def step(state):
            if state["history"] is None:
                state["history"] = [{"role": "user", "content": [
                    {"type": "text", "text": state["prompt"]},
                    *({"type": "image_url", "image_url": {
                        "url": f"data:{item.media_type};base64,{files.image_data(item)}",
                    }} for item in state["images"]),
                ] if state["images"] else state["prompt"]}]
            for call_id, _name, output in state["results"] or []:
                state["history"].append({"role": "tool", "tool_call_id": call_id, "content": output})
            body = {"model": model, "messages": state["history"]}
            if state["tools"]:
                body["tools"] = [{"type": "function", "function": tool} for tool in state["tools"]]
            field = "max_completion_tokens" if provider == "openai" else "max_tokens"
            response = await self._send(
                provider, f"{provider}:{model}", url, body, lambda body, limit: body.update({field: limit}),
                state, spend_ok, headers={"Authorization": f"Bearer {os.environ[env]}"},
            )
            if response.is_error:
                raise ProviderError(f"{provider} returned {response.status_code}: {response.text[:300]}")
            data = response.json()
            usage = data.get("usage") or {}
            choice = data["choices"][0]
            message = choice["message"]
            calls = message.get("tool_calls") or []
            if calls:
                reply = {"role": "assistant", "content": message.get("content"), "tool_calls": calls}
                if message.get("reasoning_content"):
                    # DeepSeek's (and Kimi's) thinking mode needs it back within a tool loop.
                    reply["reasoning_content"] = message["reasoning_content"]
                state["history"].append(reply)
            return (
                message.get("content"),
                [(call["id"], call["function"]["name"], _arguments(call["function"].get("arguments"))) for call in calls],
                usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                choice.get("finish_reason") == "length",
            )

        return await self._loop(provider, model, prompt, images, agent, spend_ok, step)

    async def _anthropic(self, model: str, prompt: str, images: list[Attachment] = (), agent=None, spend_ok=None) -> ProviderResult:
        async def step(state):
            if state["history"] is None:
                state["history"] = [{"role": "user", "content": [
                    *({"type": "image", "source": {
                        "type": "base64", "media_type": item.media_type, "data": files.image_data(item),
                    }} for item in state["images"]),
                    {"type": "text", "text": state["prompt"]},
                ] if state["images"] else state["prompt"]}]
            if state["results"]:
                state["history"].append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": call_id, "content": output}
                    for call_id, _name, output in state["results"]
                ]})
            body = {"model": model, "messages": state["history"]}
            if state["tools"]:
                body["tools"] = [
                    {"name": tool["name"], "description": tool["description"], "input_schema": tool["parameters"]}
                    for tool in state["tools"]
                ]
            response = await self._send(
                "anthropic", f"anthropic:{model}", "https://api.anthropic.com/v1/messages", body,
                lambda body, limit: body.update(max_tokens=limit), state, spend_ok,
                headers={"x-api-key": os.environ[PROVIDER_ENV["anthropic"]], "anthropic-version": "2023-06-01"},
            )
            if response.is_error:
                raise ProviderError(f"anthropic returned {response.status_code}: {response.text[:300]}")
            data = response.json()
            blocks = data["content"]
            calls = [block for block in blocks if block.get("type") == "tool_use"]
            if calls:
                state["history"].append({"role": "assistant", "content": blocks})
            text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
            return (
                text, [(block["id"], block["name"], block.get("input") or {}) for block in calls],
                data["usage"]["input_tokens"], data["usage"]["output_tokens"],
                data.get("stop_reason") == "max_tokens",
            )

        return await self._loop("anthropic", model, prompt, images, agent, spend_ok, step)

    async def _gemini(self, model: str, prompt: str, images: list[Attachment] = (), agent=None, spend_ok=None) -> ProviderResult:
        async def step(state):
            if state["history"] is None:
                state["history"] = [{"role": "user", "parts": [
                    *({"inline_data": {"mime_type": item.media_type, "data": files.image_data(item)}} for item in state["images"]),
                    {"text": state["prompt"]},
                ]}]
            if state["results"]:
                state["history"].append({"role": "user", "parts": [
                    {"functionResponse": {"name": name, "response": {"result": output}}}
                    for _call_id, name, output in state["results"]
                ]})
            body = {"contents": state["history"]}
            if state["tools"]:
                body["tools"] = [{"functionDeclarations": state["tools"]}]
            response = await self._send(
                "google", f"google:{model}",
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", body,
                lambda body, limit: body.update(generationConfig={"maxOutputTokens": limit}), state, spend_ok,
                params={"key": os.environ[PROVIDER_ENV["google"]]},
            )
            if response.is_error:
                raise ProviderError(f"google returned {response.status_code}: {response.text[:300]}")
            data = response.json()
            usage = data.get("usageMetadata", {})
            candidate = data["candidates"][0]
            content = candidate.get("content") or {}
            parts = content.get("parts") or []
            calls = [part["functionCall"] for part in parts if "functionCall" in part]
            if calls:
                state["history"].append({"role": "model", "parts": parts})
            text = "".join(part.get("text", "") for part in parts)
            return (
                text, [(call["name"], call["name"], call.get("args") or {}) for call in calls],
                usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0),
                candidate.get("finishReason") == "MAX_TOKENS",
            )

        return await self._loop("google", model, prompt, images, agent, spend_ok, step)


def _arguments(raw) -> dict:
    """Tool arguments arrive as a JSON string; malformed ones become an error the tool reports."""
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"__invalid__": raw}
    return value if isinstance(value, dict) else {"__invalid__": raw}

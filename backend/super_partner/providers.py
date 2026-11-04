from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from . import agent as agent_tools
from . import attachments as files
from . import calls as call_records
from . import conversation as memory
from . import progress, streaming
from .budget import BudgetExceeded, ProviderError, StepLimitExceeded
from .call_control import ProviderGate, input_bound, retry_after
from .cli_channels import CLI_SPECS, CliChannelRegistry, InputTooLarge, SessionLost, input_too_large
from .config import settings
from .contracts import ArtifactBlocked
from .conversation import Conversation
from .db import Database
from .models import Attachment, SupervisorSession
from .quality import REVIEW_MARKER
from .quota import ModelUnavailable, QuotaTracker
from .secrets import PROVIDER_ENV

TOOL_LIMIT_REQUIREMENTS = {"counting_unit": "tool_call", "hard_limit": True}


@dataclass
class ProviderResult:
    text: str
    input_tokens: int
    output_tokens: int
    used_model_id: str | None = None
    accounted_cost_usd: float = 0.0
    # Input tokens the provider read from its prompt cache (billed at the cache price).
    cached_input_tokens: int = 0
    # Turns of tool calls an acting API model took, and whether it hit its limit, so this text
    # is the progress summary it was asked for at the end rather than a finished answer.
    turns: int = 0
    turn_limit: bool = False
    usage_known: bool = True
    cache_write_tokens: int = 0
    warnings: list[str] = field(default_factory=list)


def cached_tokens(usage: dict) -> int:
    """Input tokens read from the prompt cache, as an OpenAI-compatible provider reports them:
    DeepSeek's prompt_cache_hit_tokens, or prompt_tokens_details.cached_tokens (OpenAI, GLM,
    MiniMax, Kimi and others)."""
    if usage.get("prompt_cache_hit_tokens") is not None:
        return int(usage["prompt_cache_hit_tokens"] or 0)
    details = usage.get("prompt_tokens_details") or {}
    return int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0


# Thinking some models (MiniMax) write into the content rather than a field of its own.
THINKING = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def _answer(content):
    """A reply's content without written-out thinking: kept in the history, not in the answer."""
    if not isinstance(content, str) or "<think>" not in content:
        return content
    return THINKING.sub("", content).strip()


# A crash between a tool's side effects and checkpoint has an uncertain outcome.
NOT_RUN = memory.UNKNOWN


def _reopen(state: dict, followup: str) -> None:
    """Make an OpenAI-format conversation that ended (finished, interrupted, or cut off before
    its tool calls ran) ready to take ``followup`` as the next user message."""
    memory.reopen(state, followup)


RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 529}
# The floor a cap the model rejects is retried down to.
PLAIN_OUTPUT_TOKENS = 4096
OUTPUT_LIMIT_REJECTED = re.compile(r"max_(?:completion_)?tokens|max_?output|output tokens", re.IGNORECASE)


# Turns for a tool loop that has no limit set and nobody checking on it (a diagnosis, say).
UNWATCHED_TURNS = 40
# Turns before the limit at which the model is told to wrap up.
WRAP_UP_TURNS = 5
# The start of each tool's output kept in the attempt's log, to see what the model saw.
LOGGED_OUTPUT = 500
MAX_LOGGED_CALLS = 300
# How much of each tool output the full trace keeps (agent.Workspace.trace).
TRACE_OUTPUT = 200_000
TURN_LIMIT_MARK = "在 {limit} 轮工具调用后仍未完成"


def _transcript(history: list[dict], prompt: str) -> str:
    """A conversation as one prompt; earlier turns first, so the prefix stays the same (and cached)."""
    if not history:
        return prompt
    turns = "\n\n".join(f"[{item['role'].upper()}]\n{item['content']}" for item in history)
    return f"The conversation so far:\n\n{turns}\n\n[USER]\n{prompt}"


# A tool loop sends its whole history every turn. Past COMPACT_AT characters, older tool output
# and written file contents are cut to their start, all at once, so the history stays the same
# (and cached) for the turns after; the last KEEP_RESULTS tool results stay whole.
COMPACT_AT = 200_000
KEEP_RESULTS = 6
KEPT_CHARS = 400
ELIDED = "\n[... {n} more characters left out to keep the conversation short; read or run it again if needed]"


def _cut(text, keep: int = KEPT_CHARS):
    if not isinstance(text, str) or len(text) <= keep + len(ELIDED) + 20 or ELIDED[:20] in text:
        return text
    return text[:keep] + ELIDED.format(n=len(text) - keep)


def _written(args) -> None:
    """A Write call's content, in place: the file holds it now."""
    if isinstance(args, dict) and isinstance(args.get("content"), str) and len(args["content"]) > KEPT_CHARS * 2:
        args["content"] = _cut(args["content"])


def compact(history: list | None) -> bool:
    """Shorten an over-long tool-loop history in place (OpenAI-compatible, Anthropic or Gemini
    messages). True if it changed."""
    if not history or len(json.dumps(history, ensure_ascii=False)) < COMPACT_AT:
        return False
    results = []  # (container, key) of every tool result, oldest first
    for message in history:
        if message.get("role") == "tool":
            results.append((message, "content"))
        for block in message.get("content") if isinstance(message.get("content"), list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                results.append((block, "content"))
            if isinstance(block, dict) and block.get("type") == "tool_use":
                _written(block.get("input"))
        for part in message.get("parts") or []:
            if isinstance(part, dict) and isinstance((part.get("functionResponse") or {}).get("response"), dict):
                results.append((part["functionResponse"]["response"], "result"))
            if isinstance(part, dict) and "functionCall" in part:
                _written(part["functionCall"].get("args"))
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            try:
                args = json.loads(function.get("arguments") or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(args, dict) and isinstance(args.get("content"), str) and len(args["content"]) > KEPT_CHARS * 2:
                _written(args)
                function["arguments"] = json.dumps(args, ensure_ascii=False)
    for container, key in results[:-KEEP_RESULTS] if len(results) > KEEP_RESULTS else []:
        container[key] = _cut(container[key])
    return True


def _turns(turn: int, limit: Optional[int]) -> int:
    return turn if limit is None else min(turn, limit)


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
        self.db = db
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(settings.api_request_timeout, connect=30))
        self.cli = CliChannelRegistry()
        self.quota = QuotaTracker(db)
        # The live catalog (updated in place by catalog sync), for each model's output ceiling.
        self.models = models if models is not None else {}
        self.max_attempts = 4
        # Models that rejected a reasoning effort: they are sent none (see _send).
        self.no_effort: set[str] = set()
        self.retry_base_delay = 1.0
        self.retry_max_delay = 30.0
        self.gates: dict[str, ProviderGate] = {}

    def configured_models(self, model_ids: list[str]) -> set[str]:
        """Models that can take work now: set up, and not out of quota."""
        configured = {"mock:local", *self.cli.available_model_ids()}
        for model_id in model_ids:
            provider = model_id.split(":", 1)[0]
            env_name = PROVIDER_ENV.get(provider)
            if env_name and os.getenv(env_name):
                configured.add(model_id)
        return {model_id for model_id in configured if not self.quota.exhausted(model_id)}

    def tool_limit_capabilities(self, model_id: str) -> dict:
        """Only the platform-owned API tool loop can count and intercept every invocation."""
        if model_id.startswith("cli:"):
            return self.cli.tool_limit_capabilities(model_id)
        model = self.models.get(model_id)
        if (model_id.split(":", 1)[0] not in PROVIDER_ENV or not settings.api_agent
                or (model and (model.channel == "cli" or model.supports_tools is False))):
            return {"counting_unit": None, "hard_limit": False}
        return dict(TOOL_LIMIT_REQUIREMENTS)

    @call_records.observed
    async def complete(self, model_id: str, prompt: str, *args, **kwargs) -> ProviderResult:
        """A limit or exhausted upstream retry raises ModelUnavailable and excludes this
        model until its cooldown ends, so callers can switch to another."""
        if self.quota.exhausted(model_id):
            raise self.quota.error(model_id)
        try:
            return await self._complete(model_id, prompt, *args, **kwargs)
        except (ModelUnavailable, InputTooLarge):
            raise
        except RuntimeError as exc:
            kind = self.quota.classify(model_id, str(exc))
            if kind:
                unavailable = self.quota.mark(model_id, str(exc), kind)
                unavailable.usage = getattr(exc, "usage", None)
                raise unavailable from exc
            raise

    @call_records.observed
    async def converse(self, session: SupervisorSession, prompt: str, timeout: Optional[float] = None,
                       spend_ok=None) -> ProviderResult:
        """One message in a conversation that lasts (the task's lead, supervisor.py): a membership
        CLI continues its own conversation; an API model is sent the whole history again, which
        its prompt cache makes cheap. ``session`` is updated in place."""
        model_id = session.model_id
        effort = session.reasoning_effort or settings.lead_reasoning_effort
        if record := call_records.row():
            record["requested_effort"] = effort
        if self.quota.exhausted(model_id):
            raise self.quota.error(model_id)
        if model_id.startswith("cli:"):
            Path(session.cwd).mkdir(parents=True, exist_ok=True)
            try:
                text, session.session_id = await self.cli.converse(
                    model_id, prompt, Path(session.cwd), session.session_id, timeout,
                    **({"effort": session.reasoning_effort} if session.reasoning_effort else {}))
            except (SessionLost, InputTooLarge):
                raise
            except RuntimeError as exc:
                if input_too_large(exc):
                    raise InputTooLarge(str(exc)) from exc
                kind = self.quota.classify(model_id, str(exc))
                if kind:
                    raise self.quota.mark(model_id, str(exc), kind) from exc
                raise ProviderError(str(exc)) from exc
            result = _cli_result(text, model_id)
        else:
            result = await self.complete(model_id, _transcript(session.history, prompt), spend_ok=spend_ok,
                                         effort=effort)
            session.history += [{"role": "user", "content": prompt}, {"role": "assistant", "content": result.text}]
        session.calls += 1
        return result

    async def _complete(
        self, model_id: str, prompt: str, workspace=None, cli_timeout: int = 900,
        allow_api_fallback: bool = True, attachments: tuple[Attachment, ...] | list[Attachment] = (),
        act: bool = False, spend_ok: Optional[Callable[[str, int, int], bool]] = None,
        repair: bool = False, max_turns: Optional[int] = None, tool_log: Optional[list] = None,
        network: bool = False, review_workspace: Optional[Path] = None, review_files: list[str] = (),
        watch: Optional[progress.Watch] = None, conversation: Optional[Conversation] = None,
        effort: Optional[str] = None, overrun: Optional[tuple[int, progress.Watch]] = None,
        tool_trace: Optional[Path] = None,
    ) -> ProviderResult:
        """One model call. With ``act``, an API model works on ``workspace`` through tools
        (see agent.py) until it is done; ``spend_ok(model_id, input, output)`` can stop it.
        ``repair`` lets the model's commands leave the sandbox to fix the environment;
        ``network`` lets sandboxed commands reach the internet for a task that allows it.
        ``review_workspace`` gives API reviewers only safe file reads, independently of act/repair.
        ``watch`` checks a long call from time to time instead of a turn limit or a timeout.
        ``conversation`` keeps an acting model's conversation so a later attempt can continue it.
        ``effort`` replaces the configured reasoning effort, for simple work such as summaries.
        ``overrun`` is (expected turns, check): an acting model past progress.OVERRUN_FACTOR times
        those turns is checked, and stopped if the check says so."""
        provider, model = model_id.split(":", 1)
        if (getattr(spend_ok, "turn_remaining", None) is not None
                and (not act or not all(self.tool_limit_capabilities(model_id).get(k) == v
                                       for k, v in TOOL_LIMIT_REQUIREMENTS.items())
                     or not callable(getattr(spend_ok, "begin_tool", None)))):
            raise ArtifactBlocked("此通道不能可靠执行声明的工具调用上限。")
        attachments = list(attachments)
        if provider == "cli":
            if conversation and conversation.handoff:
                prompt += conversation.handoff
            try:
                text = await self.cli.complete(
                    model_id, prompt + files.describe(attachments, with_paths=True),
                    workspace or settings.workspace_root, timeout=cli_timeout,
                    attachments=attachments, repair=repair, **({"network": True} if network else {}),
                    **({"watch": watch} if watch else {}), **({"effort": effort} if effort else {}),
                    **({"tool_trace": tool_trace} if tool_trace is not None else {}),
                )
                return _cli_result(text, model_id)
            except progress.Stopped as exc:
                # Stuck, not unavailable: the attempt fails rather than falling back to an API model.
                raise ProviderError(str(exc)) from exc
            except InputTooLarge:
                raise
            except RuntimeError as exc:
                if input_too_large(exc):
                    raise InputTooLarge(str(exc)) from exc
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
                    review_workspace=review_workspace, review_files=review_files, watch=watch,
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
                # More turns when it can rerun a check than when it only reads.
                max_turns=max_turns or (12 if agent_tools.sandbox_available() else 8), log=tool_log,
            )
        else:
            agent = agent_tools.Workspace(
                Path(workspace or settings.workspace_root), host=repair, max_turns=max_turns, log=tool_log,
                network=network, trace=tool_trace,
            ) if act else None
        if agent:
            agent.watch, agent.overrun = watch, overrun
        try:
            if provider == "anthropic":
                return await self._anthropic(model, prompt, images, agent, spend_ok, effort, conversation)
            if provider == "google":
                return await self._gemini(model, prompt, images, agent, spend_ok, effort, conversation)
            return await self._openai_compatible(provider, model, prompt, images, agent, spend_ok,
                                                 conversation if agent else None, effort)
        finally:
            if agent:
                agent.close()  # this attempt's /tmp

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

    async def _post(self, provider: str, url: str, stream: Optional[streaming.Assemble] = None,
                    **kwargs) -> httpx.Response:
        """POST with exponential backoff on rate limits, server errors, and network failures.

        With ``stream`` the reply comes as server-sent events, put back together by ``stream``
        into the reply the provider sends without streaming. The request is then stopped only
        when nothing at all arrives for ``api_reply_deadline``: a model may think for long, as
        long as it keeps sending."""
        deadline = settings.api_reply_deadline
        gate = self.gates.setdefault(provider, ProviderGate())
        limits = settings.provider_limits.get(provider, {})
        for attempt in range(1, self.max_attempts + 1):
            delay = self.retry_base_delay * 2 ** (attempt - 1)
            body = kwargs.get("json") or {}
            ceiling = body.get("max_tokens", body.get("max_completion_tokens",
                       (body.get("generationConfig") or {}).get("maxOutputTokens", 0)))
            requested = input_bound(body) + ceiling
            record = call_records.row()
            try:
                async with gate.hold(limits, requested) as (queued, reservation):
                    if record is not None:
                        record["requests"] += 1
                        record["retries"] += int(attempt > 1)
                        record["queue_seconds"] += queued
                        call_records.save()
                    started = time.monotonic()
                    response = await (self._stream(url, stream, deadline, **kwargs) if stream
                                      else asyncio.wait_for(self.client.post(url, **kwargs), deadline))
                    if record is not None:
                        record["exchanges"].append({"status": response.status_code,
                            "duration_seconds": round(time.monotonic() - started, 3), "queue_seconds": round(queued, 3)})
                    if response.is_success:
                        measured = _response_usage(provider, response.json())
                        if measured[4]:
                            reservation[1] = measured[0] + measured[1]
                        call_records.usage(self.models.get(record["model_id"]) if record else None, *measured)
            except asyncio.TimeoutError as exc:
                # Sending it again would most likely hang again: fail now so another model takes over
                # (quota.UNAVAILABLE matches "request failed"). A stream that stopped is StreamSilent.
                silence = "回复中途停了，没有收到任何数据" if isinstance(exc, StreamSilent) else "没有回复"
                raise ProviderError(f"{provider} request failed: {deadline / 60:g} 分钟{silence}，"
                                    "已停止这次请求") from exc
            except streaming.StreamError as exc:
                raise ProviderError(f"{provider} returned an error in its stream: {exc}") from exc
            except httpx.TransportError as exc:
                if record is not None:
                    record["usage_known"] = False
                call_records.warning("请求传输失败；重试前的请求可能已被计费，其用量尚未确认。")
                if attempt == self.max_attempts:
                    raise ProviderError(f"{provider} request failed: {exc}") from exc
            else:
                if response.status_code not in RETRYABLE_STATUS or attempt == self.max_attempts:
                    return response
                delay = retry_after(response.headers.get("retry-after"), min(delay, self.retry_max_delay))
                if response.status_code == 429:
                    gate.cool_down(delay)
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def _stream(self, url: str, assemble: streaming.Assemble, deadline: float, **kwargs) -> httpx.Response:
        """One streamed POST, waiting at most ``deadline`` for each piece. An error, or a reply
        that is not a stream (a provider that ignores ``stream``), is returned as it came."""
        request = self.client.build_request("POST", url, **kwargs)
        response = await asyncio.wait_for(self.client.send(request, stream=True), deadline)
        try:
            if response.is_error or "text/event-stream" not in response.headers.get("content-type", ""):
                await asyncio.wait_for(response.aread(), deadline)
                return response
            lines, pieces = [], response.aiter_lines()
            while True:
                try:
                    lines.append(await asyncio.wait_for(pieces.__anext__(), deadline))
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError as exc:
                    raise StreamSilent() from exc
            return httpx.Response(200, json=assemble(streaming.events(lines)), request=request)
        finally:
            await response.aclose()

    async def _loop(self, provider: str, model: str, prompt: str, images, agent, spend_ok, step,
                    conversation: Optional[Conversation] = None):
        """Call the model, run the tools it asks for, and call again until it stops asking.

        ``step(state)`` makes one request and returns (text, tool_calls, input, output,
        truncated); ``tool_calls`` are (id, name, arguments) and their results go back via
        ``state``. ``truncated`` means the output hit its token cap. Without a workspace this is
        a single plain call. With ``conversation`` (OpenAI format only), the state is kept
        there, and a conversation of this model with a ``followup`` is continued with it.
        """
        model_id = f"{provider}:{model}"
        tools = agent_tools.tools_for(agent_tools.sandbox_available(), read_only=agent.read_only) if agent else []
        if agent:
            from .extensions import definitions
            tools = [*tools, *definitions(agent)]
        if conversation and conversation.recall and agent:
            tools = [*tools, memory.RECALL_TOOL]
        if conversation and conversation.handoff:
            prompt += conversation.handoff
        # How to work with the tools is a system prompt, as Claude Code gives it; the task is the user's.
        system = agent.instructions() if agent else None
        state = fresh = {"prompt": prompt, "system": system, "images": images, "tools": tools, "history": None,
                         "results": None, "provider": provider}
        resumed = bool(conversation and conversation.model_id == model_id and conversation.followup
                       and conversation.state and conversation.state.get("history"))
        if resumed:
            state = conversation.state
            state.update(tools=tools, system=system, images=images, provider=provider)
            _reopen(state, conversation.followup)
        if conversation:
            conversation.model_id, conversation.state, conversation.followup = model_id, state, None
            conversation.save()
        total_in = total_out = total_cached = turn = 0
        text = ""
        # Without a limit (the default) a long attempt is checked instead (progress.py).
        watch = getattr(agent, "watch", None)
        limit = (agent.max_turns or settings.api_agent_max_turns or (None if watch else UNWATCHED_TURNS)) if agent else 1
        hard_limit = getattr(spend_ok, "turn_remaining", None)
        tool_remaining = getattr(spend_ok, "tool_remaining", None)
        if tool_remaining is not None and tool_remaining() <= 0:
            raise StepLimitExceeded("达到声明工具调用上限，停止工作包。")
        if hard_limit is not None:
            remaining = hard_limit()
            if remaining <= 0:
                raise StepLimitExceeded("达到声明轮次上限，停止工作包。")
            limit = min(limit, remaining) if limit is not None else remaining
        interval = settings.progress_check_minutes * 60
        next_check, seen = time.monotonic() + interval, []

        async def stepped(state):
            """``step(state)``, with the check still made while one request takes long: a
            provider that never answers is otherwise only noticed after its read timeout and
            retries, many minutes later."""
            nonlocal next_check
            pending, started = asyncio.ensure_future(step(state)), time.monotonic()
            try:
                while watch:
                    done, _ = await asyncio.wait({pending}, timeout=max(0.0, next_check - time.monotonic()))
                    if done:
                        break
                    waiting = progress.WAITING.format(minutes=(time.monotonic() - started) / 60)
                    if reason := await watch(f"{progress.api_evidence(turn, seen)}\n{waiting}"):
                        raise ProviderError(f"{model_id} {progress.STOPPED}{reason}")
                    next_check = time.monotonic() + interval
                return await pending
            finally:
                if not pending.done():
                    pending.cancel()
        try:
            # One turn past the limit: the model's chance to answer with a progress summary.
            for turn in itertools.count(1) if limit is None else range(1, limit + 2 if agent else 2):
                final = bool(agent) and limit is not None and turn > limit
                if final and hard_limit is not None:
                    # No paid extra summary request at a declared hard limit.
                    return ProviderResult(text or "声明轮次已用尽，保留工具日志供诊断。", total_in, total_out,
                                          model_id, turns=limit, turn_limit=True,
                                          cached_input_tokens=total_cached)
                if spend_ok and not spend_ok(model_id, total_in, total_out):
                    raise BudgetExceeded("已达到任务可用的预算，模型调用被中止。")
                memory.flush(state)
                if conversation:
                    conversation.save()
                compacted = await self._compact_context(model_id, state, step, spend_ok, conversation) if agent else None
                if compacted:
                    used_in, used_out, used_cached = compacted
                    total_in += used_in
                    total_out += used_out
                    total_cached += used_cached
                if begin := getattr(spend_ok, "begin_turn", None):
                    await begin()
                try:
                    text, calls, used_in, used_out, truncated = await stepped(state)
                except ProviderError as exc:
                    if not (resumed and turn == 1 and " returned 400" in str(exc)):
                        raise
                    # The provider would not take the continued conversation: start afresh.
                    # Preserve an explicit handoff and archive when native continuation is rejected.
                    fresh["prompt"] += memory.handoff(conversation)
                    state = conversation.state = fresh
                    resumed = False
                    if begin:
                        await begin()
                    text, calls, used_in, used_out, truncated = await stepped(state)
                state["calls"] = calls
                state["last_input_tokens"] = used_in
                state["last_history_size"] = len(json.dumps(state.get("history"), ensure_ascii=False).encode())
                used_cached = state.pop("cached", 0) or 0
                total_in, total_out, total_cached = total_in + used_in, total_out + used_out, total_cached + used_cached
                if record := getattr(spend_ok, "record", None):
                    await record(model_id, used_in, used_out, used_cached)
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
                    state["final"] = text
                    if conversation:
                        conversation.save()
                    return ProviderResult(text or "", total_in, total_out, model_id, turns=_turns(turn, limit),
                                          turn_limit=final, cached_input_tokens=total_cached)
                if final:
                    raise ProviderError(f"{model_id} {TURN_LIMIT_MARK.format(limit=limit)}，最后也没有交出进度总结。")
                results = []
                state["results"] = results
                if conversation:
                    conversation.save()  # durable intent before any tool side effects
                # A turn's calls together: its reads and searches run at the same time.
                async def execute_tool(call, results=results, turn=turn):
                    call_id, name, args = call
                    if begin_tool := getattr(spend_ok, "begin_tool", None):
                        try:
                            await begin_tool()  # reserve durably, before any tool side effects
                        except StepLimitExceeded as exc:
                            results.append((call_id, name, f"BLOCKED: 工具未执行：{exc}"))
                            if conversation:
                                conversation.save()
                            raise
                    output, ok = (memory.recall(conversation, args) if name == "RecallHistory" and conversation
                                  else await agent.run(name, args))
                    results.append((call_id, name, output))
                    if conversation:
                        conversation.save()  # each completion survives a later interrupted sibling
                    seen.append({"turn": turn, "tool": name, "detail": agent_tools.describe_call(name, args),
                                 "ok": ok, "output": output[:LOGGED_OUTPUT]})
                    if agent.log is not None and len(agent.log) < MAX_LOGGED_CALLS:
                        agent.log.append(seen[-1])
                    if getattr(agent, "trace", None):
                        _trace(agent.trace, {**seen[-1], "output": output[:TRACE_OUTPUT]})
                if (not settings.api_tool_hooks and
                        all(name in agent_tools.READ_ONLY_TOOLS or name == "RecallHistory" for _id, name, _args in calls)):
                    # Settle admitted siblings before propagating a rejection; no tools may
                    # outlive the attempt or lose their completion checkpoint/accounting.
                    completed = await asyncio.gather(*(execute_tool(call) for call in calls), return_exceptions=True)
                    for result in completed:
                        if isinstance(result, BaseException):
                            raise result
                else:
                    for call in calls:
                        await execute_tool(call)
                # Provider order is stable even if read-only tools finished out of order.
                by_id = {item[0]: item for item in results}
                results = [by_id[call_id] for call_id, _name, _args in calls]
                if limit is not None and (note := _turn_note(limit - turn)):
                    call_id, name, output = results[-1]
                    results[-1] = (call_id, name, f"{output}\n\n{note}")
                state["results"] = results
                if conversation:
                    conversation.save()
                if hard_limit is not None and (
                    hard_limit() <= 0 or (tool_remaining is not None and tool_remaining() <= 0)
                    or (spend_ok.step_remaining is not None and spend_ok.step_remaining() <= 0)
                ):
                    return ProviderResult(text or "声明限额已用尽，保留工具日志供诊断。", total_in, total_out,
                                          model_id, turns=turn, turn_limit=True, cached_input_tokens=total_cached)
                expected, check = getattr(agent, "overrun", None) or (0, None)
                # Far past what a package was planned for: the lead says whether to go on.
                if (check and turn % (expected * progress.OVERRUN_FACTOR) == 0
                        and (reason := await check(progress.api_evidence(turn, seen)))):
                    raise ProviderError(f"{model_id} {progress.OVERRUN}{reason}")
                if watch and time.monotonic() >= next_check:
                    # Not cut off at a time limit: another model says whether it is stuck.
                    if reason := await watch(progress.api_evidence(turn, seen)):
                        raise ProviderError(f"{model_id} {progress.STOPPED}{reason}")
                    next_check = time.monotonic() + interval
            raise AssertionError("unreachable")
        except (Exception, asyncio.CancelledError) as exc:
            if conversation:
                conversation.save()
            exc.usage = ProviderResult("", total_in, total_out, model_id, turns=_turns(turn, limit),
                                       cached_input_tokens=total_cached)
            raise

    async def _compact_context(self, model_id, state, step, spend_ok, conversation):
        """Summarize before context exhaustion; replace only after schema validation and archive.
        The summary uses the same model, budget reservation, effort and hard turn counter."""
        history = state.get("history") or []
        if not history:
            return None
        size = len(json.dumps(history, ensure_ascii=False).encode())
        estimate = max(size // 3, state.get("last_input_tokens", 0) +
                       max(0, size - state.get("last_history_size", 0)) // 3)
        context = getattr(self.models.get(model_id), "context_window", None) or 64000
        if estimate < context * 0.65:
            return None
        if not conversation or not conversation.archive:
            raise ProviderError("上下文接近上限且无法归档，保留记录并停止，禁止静默截断。")
        remaining = getattr(spend_ok, "turn_remaining", None)
        if remaining and remaining() < 2:
            raise StepLimitExceeded("剩余轮次不足以压缩上下文并继续，已保存会话。")
        archive_id = conversation.remember()
        if not archive_id:
            raise ProviderError("无法保存上下文原文，停止压缩。")
        summary_state = {"prompt": memory.SUMMARY_PROMPT + json.dumps(history, ensure_ascii=False),
                         "system": "Return a concise, factual JSON memory. Do not use tools.", "images": [],
                         "tools": [], "history": None, "results": None}
        if begin := getattr(spend_ok, "begin_turn", None):
            await begin()
        text, calls, used_in, used_out, truncated = await step(summary_state)
        cached = summary_state.pop("cached", 0) or 0
        if record := getattr(spend_ok, "record", None):
            await record(model_id, used_in, used_out, cached)
        try:
            if calls or truncated:
                raise ValueError("unfinished summary")
            summary = memory.parse_summary(text or "")
        except (ValueError, TypeError) as exc:
            raise ProviderError("上下文摘要未通过结构验证；原会话已保留，停止本次尝试。") from exc
        # Keep ALL user instructions verbatim (tool results are not user instructions), plus
        # structured observations. If immutable instructions alone do not fit, stop explicitly.
        original = []
        for message in history:
            if message.get("role") == "system":
                original.append(message)
            elif message.get("role") == "user":
                blocks = message.get("content", message.get("parts", ""))
                if (message != state.get("memory_message") and (isinstance(blocks, str) or (isinstance(blocks, list) and
                        not any("tool_result" == b.get("type") or "functionResponse" in b for b in blocks)))):
                    original.append(message)
        notice = memory.user_message(state["provider"], "Task memory (observations, verify before accepting):\n" +
                                     json.dumps(summary, ensure_ascii=False) +
                                     f"\nFull history: RecallHistory archive_id={archive_id}.")
        # No private marker is sent to providers; archive references distinguish generated memory.
        reduced = [*original, notice]
        if len(json.dumps(reduced, ensure_ascii=False).encode()) // 3 > context * 0.5:
            raise ProviderError("必须保留的任务约束超过上下文容量，已归档；需要拆分步骤。")
        state.update(history=reduced, summary=summary, memory_message=notice, last_input_tokens=0, last_history_size=0)
        conversation.save()
        return used_in, used_out, cached

    def _reasoning(self, provider: str, model: str, effort: Optional[str] = None) -> dict:
        """How hard the model should think, in the request fields its API takes: ``effort``
        (settings.reasoning_by_difficulty for steps), or for DeepSeek without one its configured
        effort. Nothing for a model known not to reason, or that rejected these fields before."""
        model_id = f"{provider}:{model}"
        known = self.models.get(model_id)
        effort = effort or (settings.deepseek_reasoning_effort if provider == "deepseek" else None)
        record = call_records.row()
        if record is not None:
            record["requested_effort"] = effort
        if not effort or getattr(known, "supports_reasoning", None) is False or model_id in self.no_effort:
            if effort:
                call_records.warning(f"{model_id} 未应用请求的推理强度 {effort}；使用供应商默认值。")
                if settings.reasoning_fallback == "strict":
                    raise ProviderError("无法保证请求的推理强度；strict 模式禁止降级。")
            if record is not None:
                record["effective_effort"] = "provider_default"
            return {}
        fields = reasoning_fields(provider, effort)
        if record is not None:
            record["effective_effort"] = (fields.get("reasoning_effort") or
                (fields.get("output_config") or {}).get("effort") or
                ((fields.get("generationConfig") or {}).get("thinkingConfig") or {}).get("thinkingLevel"))
        return fields

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
        tpm = settings.provider_limits.get(provider, {}).get("tpm", 0)
        if tpm:
            maximum = min(maximum, tpm - input_bound(body) - 128)
            if maximum < 1:
                raise ProviderError("请求上下文已超过供应商 TPM 配额，不能排队后重试。")
        while True:
            state["limit"] = await self._limit_request(spend_ok, model_id, body, maximum)
            put(body, state["limit"])
            response = await self._post(provider, url, json=body, **kwargs)
            if (
                response.status_code == 400 and state["limit"] > PLAIN_OUTPUT_TOKENS
                and OUTPUT_LIMIT_REJECTED.search(response.text)
            ):
                maximum = state["maximum"] = max(PLAIN_OUTPUT_TOKENS, state["limit"] // 2)
                if record := call_records.row():
                    record["retries"] += 1
                continue
            if response.status_code == 400 and EFFORT_REJECTED.search(response.text) and _drop_reasoning(body):
                if settings.reasoning_fallback == "strict":
                    raise ProviderError(f"{model_id} 拒绝请求的推理参数；strict 模式禁止自动降级。")
                call_records.warning(f"{model_id} 拒绝推理参数，已明确降级为供应商默认值。")
                if record := call_records.row():
                    record["effective_effort"] = "provider_default"
                    record["retries"] += 1
                retried = await self._post(provider, url, json=body, **kwargs)
                if not retried.is_error:
                    self.no_effort.add(model_id)
                return retried
            return response

    async def _openai_compatible(
        self, provider: str, model: str, prompt: str, images: list[Attachment] = (), agent=None, spend_ok=None,
        conversation: Optional[Conversation] = None, effort: Optional[str] = None,
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
                state["history"] = [{"role": "system", "content": state["system"]}] if state.get("system") else []
                state["history"].append({"role": "user", "content": [
                    {"type": "text", "text": state["prompt"]},
                    *({"type": "image_url", "image_url": {
                        "url": f"data:{item.media_type};base64,{files.image_data(item)}",
                    }} for item in state["images"]),
                ] if state["images"] else state["prompt"]})
            for call_id, _name, output in state.pop("results", None) or []:
                state["history"].append({"role": "tool", "tool_call_id": call_id, "content": output})
            if followup := state.pop("followup", None):
                state["history"].append({"role": "user", "content": followup})
            body = {"model": model, "messages": state["history"], **self._reasoning(provider, model, effort)}
            if state["tools"]:
                body["tools"] = [{"type": "function", "function": tool} for tool in state["tools"]]
            field = "max_completion_tokens" if provider == "openai" else "max_tokens"
            body.update(stream=True, stream_options={"include_usage": True})
            response = await self._send(
                provider, f"{provider}:{model}", url, body, lambda body, limit: body.update({field: limit}),
                state, spend_ok, headers={"Authorization": f"Bearer {os.environ[env]}"}, stream=streaming.openai,
            )
            if response.is_error:
                raise ProviderError(f"{provider} returned {response.status_code}: {response.text[:300]}")
            data = response.json()
            usage = data.get("usage") or {}
            state["cached"] = cached_tokens(usage)
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
                _answer(message.get("content")),
                [(call["id"], call["function"]["name"], _arguments(call["function"].get("arguments"))) for call in calls],
                usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                choice.get("finish_reason") == "length",
            )

        return await self._loop(provider, model, prompt, images, agent, spend_ok, step, conversation)

    async def _anthropic(self, model: str, prompt: str, images: list[Attachment] = (), agent=None, spend_ok=None,
                         effort: Optional[str] = None, conversation: Optional[Conversation] = None) -> ProviderResult:
        async def step(state):
            if state["history"] is None:
                state["history"] = [{"role": "user", "content": [
                    *({"type": "image", "source": {
                        "type": "base64", "media_type": item.media_type, "data": files.image_data(item),
                    }} for item in state["images"]),
                    {"type": "text", "text": state["prompt"]},
                ] if state["images"] else state["prompt"]}]
            if state.get("results"):
                state["history"].append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": call_id, "content": output}
                    for call_id, _name, output in state["results"]
                ]})
            body = {"model": model, "messages": _cache_point(state["history"]), **self._reasoning("anthropic", model, effort)}
            if state.get("system"):
                # The tools come before the system prompt in Anthropic's cache, so this caches both.
                body["system"] = [{"type": "text", "text": state["system"], "cache_control": CACHE_POINT}]
            if state["tools"]:
                body["tools"] = [
                    {"name": tool["name"], "description": tool["description"], "input_schema": tool["parameters"]}
                    for tool in state["tools"]
                ]
            body["stream"] = True
            response = await self._send(
                "anthropic", f"anthropic:{model}", "https://api.anthropic.com/v1/messages", body,
                lambda body, limit: body.update(max_tokens=limit), state, spend_ok,
                headers={"x-api-key": os.environ[PROVIDER_ENV["anthropic"]], "anthropic-version": "2023-06-01"},
                stream=streaming.anthropic,
            )
            if response.is_error:
                raise ProviderError(f"anthropic returned {response.status_code}: {response.text[:300]}")
            data = response.json()
            blocks = data["content"]
            calls = [block for block in blocks if block.get("type") == "tool_use"]
            if calls:
                state["history"].append({"role": "assistant", "content": blocks})
            text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
            usage = data["usage"]
            # Anthropic counts cached and cache-writing input apart from input_tokens.
            state["cached"] = usage.get("cache_read_input_tokens") or 0
            return (
                text, [(block["id"], block["name"], block.get("input") or {}) for block in calls],
                usage["input_tokens"] + state["cached"] + (usage.get("cache_creation_input_tokens") or 0),
                usage["output_tokens"],
                data.get("stop_reason") == "max_tokens",
            )

        return await self._loop("anthropic", model, prompt, images, agent, spend_ok, step, conversation)

    async def _gemini(self, model: str, prompt: str, images: list[Attachment] = (), agent=None, spend_ok=None,
                      effort: Optional[str] = None, conversation: Optional[Conversation] = None) -> ProviderResult:
        async def step(state):
            if state["history"] is None:
                state["history"] = [{"role": "user", "parts": [
                    *({"inline_data": {"mime_type": item.media_type, "data": files.image_data(item)}} for item in state["images"]),
                    {"text": state["prompt"]},
                ]}]
            if state.get("results"):
                state["history"].append({"role": "user", "parts": [
                    {"functionResponse": {"name": name, "response": {"result": output}}}
                    for _call_id, name, output in state["results"]
                ]})
            body = {"contents": state["history"], **self._reasoning("google", model, effort)}
            if state.get("system"):
                body["systemInstruction"] = {"parts": [{"text": state["system"]}]}
            if state["tools"]:
                body["tools"] = [{"functionDeclarations": state["tools"]}]
            response = await self._send(
                "google", f"google:{model}",
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent", body,
                lambda body, limit: body.setdefault("generationConfig", {}).update(maxOutputTokens=limit), state, spend_ok,
                params={"key": os.environ[PROVIDER_ENV["google"]], "alt": "sse"}, stream=streaming.gemini,
            )
            if response.is_error:
                raise ProviderError(f"google returned {response.status_code}: {response.text[:300]}")
            data = response.json()
            usage = data.get("usageMetadata", {})
            state["cached"] = usage.get("cachedContentTokenCount") or 0
            candidate = data["candidates"][0]
            content = candidate.get("content") or {}
            parts = content.get("parts") or []
            calls = [part["functionCall"] for part in parts if "functionCall" in part]
            if calls:
                state["history"].append({"role": "model", "parts": parts})
            text = "".join(part.get("text", "") for part in parts)
            return (
                text, [(f"{call['name']}:{i}", call["name"], call.get("args") or {}) for i, call in enumerate(calls)],
                usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0),
                candidate.get("finishReason") == "MAX_TOKENS",
            )

        return await self._loop("google", model, prompt, images, agent, spend_ok, step, conversation)


def _response_usage(provider, data):
    usage = data.get("usageMetadata" if provider == "google" else "usage") or {}
    if provider == "anthropic":
        cached, written = usage.get("cache_read_input_tokens", 0), usage.get("cache_creation_input_tokens", 0)
        return (usage.get("input_tokens", 0) + cached + written, usage.get("output_tokens", 0), cached, written,
                "input_tokens" in usage and "output_tokens" in usage)
    if provider == "google":
        return (usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0),
                usage.get("cachedContentTokenCount", 0), 0, "promptTokenCount" in usage and "candidatesTokenCount" in usage)
    return (usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), cached_tokens(usage), 0,
            "prompt_tokens" in usage and "completion_tokens" in usage)


def _cli_result(text, model_id):
    usage = getattr(text, "usage", {})
    return ProviderResult(str(text), usage.get("input_tokens", 0), usage.get("output_tokens", 0), model_id,
                          cached_input_tokens=usage.get("cached_input_tokens", 0),
                          cache_write_tokens=usage.get("cache_write_tokens", 0), usage_known=bool(usage))


# A 400 about the reasoning effort fields, as providers word it ("Unsupported parameter:
# 'reasoning_effort'", "reasoning_effort 参数值非法", "Unknown name \"thinkingConfig\"").
EFFORT_REJECTED = re.compile(r"reasoning|effort|thinking|output_config|思考|推理", re.IGNORECASE)
# Reasoning effort, from least to most, as settings give it (reasoning_by_difficulty).
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def reasoning_fields(provider: str, effort: str) -> dict:
    """Request fields asking ``provider``'s model for ``effort``: Anthropic's output_config,
    Gemini's thinking level, and reasoning_effort for OpenAI and the providers compatible with it
    (DeepSeek and others take the whole scale; GLM only low, high and max; OpenAI tops out at
    xhigh)."""
    level = effort if effort in EFFORTS else ("max" if effort == "ultra" else "medium")
    if provider == "anthropic":
        return {"output_config": {"effort": {"none": "low", "minimal": "low"}.get(level, level)}}
    if provider == "google":
        thinking = "low" if EFFORTS.index(level) <= EFFORTS.index("low") else "high"
        return {"generationConfig": {"thinkingConfig": {"thinkingLevel": thinking}}}
    if provider == "deepseek":
        return {"reasoning_effort": effort}
    if provider == "glm":
        # GLM 5.x always thinks and takes only low, high or max (2026-10-21: "medium" is a 400).
        return {"reasoning_effort": "low" if EFFORTS.index(level) <= EFFORTS.index("low")
                else "high" if EFFORTS.index(level) <= EFFORTS.index("high") else "max"}
    if provider == "openai" and level == "max":
        level = "xhigh"
    return {"reasoning_effort": level}


def _drop_reasoning(body: dict) -> bool:
    """Take the reasoning effort out of a request body; whether it had one."""
    found = body.pop("reasoning_effort", None) is not None
    found = body.pop("output_config", None) is not None or found
    config = body.get("generationConfig") or {}
    return config.pop("thinkingConfig", None) is not None or found


class StreamSilent(asyncio.TimeoutError):
    """A streamed reply that started, then sent nothing for the whole deadline."""


# Anthropic caches a request only up to a block marked like this (at most four per request).
CACHE_POINT = {"type": "ephemeral"}


def _cache_point(history: list[dict]) -> list[dict]:
    """The conversation to send with its newest message marked for the prompt cache: the next
    turn then reads everything up to it from the cache. The history itself stays unmarked, so
    older turns do not keep their marks."""
    if not history:
        return history
    last = dict(history[-1])
    content = last["content"]
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else [dict(block) for block in content]
    if blocks:
        blocks[-1]["cache_control"] = CACHE_POINT
    last["content"] = blocks
    return [*history[:-1], last]


def _trace(path: Path, call: dict) -> None:
    """Add one tool call to an attempt's full trace; a trace that cannot be written is skipped."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(call, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _arguments(raw) -> dict:
    """Tool arguments arrive as a JSON string; malformed ones become an error the tool reports."""
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"__invalid__": raw}
    return value if isinstance(value, dict) else {"__invalid__": raw}

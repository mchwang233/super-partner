"""Per-invocation accounting. No prompts, credentials or provider response bodies are stored here."""
from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from uuid import uuid4

from .budget import token_cost

current: ContextVar = ContextVar("model_call", default=None)


def save():
    held = current.get()
    if held and held[0] is not None:
        held[0].save_model_call(held[1])


def row():
    held = current.get()
    return held[1] if held else None


def warning(message):
    record = row()
    if record is not None and message not in record["warnings"]:
        record["warnings"].append(message)


def usage(model, input_tokens, output_tokens, cached=0, cache_write=0, known=True):
    record = row()
    if record is None:
        return
    record["usage_known"] = record["usage_known"] and known
    for name, count in (("input_tokens", input_tokens), ("output_tokens", output_tokens),
                        ("cached_input_tokens", cached), ("cache_write_tokens", cache_write)):
        record[name] += count or 0
    record["cost_usd"] += token_cost(model, input_tokens or 0, output_tokens or 0, cached or 0)
    from .cli_quota import record as membership_usage
    membership_usage(record["model_id"], input_tokens or 0, output_tokens or 0, cached or 0)
    save()


def observed(method):
    @wraps(method)
    async def wrapped(self, model_or_session, *args, **kwargs):
        model_id = model_or_session if isinstance(model_or_session, str) else model_or_session.model_id
        budget = kwargs.get("spend_ok")
        context = getattr(budget, "context", {})
        parent = row()
        if parent is not None:
            parent["delegated"] = True
        record = {"id": uuid4().hex, "parent_id": parent["id"] if parent else None,
                  "model_id": model_id, "run_id": context.get("run_id"), "node_id": context.get("node_id"),
                  "purpose": context.get("purpose", "lead" if method.__name__ == "converse" else "helper"),
                  "started_at": datetime.now(timezone.utc).isoformat(), "status": "running",
                  "requested_effort": kwargs.get("effort"), "effective_effort": "unspecified",
                  "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "cache_write_tokens": 0,
                  "usage_known": True, "cost_usd": 0.0, "requests": 0, "retries": 0, "queue_seconds": 0.0,
                  "duration_seconds": 0.0, "warnings": [], "exchanges": []}
        token = current.set((self.db, record))
        started = time.monotonic()
        save()
        try:
            result = await method(self, model_or_session, *args, **kwargs)
            if not record["requests"] and not record.get("delegated"):
                usage(self.models.get(model_id), result.input_tokens, result.output_tokens,
                      result.cached_input_tokens, getattr(result, "cache_write_tokens", 0),
                      getattr(result, "usage_known", True))
            result.warnings = list(dict.fromkeys([*getattr(result, "warnings", []), *record["warnings"]]))
            result.usage_known = record["usage_known"] and result.usage_known
            if record["requests"]:
                result.cache_write_tokens = record["cache_write_tokens"]
            record["status"] = "succeeded"
            return result
        except BaseException as exc:
            record["status"] = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            record["error_type"] = type(exc).__name__
            # A dropped response may have been billed: the observed subtotal is not a total.
            record["usage_known"] = False
            raise
        finally:
            record["duration_seconds"] = round(time.monotonic() - started, 3)
            save()
            current.reset(token)
    return wrapped

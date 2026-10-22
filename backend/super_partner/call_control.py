"""Provider-wide rolling request/token admission and cancellable concurrency gates."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .budget import ProviderError


def retry_after(value, fallback):
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return fallback


class ProviderGate:
    def __init__(self):
        self.condition = asyncio.Condition()
        self.active = 0
        self.window = deque()
        self.blocked_until = 0.0

    def cool_down(self, seconds):
        self.blocked_until = max(self.blocked_until, time.monotonic() + seconds)

    @asynccontextmanager
    async def hold(self, limits, tokens):
        concurrency, rpm, tpm = (limits.get(key, 0) for key in ("concurrency", "rpm", "tpm"))
        if tpm and tokens > tpm:
            raise ProviderError("单次请求的 token 预留超过供应商 TPM 配额；请缩小上下文或修正配额。")
        started = time.monotonic()
        async with self.condition:
            while True:
                now = time.monotonic()
                while self.window and self.window[0][0] <= now - 60:
                    self.window.popleft()
                wait = self.blocked_until - now
                full = (rpm and len(self.window) >= rpm) or (tpm and sum(x[1] for x in self.window) + tokens > tpm)
                if full:
                    wait = max(wait, self.window[0][0] + 60 - now)
                if wait <= 0 and (not concurrency or self.active < concurrency):
                    self.active += 1
                    reservation = [now, tokens]
                    self.window.append(reservation)
                    break
                try:
                    await asyncio.wait_for(self.condition.wait(), timeout=max(0.01, wait) if wait > 0 else None)
                except asyncio.TimeoutError:
                    pass
        try:
            yield time.monotonic() - started, reservation
        finally:
            async with self.condition:
                self.active -= 1
                self.condition.notify_all()


def input_bound(body):
    import json
    # Byte length is a conservative token bound without provider-specific tokenizers.
    return len(json.dumps(body, ensure_ascii=False).encode()) + 256

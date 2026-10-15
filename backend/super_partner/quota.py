"""Noticing that a model has run out of quota, and keeping it out of routing until it resets.

Membership CLIs stop at a usage limit ("Claude AI usage limit reached", "You've hit your
usage limit ... try again in 2 hours"); API models answer 429 (rate limit, quota) or 402 / an
insufficient balance. Such a model is marked exhausted until the reset time the message gives,
or for a default cooldown, and configured_models() leaves it out, so every routing decision
picks a backup instead.
"""
from __future__ import annotations

import hashlib
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from .db import Database

QUOTA = re.compile(
    r"(?:usage|session|weekly|daily|monthly) limit|limit reached|hit your (usage )?limit"
    r"|rate.?limit|quota|insufficient[_ ](balance|quota|credit)"
    r"|credit balance is too low|out of credits|too many requests|billing|returned (429|402)\b|余额不足|额度",
    re.IGNORECASE,
)
# A plain 429 usually clears quickly; running out of money or a membership limit does not.
RATE_LIMIT = re.compile(r"rate.?limit|too many requests|returned 429\b", re.IGNORECASE)
RATE_LIMIT_COOLDOWN = timedelta(minutes=5)
UNAVAILABLE = re.compile(
    r"returned (?:408|500|502|503|504|529)\b|service[_ ]unavailable|overloaded_error"
    r"|\b(?:openai|anthropic|google|deepseek|qwen|doubao|glm|minimax|kimi) request failed:",
    re.IGNORECASE,
)
DEFAULT_COOLDOWN = timedelta(hours=1)
MAX_COOLDOWN = timedelta(days=7)

EPOCH = re.compile(r"\|\s*(\d{10})\b")  # "Claude AI usage limit reached|1760500800"
IN = re.compile(r"(?:try again|resets?|available again)\s+in\s+((?:\d+\s*(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b[\s,and]*)+)", re.IGNORECASE)
PART = re.compile(r"(\d+)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", re.IGNORECASE)
AT = re.compile(
    r"(?:try again at|resets?(?: at)?)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"
    r"(?:\s*\(([^()\s]+)\))?", re.IGNORECASE,
)


class ModelUnavailable(RuntimeError):
    """A model cannot take work until its quota or upstream service recovers."""

    kind = "unavailable"

    def __init__(self, model_id: str, until: datetime, message: str):
        super().__init__(message)
        self.model_id, self.until = model_id, until


class QuotaExhausted(ModelUnavailable):
    """A model ran out of quota; another model should take the work."""

    kind = "quota"


def is_quota_error(text: str) -> bool:
    return bool(QUOTA.search(text or ""))


def failure_kind(text: str) -> Optional[str]:
    if is_quota_error(text):
        return "quota"
    return "unavailable" if UNAVAILABLE.search(text or "") else None


def _signature(message: str) -> str:
    # Exact, model-scoped error signatures; ignore only whitespace and volatile request IDs.
    # Do not promote arbitrary model-generated regexes or commands into shared policy.
    normalized = re.sub(r"request_id[\"'\s:=]+[\w-]+", "request_id=<id>", message.casefold())
    normalized = " ".join(normalized.split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def reset_time(text: str, now: Optional[datetime] = None) -> datetime:
    """When the quota comes back, from the message if it says, else after a cooldown."""
    now = now or datetime.now(timezone.utc)
    text = text or ""
    if match := EPOCH.search(text):
        until = datetime.fromtimestamp(int(match.group(1)), timezone.utc)
    elif match := IN.search(text):
        units = {"d": "days", "h": "hours", "m": "minutes", "s": "seconds"}
        delta = timedelta()
        for amount, unit in PART.findall(match.group(1)):
            delta += timedelta(**{units[unit[0].lower()]: int(amount)})
        until = now + delta
    elif match := AT.search(text):
        # Prefer an explicit zone, e.g. "resets 2pm (Asia/Shanghai)"; otherwise use
        # the machine's clock as before.
        hour, minute, half = int(match.group(1)), int(match.group(2) or 0), (match.group(3) or "").lower()
        hour = hour % 12 + (12 if half == "pm" else 0) if half else hour
        if hour > 23 or minute > 59:
            return now + DEFAULT_COOLDOWN
        local = now.astimezone()
        if match.group(4):
            try:
                local = now.astimezone(ZoneInfo(match.group(4)))
            except (ZoneInfoNotFoundError, ValueError):
                pass
        until = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if until <= local:
            until += timedelta(days=1)
        until = until.astimezone(timezone.utc)
    else:
        return now + (RATE_LIMIT_COOLDOWN if RATE_LIMIT.search(text) else DEFAULT_COOLDOWN)
    return min(max(until, now + timedelta(minutes=1)), now + MAX_COOLDOWN)


class QuotaTracker:
    """Shared model cooldowns and learned failure classifications, optionally persistent."""

    def __init__(self, db: Optional[Database] = None):
        self.db = db
        self.lock = threading.RLock()
        self._until: dict[str, datetime] = {}
        self._reason: dict[str, str] = {}
        self._kind: dict[str, str] = {}
        state = db.load_recovery_state() if db else {}
        self.rules: list[dict] = state.get("rules", [])
        for item in state.get("models", []):
            try:
                until = datetime.fromisoformat(item["until"])
                if until.tzinfo is None or until <= datetime.now(timezone.utc):
                    continue
                model_id = item["model_id"]
                self._until[model_id], self._reason[model_id] = until, item.get("reason", "")
                self._kind[model_id] = item.get("kind", "quota")
            except (KeyError, TypeError, ValueError):
                continue

    def classify(self, model_id: str, message: str) -> Optional[str]:
        known = failure_kind(message)
        if known:
            return known
        signature = _signature(message)
        return next((item["kind"] for item in reversed(self.rules)
                     if item["model_id"] == model_id and item["signature"] == signature), None)

    def learn(self, model_id: str, message: str, kind: str, run_id: str, diagnosis_id: str) -> None:
        if kind not in {"quota", "unavailable"} or not message.strip():
            return
        signature = _signature(message)
        with self.lock:
            self.rules = [item for item in self.rules
                          if (item["model_id"], item["signature"]) != (model_id, signature)]
            self.rules.append({"model_id": model_id, "signature": signature, "kind": kind,
                               "run_id": run_id, "diagnosis_id": diagnosis_id})
            self.rules = self.rules[-200:]
            self._save()

    def _save(self) -> None:
        if self.db:
            self.db.save_recovery_state({"models": [
                {"model_id": mid, "until": until.isoformat(), "reason": self._reason[mid],
                 "kind": self._kind[mid]} for mid, until in self._until.items()
            ], "rules": self.rules})

    def mark(self, model_id: str, message: str, kind: str = "quota") -> ModelUnavailable:
        until = (reset_time(message) if kind == "quota"
                 else datetime.now(timezone.utc) + RATE_LIMIT_COOLDOWN)
        with self.lock:
            self._until[model_id], self._reason[model_id] = until, message[:300]
            self._kind[model_id] = kind
            self._save()
            return self.error(model_id)

    def error(self, model_id: str) -> ModelUnavailable:
        with self.lock:
            until = self._until[model_id]
            quota = self._kind[model_id] == "quota"
            label = "额度用完" if quota else "服务暂时不可用"
            error = QuotaExhausted if quota else ModelUnavailable
            return error(model_id, until, f"{model_id} {label}（{self._reason[model_id][:200]}），"
                         f"{until.astimezone():%m-%d %H:%M} 后重新尝试")

    def exhausted(self, model_id: Optional[str]) -> bool:
        with self.lock:
            until = self._until.get(model_id or "")
            if until and until <= datetime.now(timezone.utc):
                self.clear(model_id)
                return False
            return until is not None

    def clear(self, model_id: str) -> None:
        with self.lock:
            self._until.pop(model_id, None)
            self._reason.pop(model_id, None)
            self._kind.pop(model_id, None)
            self._save()

    def clear_provider(self, provider: str) -> None:
        """A new API key or a refilled account: try that provider's models again."""
        with self.lock:
            for model_id in [item for item in self._until if item.split(":", 1)[0] == provider]:
                self.clear(model_id)

    def public(self) -> list[dict]:
        with self.lock:
            return [
                {"model_id": model_id, "until": until.isoformat(), "reason": self._reason.get(model_id, ""),
                 "kind": self._kind.get(model_id, "quota")}
                for model_id, until in list(self._until.items()) if self.exhausted(model_id)
            ]

"""What a membership CLI costs: its subscription, shared out by the quota each call uses.

A subscription buys a quota (Codex: a weekly window, sometimes also a 5-hour one). Codex writes
the quota's state, and the usage of its saved sessions, into its session files
($CODEX_HOME/sessions/**.jsonl, `token_count` events). The platform's steps run Codex without
saving a session, so the platform keeps its own ledger of every Codex call (`record`), which
outlives deleted tasks; sessions in the leads' folders are the platform's too and are left out.

- calibration: from a baseline reading on, all tokens spent (the ledger plus the user's own
  sessions) over the share of the window used since give the tokens a whole window holds; the
  window's part of the monthly fee over that is the price per token. It is set as the CLI's
  catalog price, so charging, task budgets, routing and the lead's team choice all use it like
  any API price. Until enough of the window is used after the baseline, the CLI has no price.
- pressure: from `cli_quota_soft_percent` of a window used, the CLI loses its first place in
  routing and the lead is told; from `cli_quota_stop_percent` it is marked out of quota until
  the window resets (quota.QuotaTracker), so new work goes to other models.

Only CLIs listed in `settings.cli_plans` are priced; without it nothing changes. Tokens are
weighted as their prices are: uncached input and output 1, input read from cache CACHED_WEIGHT.
The session files are not a published interface: anything unreadable leaves the CLI as it was.
They are written only while Codex works, so once it is stopped they say nothing new, and Codex can
reset a window before the time it gave. When the last reading is older than STALE_MINUTES, the
reader asks Codex's app server for the quota now (`account/rateLimits/read`: no model call, no
quota used), at most every ASK_MINUTES.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import settings

logger = logging.getLogger(__name__)

CACHED_WEIGHT = 0.1
WEEKS_PER_MONTH = 52 / 12
# Below this share of a window used, the tokens per window are too uncertain to calibrate;
# the last good calibration is kept instead.
MIN_CALIBRATION_PERCENT = 2.0
REFRESH_SECONDS = 60
STALE_MINUTES = 10
ASK_MINUTES = 10
ASK_TIMEOUT_SECONDS = 60


@dataclass
class Window:
    used_percent: float
    minutes: int
    resets_at: datetime


@dataclass
class Usage:
    """A CLI's quota now, and the weighted tokens spent in its longest window."""
    windows: list[Window] = field(default_factory=list)
    tokens: float = 0.0
    plan: str = ""

    @property
    def longest(self) -> Optional[Window]:
        return max(self.windows, key=lambda item: item.minutes, default=None)

    @property
    def fullest(self) -> Optional[Window]:
        return max(self.windows, key=lambda item: item.used_percent, default=None)


def weighted(usage: dict) -> float:
    total_in, cached = usage.get("input_tokens") or 0, usage.get("cached_input_tokens") or 0
    cached = min(max(cached, 0), total_in)
    return total_in - cached + cached * CACHED_WEIGHT + (usage.get("output_tokens") or 0)


def _time(value) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _windows(limits: dict) -> list[Window]:
    found = []
    for key in ("primary", "secondary"):
        item = limits.get(key)
        if isinstance(item, dict) and isinstance(item.get("used_percent"), (int, float)):
            try:
                found.append(Window(float(item["used_percent"]), int(item["window_minutes"]),
                                    datetime.fromtimestamp(int(item["resets_at"]), timezone.utc)))
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
    return found


def _events(path: Path, platform: Path):
    """(time, cumulative usage or None for the platform's own sessions, rate limits) of each
    token_count event in a session file."""
    own = False
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle):
                if number == 0 and '"session_meta"' in line:
                    try:
                        cwd = ((json.loads(line).get("payload") or {}).get("cwd") or "")
                        own = bool(cwd) and Path(cwd).is_relative_to(platform)
                    except (ValueError, TypeError, AttributeError):
                        pass
                if '"token_count"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                payload = event.get("payload") or {}
                if payload.get("type") != "token_count":
                    continue
                at = _time(event.get("timestamp"))
                info = payload.get("info") or {}
                if at:
                    yield at, None if own else info.get("total_token_usage") or None, payload.get("rate_limits") or None
    except OSError:
        return


def _window(item) -> Optional[dict]:
    if not isinstance(item, dict):
        return None
    return {"used_percent": item.get("usedPercent"), "window_minutes": item.get("windowDurationMins"),
            "resets_at": item.get("resetsAt")}


def ask_codex(home: Optional[Path] = None, timeout: float = ASK_TIMEOUT_SECONDS) -> Optional[dict]:
    """Codex's quota now, from its app server, in the session files' form; None if it cannot say."""
    from .cli_channels import CLI_SPECS

    executable = shutil.which(CLI_SPECS["cli:codex"].executable)
    if not executable:
        return None
    env = {**os.environ, **({"CODEX_HOME": str(home)} if home else {})}
    messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"clientInfo": {"name": "super-partner", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read"}]
    try:
        process = subprocess.Popen([executable, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, env=env)
    except OSError:
        return None
    timer = threading.Timer(timeout, process.kill)
    timer.start()
    try:
        process.stdin.write("".join(json.dumps(message) + "\n" for message in messages))
        process.stdin.flush()
        for line in process.stdout:
            try:
                answer = json.loads(line)
            except ValueError:
                continue
            if answer.get("id") != 2:
                continue
            limits = (answer.get("result") or {}).get("rateLimits") or {}
            found = {"plan_type": limits.get("planType"), "primary": _window(limits.get("primary")),
                     "secondary": _window(limits.get("secondary"))}
            if (answer.get("result") or {}).get("ordinaryUsageAllowed") is False and found["primary"]:
                found["primary"]["used_percent"] = 100.0  # Codex itself says no more ordinary use
            return found
        return None
    except (OSError, ValueError, AttributeError):
        return None
    finally:
        timer.cancel()
        process.kill()
        process.wait()


class CodexReader:
    """Codex's quota from its session files; each file is parsed again only when it changes.
    ``ask`` gives the quota now when the files are stale (ask_codex)."""

    def __init__(self, home: Optional[Path] = None, ask=None):
        self.home = home
        self.ask = ask
        self._parsed: dict[Path, tuple] = {}
        self._asked: Optional[tuple] = None  # (when, monotonic time, limits or None)

    def _ask(self, now: datetime):
        """The quota from ``ask``, at most every ASK_MINUTES: (time, limits) or None."""
        if self._asked is None or time.monotonic() - self._asked[1] >= ASK_MINUTES * 60:
            try:
                limits = self.ask(self.home)
            except Exception:  # a broken app server leaves the files' reading
                logger.exception("asking Codex for its quota failed")
                limits = None
            self._asked = (now, time.monotonic(), limits)
        return (self._asked[0], self._asked[2]) if self._asked[2] else None

    def sessions(self) -> Path:
        home = self.home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        return home / "sessions"

    def _summary(self, path: Path):
        try:
            stat = path.stat()
        except OSError:
            return None
        key = (stat.st_mtime_ns, stat.st_size)
        cached = self._parsed.get(path)
        if cached and cached[0] == key:
            return cached[1]
        from .folders import lead_root
        events = list(_events(path, lead_root().resolve()))
        self._parsed[path] = (key, events)
        return events

    def read(self, since: datetime, now: Optional[datetime] = None) -> Optional[Usage]:
        """The quota now, and the weighted tokens of the user's own sessions since ``since``."""
        now = now or datetime.now(timezone.utc)
        root = self.sessions()
        if not root.is_dir():
            return None
        latest, latest_at, files = None, None, []
        # A window lasts at most a week; a resumed session appends to its old file, so look at
        # the files changed within the longest window rather than at their folder dates.
        horizon = now.timestamp() - 8 * 24 * 3600
        for path in root.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime < horizon:
                    continue
            except OSError:
                continue
            events = self._summary(path) or []
            files.append(events)
            for at, _total, limits in events:
                if limits and (latest_at is None or at > latest_at):
                    latest, latest_at = limits, at
        for path in [item for item in self._parsed if not item.exists()]:
            del self._parsed[path]
        stale = latest_at is None or (now - latest_at).total_seconds() > STALE_MINUTES * 60
        if self.ask and stale and (asked := self._ask(now)) and (latest_at is None or asked[0] > latest_at):
            latest_at, latest = asked
        if not latest:
            return None
        usage = Usage(windows=_windows(latest), plan=str(latest.get("plan_type") or ""))
        if not usage.longest:
            return None
        start = since.timestamp()
        for events in files:
            before, last = None, None
            for at, total, _limits in events:
                if not total:
                    continue
                if at.timestamp() < start:
                    before = total
                else:
                    last = total
            if last:
                usage.tokens += max(0.0, weighted(last) - (weighted(before) if before else 0.0))
        return usage


READERS = {"cli:codex": CodexReader(ask=ask_codex)}
STOP_LINE = "达到停用线"
_warned: set = set()


def _store() -> Path:
    return settings.data_dir / "cli_quota.json"


def _ledger() -> Path:
    return settings.data_dir / "cli_usage.jsonl"


_ledger_lock = threading.Lock()
LEDGER_DAYS = 8


def record(model_id: str, input_tokens: int, output_tokens: int, cached: int = 0) -> None:
    """Note a platform call's usage of a priced CLI (calls.usage)."""
    if model_id not in READERS or not (input_tokens or output_tokens):
        return
    line = json.dumps({"model_id": model_id, "at": datetime.now(timezone.utc).isoformat(),
                       "tokens": weighted({"input_tokens": input_tokens, "output_tokens": output_tokens,
                                           "cached_input_tokens": cached})})
    with _ledger_lock:
        try:
            _ledger().parent.mkdir(parents=True, exist_ok=True)
            with _ledger().open("a") as handle:
                handle.write(line + "\n")
        except OSError:
            logger.warning("could not record membership usage")


def recorded(model_id: str, since: datetime) -> float:
    """The weighted tokens the platform's calls of ``model_id`` used since ``since``; drops
    entries older than any window."""
    total, keep = 0.0, []
    horizon = datetime.now(timezone.utc).timestamp() - LEDGER_DAYS * 24 * 3600
    with _ledger_lock:
        try:
            lines = _ledger().read_text().splitlines()
        except OSError:
            return 0.0
        for line in lines:
            try:
                item = json.loads(line)
                at = _time(item["at"])
            except (ValueError, KeyError, TypeError):
                continue
            if not at or at.timestamp() < horizon:
                continue
            keep.append(line)
            if item.get("model_id") == model_id and at >= since:
                total += float(item.get("tokens") or 0)
        if len(keep) < len(lines):
            try:
                _ledger().write_text("".join(item + "\n" for item in keep))
            except OSError:
                pass
    return total


def _load() -> dict:
    try:
        return json.loads(_store().read_text())
    except (OSError, ValueError):
        return {}


def _keep(state: dict) -> None:
    try:
        _store().parent.mkdir(parents=True, exist_ok=True)
        _store().write_text(json.dumps(state))
    except OSError:
        pass


def price_per_million(monthly_usd: float, usage: Usage, used_since: float,
                      saved: Optional[float] = None) -> tuple[Optional[float], Optional[float]]:
    """(price per million weighted tokens, tokens a full longest window holds). ``usage.tokens``
    were spent while ``used_since`` percent of the window was used."""
    window = usage.longest
    per_window = saved
    if window and used_since >= MIN_CALIBRATION_PERCENT and usage.tokens > 0:
        per_window = usage.tokens / (used_since / 100)
    if not per_window or not window:
        return None, per_window
    window_usd = monthly_usd / WEEKS_PER_MONTH * (window.minutes / (7 * 24 * 60))
    return window_usd / per_window * 1_000_000, per_window


def starting_price(plan: dict) -> Optional[tuple[float, float]]:
    """The plan's price per million (input, output) until its quota calibrates one, if set."""
    try:
        start = float(plan["input_per_million"]), float(plan["output_per_million"])
    except (KeyError, TypeError, ValueError):
        return None
    return start if min(start) >= 0 else None


def _price(model, input_usd: float, output_usd: float, cached_usd: float) -> None:
    model.input_per_million, model.output_per_million = input_usd, output_usd
    model.cached_input_per_million = cached_usd


def note(usage: Usage, price: Optional[float], monthly_usd: float,
         start: Optional[tuple[float, float]] = None) -> str:
    parts = []
    for window in sorted(usage.windows, key=lambda item: item.minutes, reverse=True):
        label = "周" if window.minutes >= 7 * 24 * 60 else f"{window.minutes // 60} 小时"
        parts.append(f"{label}额度已用 {window.used_percent:g}%，{window.resets_at.astimezone():%m-%d %H:%M} 重置")
    if price:
        priced = f"按月费 ${monthly_usd:g} 折算 ${price:.3g}/百万 tokens"
    elif start:
        priced = f"月费 ${monthly_usd:g}，暂未折算，先按设定价 输入 ${start[0]:g}/输出 ${start[1]:g} 每百万 tokens"
    else:
        priced = f"月费 ${monthly_usd:g}，额度用量太少，暂未折算价格"
    return "；".join([*parts, priced])


def apply(models: dict, tracker=None, now: Optional[datetime] = None) -> None:
    """Price each CLI with a plan by its quota, and steer work away from it as the quota runs
    out. A CLI without a plan, or whose usage cannot be read, keeps the catalog's terms."""
    from .catalog import builtin_models

    builtin = {model.id: model for model in builtin_models()}
    state, changed = _load(), False
    for model_id, reader in READERS.items():
        model = models.get(model_id)
        if not model:
            continue
        plan = settings.cli_plans.get(model_id) or {}
        monthly = float(plan.get("monthly_usd") or 0)
        mine = state.setdefault(model_id, {}) if monthly > 0 else {}
        base = mine.get("base") or {}
        since = _time(base.get("at")) or now or datetime.now(timezone.utc)
        usage = reader.read(since, now) if monthly > 0 else None
        original = builtin.get(model_id)
        set_price = starting_price(plan) if monthly > 0 else None
        if usage:
            _warned.discard(model_id)
        if not usage:
            if monthly > 0 and model_id not in _warned:
                _warned.add(model_id)
                logger.warning("%s: membership usage could not be read; it keeps its catalog price", model_id)
            if set_price:
                _price(model, *set_price, set_price[0] * CACHED_WEIGHT)
            elif original:
                _price(model, original.input_per_million, original.output_per_million,
                       original.cached_input_per_million)
            if original:
                model.execution_priority = original.execution_priority
            model.usage_note = None
            continue
        window = usage.longest
        resets = int(window.resets_at.timestamp())
        if base.get("resets") != resets:
            # The first reading, or a new window: measure from here. A window that reset since the
            # last reading began empty at its start.
            fresh = bool(base) and base.get("resets", 0) < resets
            start = window.resets_at.timestamp() - window.minutes * 60
            mine["base"] = base = {
                "at": (datetime.fromtimestamp(start, timezone.utc) if fresh else (now or datetime.now(timezone.utc))).isoformat(),
                "percent": 0.0 if fresh else window.used_percent, "resets": resets}
            changed = True
            since = _time(base["at"])
            usage = reader.read(since, now) or usage
        usage.tokens += recorded(model_id, since)
        price, per_window = price_per_million(monthly, usage, window.used_percent - base["percent"],
                                              mine.get("per_window"))
        if per_window and per_window != mine.get("per_window"):
            mine["per_window"], changed = per_window, True
        if price is not None:
            _price(model, round(price, 6), round(price, 6), round(price * CACHED_WEIGHT, 6))
        elif set_price:
            _price(model, *set_price, set_price[0] * CACHED_WEIGHT)
        fullest = usage.fullest
        tight = fullest is not None and fullest.used_percent >= settings.cli_quota_soft_percent
        # A CLI goes first in routing only while its quota is comfortable.
        model.execution_priority = 100 if tight else (original.execution_priority if original else 0)
        model.usage_note = note(usage, price, monthly, set_price) + ("；额度紧张，优先给其他模型" if tight else "")
        if tracker is not None and fullest is not None:
            if fullest.used_percent >= settings.cli_quota_stop_percent:
                if not tracker.exhausted(model_id):
                    tracker.mark(model_id, f"会员额度已用 {fullest.used_percent:g}%，{STOP_LINE} "
                                 f"{settings.cli_quota_stop_percent:g}%", until=fullest.resets_at)
            elif tracker.exhausted(model_id) and STOP_LINE in (tracker.public_reason(model_id) or ""):
                tracker.clear(model_id)
    if changed:
        _keep(state)


async def keep_applying(models: dict, tracker=None, interval: float = REFRESH_SECONDS) -> None:
    import asyncio

    while True:
        try:
            await asyncio.to_thread(apply, models, tracker)
        except Exception:  # never let a bad session file stop the service
            logger.exception("membership usage refresh failed")
        await asyncio.sleep(interval)

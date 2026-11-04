"""A membership CLI priced by its subscription: its quota read from Codex's session files, the
platform's own calls from its ledger, and the price and quota pressure that follow (cli_quota.py)."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from super_partner import calls, cli_quota, team
from super_partner.budget import token_cost
from super_partner.catalog import builtin_models
from super_partner.config import settings
from super_partner.quota import QuotaTracker

# Before the real time: the platform's ledger notes its calls when they happen.
NOW = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=3)
RESETS = NOW + timedelta(days=3)


@pytest.fixture
def codex(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "lead_root", str(tmp_path / "leads"))
    monkeypatch.setattr(settings, "cli_plans", {"cli:codex": {"monthly_usd": 200}})
    monkeypatch.setattr(settings, "cli_quota_soft_percent", 70)
    monkeypatch.setattr(settings, "cli_quota_stop_percent", 90)
    monkeypatch.setitem(cli_quota.READERS, "cli:codex", cli_quota.CodexReader(home=tmp_path / "codex"))
    (tmp_path / "codex" / "sessions" / "2026" / "10" / "26").mkdir(parents=True)
    return tmp_path


def session(home, name, cwd, events, resets=RESETS):
    """A Codex session file: (minutes after NOW, cumulative input, cached, output, used percent).
    Codex only appends to its files; a new reading here comes in a new or longer file."""
    lines = [json.dumps({"timestamp": NOW.isoformat(), "type": "session_meta", "payload": {"cwd": str(cwd)}})]
    for minutes, total_in, cached, out, percent in events:
        lines.append(json.dumps({"timestamp": (NOW + timedelta(minutes=minutes)).isoformat(), "type": "event_msg",
                                 "payload": {"type": "token_count", "info": {"total_token_usage": {
                                     "input_tokens": total_in, "cached_input_tokens": cached, "output_tokens": out}},
                                     "rate_limits": {"plan_type": "pro", "primary": {
                                         "used_percent": percent, "window_minutes": 10080,
                                         "resets_at": int(resets.timestamp())}}}}))
    (home / "codex" / "sessions" / "2026" / "10" / "26" / name).write_text("\n".join(lines) + "\n")


def codex_model():
    return next(model for model in builtin_models() if model.id == "cli:codex")


def test_the_quota_comes_from_the_session_files_and_only_the_users_own_sessions_count(codex):
    session(codex, "user.jsonl", codex / "project", [(0, 1000, 0, 100, 10.0), (60, 3000, 1000, 300, 12.0)])
    session(codex, "lead.jsonl", codex / "leads" / "task", [(90, 900_000, 0, 9000, 13.0)])
    usage = cli_quota.READERS["cli:codex"].read(NOW + timedelta(minutes=30), NOW + timedelta(hours=2))
    assert usage.plan == "pro"
    assert [(w.used_percent, w.minutes, w.resets_at) for w in usage.windows] == [(13.0, 10080, RESETS)]
    # The user's tokens after the 30-minute mark: (3000 - 1000 + 100 + 300) - (1000 + 100); the
    # lead's session is the platform's own, counted in its ledger instead.
    assert usage.tokens == pytest.approx(1300)


def test_a_cli_is_priced_by_its_share_of_the_subscription_and_charged_like_an_api_model(codex):
    models = {"cli:codex": codex_model()}
    session(codex, "user.jsonl", codex / "project", [(0, 0, 0, 0, 20.0)])
    cli_quota.apply(models, now=NOW)  # the baseline: no price yet
    assert models["cli:codex"].input_per_million == 0
    assert "暂未折算" in models["cli:codex"].usage_note

    held = calls.current.set((None, {"model_id": "cli:codex", "usage_known": True, "input_tokens": 0,
                                     "output_tokens": 0, "cached_input_tokens": 0, "cache_write_tokens": 0,
                                     "cost_usd": 0.0}))
    try:
        calls.usage(None, 1_000_000, 200_000, cached=500_000)  # a platform step: 750,000 weighted tokens
    finally:
        calls.current.reset(held)
    session(codex, "user.jsonl", codex / "project", [(0, 0, 0, 0, 20.0), (60, 250_000, 0, 0, 25.0)])
    cli_quota.apply(models, now=NOW + timedelta(hours=1))

    # 1,000,000 weighted tokens for 5% of a week: a week holds 20M; $200 a month is $46.15 a week.
    model = models["cli:codex"]
    week_usd = 200 / (52 / 12)
    assert model.input_per_million == model.output_per_million == pytest.approx(week_usd / 20, rel=1e-4)
    assert model.cached_input_per_million == pytest.approx(model.input_per_million * 0.1, rel=1e-4)
    assert token_cost(model, 1_000_000, 200_000, 500_000) == pytest.approx(week_usd * 750_000 / 20_000_000, rel=1e-3)
    assert "周额度已用 25%" in model.usage_note and "按月费 $200 折算" in model.usage_note

    # The calibration is kept: after the window resets, the CLI keeps its price.
    session(codex, "next.jsonl", codex / "project", [(120, 0, 0, 0, 0.5)], resets=RESETS + timedelta(days=7))
    cli_quota.apply(models, now=NOW + timedelta(hours=2))
    assert models["cli:codex"].input_per_million == pytest.approx(week_usd / 20, rel=1e-4)


def test_a_tight_quota_loses_first_place_and_a_full_one_takes_no_new_work_until_it_resets(codex):
    models, tracker = {"cli:codex": codex_model()}, QuotaTracker()
    session(codex, "user.jsonl", codex / "project", [(0, 0, 0, 0, 75.0)])
    cli_quota.apply(models, tracker, now=NOW)
    assert models["cli:codex"].execution_priority == 100
    assert "额度紧张" in models["cli:codex"].usage_note and not tracker.exhausted("cli:codex")

    session(codex, "later.jsonl", codex / "project", [(10, 0, 0, 0, 92.0)])
    cli_quota.apply(models, tracker, now=NOW)
    assert tracker.exhausted("cli:codex")
    [mark] = tracker.public()
    assert mark["until"] == RESETS.isoformat() and "停用线 90%" in mark["reason"]

    session(codex, "next.jsonl", codex / "project", [(20, 0, 0, 0, 10.0)], resets=RESETS + timedelta(days=7))
    cli_quota.apply(models, tracker, now=NOW)  # a new window
    assert not tracker.exhausted("cli:codex") and models["cli:codex"].execution_priority == 0


def test_without_a_plan_a_cli_costs_nothing_and_goes_first_as_before(codex, monkeypatch):
    monkeypatch.setattr(settings, "cli_plans", {})
    models, tracker = {"cli:codex": codex_model()}, QuotaTracker()
    session(codex, "user.jsonl", codex / "project", [(0, 0, 0, 0, 95.0)])
    cli_quota.apply(models, tracker, now=NOW)
    model = models["cli:codex"]
    assert (model.input_per_million, model.execution_priority, model.usage_note) == (0, 0, None)
    assert not tracker.exhausted("cli:codex")


async def test_the_lead_sees_a_clis_quota_when_choosing_the_team(codex):
    from test_planning_latency import CONFIGURED, MODELS, ROSTER

    model = codex_model().model_copy(update={"usage_note": "周额度已用 75%；额度紧张，优先给其他模型"})
    model.scores = MODELS[0].scores
    asked = []

    async def say(text):
        asked.append(text)
        return json.dumps(ROSTER)

    await team.form(say, "实现模块", [*MODELS, model], {*CONFIGURED, model.id})
    assert '"membership_usage": "周额度已用 75%；额度紧张，优先给其他模型"' in asked[0]
    assert "when its quota is tight give the work to other suitable models" in asked[0]


def test_a_starting_price_is_charged_until_the_quota_calibrates_one(codex, monkeypatch):
    monkeypatch.setattr(settings, "cli_plans", {"cli:codex": {
        "monthly_usd": 200, "input_per_million": 0.6, "output_per_million": 2.4}})
    models = {"cli:codex": codex_model()}
    session(codex, "user.jsonl", codex / "project", [(0, 0, 0, 0, 20.0)])
    cli_quota.apply(models, now=NOW)
    model = models["cli:codex"]
    assert (model.input_per_million, model.output_per_million) == (0.6, 2.4)
    assert model.cached_input_per_million == pytest.approx(0.06)
    assert "先按设定价" in model.usage_note

    # Usage that cannot be read still charges the set price, not nothing.
    monkeypatch.setitem(cli_quota.READERS, "cli:codex", cli_quota.CodexReader(home=codex / "missing"))
    models = {"cli:codex": codex_model()}
    cli_quota.apply(models, now=NOW)
    assert (models["cli:codex"].input_per_million, models["cli:codex"].output_per_million) == (0.6, 2.4)


def test_a_stale_full_reading_is_checked_with_codex_and_a_window_reset_early_lets_it_work_again(codex, monkeypatch):
    asked = []

    def ask(home):
        asked.append(home)
        return {"plan_type": "pro", "primary": {"used_percent": 14, "window_minutes": 10080,
                                                 "resets_at": int((RESETS + timedelta(days=2)).timestamp())}}

    monkeypatch.setitem(cli_quota.READERS, "cli:codex", cli_quota.CodexReader(home=codex / "codex", ask=ask))
    models, tracker = {"cli:codex": codex_model()}, QuotaTracker()
    session(codex, "user.jsonl", codex / "project", [(0, 0, 0, 0, 92.0)])
    cli_quota.apply(models, tracker, now=NOW)
    assert tracker.exhausted("cli:codex") and asked == []  # a fresh reading needs no question

    # Stopped, Codex writes nothing new; its window reset before the time it gave.
    cli_quota.apply(models, tracker, now=NOW + timedelta(minutes=cli_quota.STALE_MINUTES + 1))
    assert not tracker.exhausted("cli:codex") and len(asked) == 1
    assert "14%" in models["cli:codex"].usage_note

    cli_quota.apply(models, tracker, now=NOW + timedelta(minutes=cli_quota.STALE_MINUTES + 2))
    assert len(asked) == 1  # asked again only after ASK_MINUTES


def test_when_codex_cannot_say_the_last_reading_stands(codex, monkeypatch):
    monkeypatch.setitem(cli_quota.READERS, "cli:codex",
                        cli_quota.CodexReader(home=codex / "codex", ask=lambda home: None))
    models, tracker = {"cli:codex": codex_model()}, QuotaTracker()
    session(codex, "user.jsonl", codex / "project", [(0, 0, 0, 0, 92.0)])
    cli_quota.apply(models, tracker, now=NOW + timedelta(hours=1))
    assert tracker.exhausted("cli:codex")

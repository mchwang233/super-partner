"""Planning stages must not repeat completed staffing or inherit worker/lead policy accidentally."""
import asyncio
import json

import pytest
from super_partner.config import settings
from super_partner.models import CapabilityScore, ModelProfile, SupervisorSession
from super_partner.planner import autonomous_plan
from super_partner.providers import ProviderResult

MODELS = [ModelProfile(id=f"test:{name}", provider="test", display_name=name,
                      scores=CapabilityScore(**{key: 95 for key in CapabilityScore.model_fields}),
                      scores_source="artificial_analysis", context_window=100000,
                      input_per_million=1, output_per_million=1)
          for name in ("first", "second")]
CONFIGURED = {m.id for m in MODELS}
ROSTER = {"team_brief": "保持接口并独立验收", "employees": [
    {"id": "builder", "name": "开发", "role": "实现模块", "model_id": "test:first", "capabilities": ["coding"]}]}
OUTLINE = {"summary": "模块", "parts": [{"key": "p1", "title": "模块", "goal": "实现模块",
                                       "capability": "coding", "employee_id": "builder", "size": 1}]}


class Planner:
    def __init__(self):
        self.calls = []
        self.sessions = []

    async def converse(self, session, text, **kwargs):
        self.calls.append((session.model_id, "TEAM_SETUP" in text, session.reasoning_effort, kwargs))
        self.sessions.append(session)
        return ProviderResult(json.dumps(ROSTER if "TEAM_SETUP" in text else OUTLINE), 10, 10, session.model_id)


@pytest.fixture
def stage_settings(monkeypatch):
    monkeypatch.setattr(settings, "employee_teams", True)
    # Allow the test to exercise the original implementation before these settings exist.
    for name, value in {"team_reasoning_effort": "medium", "planning_reasoning_effort": "high",
                        "planning_call_timeout": 1.0}.items():
        monkeypatch.setitem(settings.__dict__, name, value)


async def test_planning_uses_stage_effort_without_changing_later_lead_policy(stage_settings):
    provider, session = Planner(), SupervisorSession()
    await autonomous_plan("实现模块", MODELS, CONFIGURED, provider, session=session)
    assert [(team, effort) for _, team, effort, _ in provider.calls] == [(True, "medium"), (False, "high")]
    assert all(kwargs["timeout"] == 1.0 for *_, kwargs in provider.calls)
    assert session.reasoning_effort is None
    assert all(s.reasoning_effort is None for s in provider.sessions)


async def test_outline_fallback_reuses_validated_team_and_keeps_accumulated_cost(stage_settings):
    class FailOutline(Planner):
        async def converse(self, session, text, **kwargs):
            result = await super().converse(session, text, **kwargs)
            if session.model_id == "test:first" and "TEAM_SETUP" not in text:
                raise RuntimeError("outline unavailable")
            if session.model_id == "test:second":
                assert "保持接口并独立验收" in text and "builder" in text
            return result

    provider, session = FailOutline(), SupervisorSession()
    plan = await autonomous_plan("实现模块", MODELS, CONFIGURED, provider, session=session)
    assert [(model, team) for model, team, *_ in provider.calls] == [
        ("test:first", True), ("test:first", False), ("test:second", False)]
    assert plan.employees[0].model_id == "test:first"
    assert plan.planner_model_id == "test:second"
    assert plan.planning_cost_usd == pytest.approx(0.00004)
    assert all(s.reasoning_effort is None for s in provider.sessions)


async def test_deadline_cancels_slow_provider_before_using_next_lead(stage_settings, monkeypatch):
    monkeypatch.setitem(settings.__dict__, "planning_call_timeout", 0.02)
    cancelled = []

    class SlowOutline(Planner):
        async def converse(self, session, text, **kwargs):
            result = await super().converse(session, text, **kwargs)
            if session.model_id == "test:first" and "TEAM_SETUP" not in text:
                try:
                    await asyncio.sleep(0.15)  # provider ignores the supplied timeout
                except asyncio.CancelledError:
                    cancelled.append(session.model_id)
                    raise
            return result

    provider = SlowOutline()
    plan = await asyncio.wait_for(autonomous_plan("实现模块", MODELS, CONFIGURED, provider,
                                                session=SupervisorSession()), timeout=2)
    assert cancelled == ["test:first"]
    assert plan.planner_model_id == "test:second"
    assert sum(team for _, team, *_ in provider.calls) == 1
    assert all(s.reasoning_effort is None for s in provider.sessions)


async def test_unvalidated_team_is_retried_on_another_lead(stage_settings):
    class FailTeam(Planner):
        async def converse(self, session, text, **kwargs):
            result = await super().converse(session, text, **kwargs)
            if session.model_id == "test:first":
                raise RuntimeError("staffing unavailable")
            return result

    provider = FailTeam()
    plan = await autonomous_plan("实现模块", MODELS, CONFIGURED, provider, session=SupervisorSession())
    assert [(model, team) for model, team, *_ in provider.calls] == [
        ("test:first", True), ("test:second", True), ("test:second", False)]
    assert plan.employees and plan.planner_model_id == "test:second"


async def test_cancellation_does_not_start_another_planner(stage_settings):
    class Cancelled(Planner):
        async def converse(self, session, text, **kwargs):
            await super().converse(session, text, **kwargs)
            raise asyncio.CancelledError

    provider = Cancelled()
    with pytest.raises(asyncio.CancelledError):
        await autonomous_plan("实现模块", MODELS, CONFIGURED, provider, session=SupervisorSession())
    assert len(provider.calls) == 1
    assert provider.sessions[0].reasoning_effort is None


async def test_unset_policy_keeps_existing_lead_defaults(monkeypatch):
    monkeypatch.setattr(settings, "employee_teams", True)
    for name, value in {"team_reasoning_effort": "", "planning_reasoning_effort": "",
                        "planning_call_timeout": 0}.items():
        monkeypatch.setitem(settings.__dict__, name, value)
    provider = Planner()
    await autonomous_plan("实现模块", MODELS, CONFIGURED, provider, session=SupervisorSession())
    assert all(effort is None and kwargs == {} for _, _, effort, kwargs in provider.calls)

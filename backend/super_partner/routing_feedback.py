"""Conservative observational routing feedback; cohorts never cross capability/difficulty."""
from __future__ import annotations

from collections import defaultdict

from .models import Outcome

MIN_SAMPLES = 8
OUTCOMES = {Outcome.PASSED, Outcome.REJECTED, Outcome.VERIFY_FAILED, Outcome.UNVERIFIED, Outcome.UNABLE, Outcome.ERROR}


def collect(db):
    groups = defaultdict(lambda: {"samples": 0, "successes": 0, "cost_usd": 0.0, "seconds": 0.0, "timed": 0})
    for run in db.list_runs()[:200]:
        for node in run.plan.nodes:
            # A difficult step with many retries is ONE observed job per model, not many independent samples.
            by_model = defaultdict(list)
            for attempt in node.attempts:
                if attempt.outcome in OUTCOMES:
                    by_model[attempt.model_id].append(attempt)
            for model_id, attempts in by_model.items():
                stats = groups[model_id, node.capability, node.difficulty]
                stats["samples"] += 1
                stats["successes"] += any(item.outcome == Outcome.PASSED for item in attempts)
                stats["cost_usd"] += sum(item.cost_usd for item in attempts)
                timed = [item.duration_seconds for item in attempts if item.duration_seconds is not None]
                if timed:
                    stats["seconds"] += sum(timed)
                    stats["timed"] += 1
    return dict(groups)


def expected_cost(node, model, base, feedback):
    stats = (feedback or {}).get((model.id, node.capability, node.difficulty))
    if not stats or stats["samples"] < MIN_SAMPLES:
        return base
    count = stats["samples"]
    success = (stats["successes"] + 2) / (count + 2)
    # Theoretical costs form a prior; include observed rework even if estimates were optimistic.
    cost = max(base, (stats["cost_usd"] + base * 2) / (count + 2)) / success
    # Latency is a bounded tie-break incentive, never an arbitrary dollar value for time.
    timed = stats.get("timed", 0)
    latency = min(stats.get("seconds", 0) / timed / 300, 2) if timed >= MIN_SAMPLES else 0
    return cost * (1 + 0.1 * latency)


def note(node, model, feedback):
    stats = (feedback or {}).get((model.id, node.capability, node.difficulty))
    if not stats or stats["samples"] < MIN_SAMPLES:
        return ""
    return (f" 同类历史 {stats['samples']} 个步骤、{stats['successes']} 个通过；"
            "已结合返工费用和有记录的耗时排序（观察数据，非因果结论）。")

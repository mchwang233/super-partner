"""Routing scores from Artificial Analysis results.

Routing scores come only from Artificial Analysis (AA); a model AA has not scored has no scores
and is not routed. AA reports 0-100 values on its own scales, while routing compares 0-1 scores
against planner thresholds (both 0-100), so each metric is converted by rank: the model's percentile among
every AA-scored model is placed linearly within SCORE_RANGES for that capability.

Membership CLIs run whatever model the account defaults to, so each inherits the scores of the
strongest AA-scored model from the same vendor.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from typing import Optional

from .models import CapabilityScore, ModelProfile

AA_SOURCE = "artificial_analysis"
INTELLIGENCE = "artificial_analysis_intelligence_index"
# AA metrics behind each routing capability. AA has no Chinese-language or structured-output
# evaluation, so those two follow the overall intelligence index.
CAPABILITY_METRICS: dict[str, tuple[str, ...]] = {
    "coding": ("artificial_analysis_coding_index", "aa_scicode", "aa_terminalbench_4"),
    "planning": (INTELLIGENCE, "aa_hle"),
    "research": ("aa_knowledge_accuracy", "aa_lcr", "aa_hle"),
    "chinese": (INTELLIGENCE,),
    "tool_use": ("artificial_analysis_agentic_index", "aa_terminalbench_4"),
    "structured_output": (INTELLIGENCE,),
    "reliability": ("aa_non_hallucination",),
}
CAPABILITY_LABELS = {
    "coding": "代码", "planning": "规划", "research": "调研", "chinese": "中文",
    "tool_use": "工具使用", "structured_output": "结构化输出", "reliability": "可靠性",
}


# Score span (0-100) per capability, from the lowest- to the highest-ranked AA model. Kept at the
# span the former hand-written scores covered, so planner quality thresholds (50-99, usually
# 70-75) keep their meaning.
SCORE_RANGES: dict[str, tuple[float, float]] = {
    "coding": (74, 97), "planning": (70, 96), "research": (72, 94), "chinese": (76, 97),
    "tool_use": (78, 98), "structured_output": (82, 96), "reliability": (78, 94),
}
# Fixed scores are kept only for the offline demo model, which has no real results.
FIXED_SCORE_PROVIDERS = {"mock"}


def _aa_rows(rows: Iterable[dict]) -> list[dict[str, float]]:
    return [
        {metric["key"]: metric["value"] for metric in row["metrics"]}
        for row in rows if row["source"] == AA_SOURCE
    ]


def _percentile(values: list[float], value: float) -> float:
    """Mid-rank percentile, so ties share a position and the scale runs 0..1."""
    below, through = bisect_left(values, value), bisect_right(values, value)
    return (below + (through - below) / 2) / len(values)


def derive_scores(
    matched_rows: list[dict], population: dict[str, list[float]],
) -> Optional[tuple[CapabilityScore, str, float]]:
    """Scores and a short explanation for one model, or None without an AA intelligence index."""
    candidates = [row for row in _aa_rows(matched_rows) if INTELLIGENCE in row]
    if not candidates:
        return None
    # Several AA rows can match one model (reasoning-effort variants); use the strongest.
    best = max(candidates, key=lambda row: row[INTELLIGENCE])
    scores, notes = {}, []
    for capability, keys in CAPABILITY_METRICS.items():
        used = [key for key in keys if key in best and population.get(key)]
        if not used:
            used = [INTELLIGENCE]
        rank = sum(_percentile(population[key], best[key]) for key in used) / len(used)
        low, high = SCORE_RANGES[capability]
        scores[capability] = round(low + rank * (high - low), 1)
        notes.append(f"{CAPABILITY_LABELS[capability]}前 {max(1, round((1 - rank) * 100))}%")
    basis = (
        f"按 Artificial Analysis 成绩换算（综合智能指数 {best[INTELLIGENCE]:g}）："
        + "，".join(notes)
        + "。按在全部 AA 已评测模型中的排名换算为调度分。"
    )
    return CapabilityScore(**scores), basis, best[INTELLIGENCE]


def apply_aa_scores(models: dict[str, ModelProfile], matches: dict[str, list[dict]],
                    all_rows: Iterable[dict]) -> list[ModelProfile]:
    """Score every model from AA, CLIs from their vendor's best AA model, and clear the rest.

    Mutates the live model objects and returns the changed ones for the caller to persist.
    """
    population: dict[str, list[float]] = {}
    for row in _aa_rows(all_rows):
        for key, value in row.items():
            population.setdefault(key, []).append(value)
    for values in population.values():
        values.sort()
    if not population.get(INTELLIGENCE):
        # No AA results cached yet (first start offline, failed fetch): keep the last scores.
        return []
    changed: list[ModelProfile] = []
    derived: dict[str, tuple[CapabilityScore, str, float]] = {}
    for model in models.values():
        if model.channel != "cli" and (result := derive_scores(matches.get(model.id, []), population)):
            derived[model.id] = result
    # A membership CLI inherits from the strongest AA-scored API model of its vendor.
    flagships: dict[str, ModelProfile] = {}
    for model_id, (_, _, index) in derived.items():
        model = models[model_id]
        current = flagships.get(model.provider)
        if current is None or index > derived[current.id][2]:
            flagships[model.provider] = model
    for model in models.values():
        if model.provider in FIXED_SCORE_PROVIDERS:
            if model.scores_source != "demo":
                model.scores_source, model.scores_basis = "demo", None
                changed.append(model)
            continue
        flagship = flagships.get(model.provider) if model.channel == "cli" else None
        if model.id in derived:
            scores, basis, _ = derived[model.id]
        elif flagship:
            scores, basis = derived[flagship.id][0], (
                f"CLI 使用会员账户的默认模型，沿用同厂商 AA 成绩最好的 {flagship.display_name} 的调度分。"
                + derived[flagship.id][1]
            )
        else:
            scores, basis = None, None
        source = AA_SOURCE if scores else "none"
        if (model.scores, model.scores_source, model.scores_basis) != (scores, source, basis):
            model.scores, model.scores_source, model.scores_basis = scores, source, basis
            changed.append(model)
    return changed


def rescore(models: dict[str, ModelProfile], benchmarks, db) -> list[ModelProfile]:
    """Re-derive AA scores from the benchmark cache and persist what changed."""
    rows = [result.model_dump(mode="json") for result in benchmarks.results()]
    changed = apply_aa_scores(models, benchmarks.match_models(list(models.values())), rows)
    db.save_models(changed)
    return changed

from super_partner.models import CapabilityScore, ModelProfile
from super_partner.scoring import AA_SOURCE, INTELLIGENCE, SCORE_RANGES, apply_aa_scores


def aa_row(index: float, **details: float) -> dict:
    metrics = {INTELLIGENCE: index, **details}
    return {"source": AA_SOURCE, "metrics": [{"key": key, "value": value} for key, value in metrics.items()]}


def model(identifier: str, scores=None, **fields) -> ModelProfile:
    return ModelProfile(**{
        "id": identifier, "provider": identifier.split(":")[0], "display_name": identifier,
        "scores": scores, "context_window": 200_000, "input_per_million": 1,
        "output_per_million": 2, **fields,
    })


# A population of ten AA-scored models with intelligence index 10, 20, ..., 100.
POPULATION = [aa_row(10 * step, aa_non_hallucination=10 * step) for step in range(1, 11)]


def test_aa_results_become_routing_scores_by_rank():
    preset = CapabilityScore(coding=50, planning=50)
    models = {"kimi:k": model("kimi:k", preset), "minimax:m3": model("minimax:m3")}
    matches = {"kimi:k": [aa_row(100, aa_non_hallucination=100)], "minimax:m3": [aa_row(10, aa_non_hallucination=10)]}

    changed = apply_aa_scores(models, matches, POPULATION)

    top, bottom = models["kimi:k"], models["minimax:m3"]
    assert {m.id for m in changed} == {"kimi:k", "minimax:m3"}
    assert top.scores_source == bottom.scores_source == AA_SOURCE
    # Highest of ten sits at the 95th percentile, lowest at the 5th, inside the score span.
    low, high = SCORE_RANGES["planning"]
    assert top.scores.planning == round(low + .95 * (high - low), 1)
    assert bottom.scores.planning == round(low + .05 * (high - low), 1)
    assert top.scores.reliability > bottom.scores.reliability
    assert "综合智能指数 100" in top.scores_basis and "前 5%" in top.scores_basis
    assert bottom.routing_ready


def test_strongest_matched_variant_is_used_and_other_sources_are_ignored():
    models = {"openai:x": model("openai:x")}
    matches = {"openai:x": [
        aa_row(30), aa_row(90),
        {"source": "superclue", "metrics": [{"key": INTELLIGENCE, "value": 100}]},
    ]}
    apply_aa_scores(models, matches, POPULATION)
    assert "综合智能指数 90" in models["openai:x"].scores_basis


def test_models_without_aa_results_lose_their_scores_and_are_not_routed():
    stale = model("deepseek:deepseek-chat", CapabilityScore(coding=82), scores_source="preset")
    demo = model("mock:local", CapabilityScore(coding=80), scores_source="demo")
    models = {"deepseek:deepseek-chat": stale, "minimax:m3": model("minimax:m3"), "mock:local": demo}
    apply_aa_scores(models, {"minimax:m3": [aa_row(50)]}, POPULATION)
    assert stale.scores is None and stale.scores_source == "none" and not stale.routing_ready
    assert models["minimax:m3"].routing_ready
    assert demo.scores == CapabilityScore(coding=80)

    changed = apply_aa_scores(models, {}, POPULATION)

    assert {m.id for m in changed} == {"minimax:m3"}
    assert models["minimax:m3"].scores is None and not models["minimax:m3"].routing_ready


def test_cli_inherits_the_strongest_aa_model_of_its_vendor():
    cli = model("cli:codex", provider="openai", channel="cli")
    models = {
        "cli:codex": cli, "openai:big": model("openai:big"), "openai:small": model("openai:small"),
        "cli:qwen": model("cli:qwen", provider="qwen", channel="cli"),
    }
    matches = {"openai:big": [aa_row(90)], "openai:small": [aa_row(20)], "cli:codex": [aa_row(10)]}
    apply_aa_scores(models, matches, POPULATION)
    assert cli.scores == models["openai:big"].scores
    assert cli.scores_source == AA_SOURCE and "openai:big" in cli.scores_basis
    # No AA-scored Qwen model in the catalog: the Qwen CLI is not routed.
    assert models["cli:qwen"].scores is None


def test_rescoring_with_unchanged_results_changes_nothing():
    models = {"minimax:m3": model("minimax:m3")}
    matches = {"minimax:m3": [aa_row(50)]}
    assert apply_aa_scores(models, matches, POPULATION)
    assert apply_aa_scores(models, matches, POPULATION) == []


def test_without_cached_aa_results_last_scores_are_kept():
    kept = model("minimax:m3", CapabilityScore(coding=90), scores_source=AA_SOURCE)
    models = {"minimax:m3": kept, "minimax:new": model("minimax:new")}
    assert apply_aa_scores(models, {"minimax:new": [aa_row(50)]}, []) == []
    assert kept.scores == CapabilityScore(coding=90) and models["minimax:new"].scores is None


def test_records_saved_on_the_old_zero_to_one_scale_are_upgraded():
    from super_partner.models import TaskNode
    assert CapabilityScore(coding=.82, planning=91.5).model_dump()["coding"] == 82
    assert CapabilityScore(planning=91.5).planning == 91.5
    node = TaskNode(id="n", title="t", instructions="i", capability="coding", quality_threshold=.75)
    assert node.quality_threshold == 75


async def test_model_list_shows_only_models_scored_from_aa(task_api):
    api, client = task_api
    scored = next(m for m in api.models.values() if m.channel == "api")
    original = (scored.scores, scored.scores_source)
    scored.scores, scored.scores_source = CapabilityScore(), AA_SOURCE
    try:
        listed = (await client.get("/api/models")).json()
    finally:
        scored.scores, scored.scores_source = original
    ids = {model["id"] for model in listed}
    assert scored.id in ids and "mock:local" not in ids
    assert all(model["scores_source"] == AA_SOURCE for model in listed)

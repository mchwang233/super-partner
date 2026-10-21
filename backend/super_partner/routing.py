from __future__ import annotations

from typing import Optional

from .budget import token_cost
from .config import settings
from .models import DIFFICULTY_THRESHOLDS, CostEstimate, ModelProfile, TaskNode
from .progress import EXPECTED_TURNS, expected_turns

# Context the project brief's writer needs for a batch of whole files (briefing.BATCH_CHARS).
BRIEF_CONTEXT = 64_000


class NoEligibleModelError(ValueError):
    pass


# How an API model's tool loop costs grow, turn by turn: its prompt, instructions and tool
# definitions; what each turn adds to the conversation (tool output and the model's own message);
# and what it writes each turn, by reasoning effort. Each turn resends the conversation, all but
# the newest part of it read from the provider's prompt cache. Fitted to measured runs (DeepSeek
# Flash at max effort, 2026-10-21: about 1,500 tokens written and 1,300 added per turn);
# the other efforts scale with it.
LOOP_BASE_TOKENS = 3_000
LOOP_TURN_GROWTH = 1_300
LOOP_TURN_OUTPUT = {"none": 250, "minimal": 250, "low": 400, "medium": 750, "high": 1_100, "max": 1_500}


def loop_turns(node: TaskNode) -> int:
    """The tool turns a step is expected to take."""
    return expected_turns(node) or EXPECTED_TURNS.get(node.difficulty, EXPECTED_TURNS["normal"])


def node_cost(node: TaskNode, model: ModelProfile) -> float:
    """What one attempt of ``node`` on ``model`` should cost: one call, or for an API model that
    works through tools, every turn of its loop."""
    if model.input_per_million is None or model.output_per_million is None:
        raise NoEligibleModelError(f"Pricing is not available for {model.id}")
    if model.channel == "cli" or not can_act(model):
        return token_cost(model, node.estimated_input_tokens, node.estimated_output_tokens)
    output = LOOP_TURN_OUTPUT.get(settings.reasoning_by_difficulty.get(node.difficulty) or "", 800)
    context, cost = LOOP_BASE_TOKENS + node.estimated_input_tokens, 0.0
    for turn in range(loop_turns(node)):
        cost += token_cost(model, context, output, context - LOOP_TURN_GROWTH if turn else 0)
        context += LOOP_TURN_GROWTH
    # The final answer, a summary of the work.
    return cost + token_cost(model, context, node.estimated_output_tokens, context - LOOP_TURN_GROWTH)


def eligible_for_node(
    node: TaskNode, models: list[ModelProfile], configured: set[str], needs_vision: bool = False,
) -> list[ModelProfile]:
    return [
        model for model in models
        if model.enabled
        and model.routing_ready
        and model.id in configured
        and model.id not in settings.helper_only_models
        and model.context_window >= node.estimated_input_tokens + node.estimated_output_tokens
        # Work on the project's files needs a CLI agent, or an API model working through tools.
        and (not node.needs_workspace or can_act(model))
        # Attached images reach API models only as image inputs; CLIs open the files themselves.
        and (not needs_vision or model.channel == "cli" or model.supports_vision is not False)
    ]


def can_act(model: ModelProfile) -> bool:
    """Whether the model can edit files and run commands itself."""
    return model.channel == "cli" or (
        settings.api_agent and model.provider != "mock" and model.supports_tools is not False
    )


CAPABILITY_LABELS = {
    "coding": "代码", "planning": "规划", "research": "调研", "chinese": "中文",
    "tool_use": "工具使用", "structured_output": "结构化输出", "reliability": "可靠性",
}


def prefer_api(candidates: list[ModelProfile]) -> tuple[list[ModelProfile], bool]:
    """Keep only API models when any qualify and the API-first policy is on; the flag says
    whether a CLI had to be used because no API model qualified."""
    if not settings.subtasks_prefer_api:
        return candidates, False
    api = [model for model in candidates if model.channel != "cli"]
    return (api, False) if api else (candidates, any(model.channel == "cli" for model in candidates))


def route_node(
    node: TaskNode, models: list[ModelProfile], configured: set[str], needs_vision: bool = False,
) -> TaskNode:
    available = eligible_for_node(node, models, configured, needs_vision)
    eligible = [
        model for model in available
        if model.scores.score_for(node.capability) >= node.quality_threshold
    ]
    if not eligible:
        if not available and node.needs_workspace:
            raise NoEligibleModelError(
                f"{node.title} must work inside the project directory, which requires an "
                "enabled membership CLI or an API model that supports tool calls."
            )
        if not available:
            raise NoEligibleModelError(
                "No enabled and configured model can run this node. Enable a membership CLI, "
                "configure an API key, or enable the Local Demo Model."
            )
        best = max(available, key=lambda model: model.scores.score_for(node.capability))
        best_score = best.scores.score_for(node.capability)
        raise NoEligibleModelError(
            f"No configured model meets {node.capability} >= {node.quality_threshold}; "
            f"best available is {best.id} at {best_score}"
        )
    pool, cli_needed = prefer_api(eligible)
    if node.difficulty == "hard":
        # Hard tasks take the strongest model for the capability; cost only breaks ties.
        key = lambda model: (
            -model.scores.score_for(node.capability),
            -model.scores.reliability,
            model.execution_priority,
            node_cost(node, model),
        )
    else:
        # Easy and normal tasks take the cheapest model that clears the threshold.
        key = lambda model: (
            model.execution_priority,
            node_cost(node, model),
            -model.scores.score_for(node.capability),
            -model.scores.reliability,
        )
    selected = min(pool, key=key)
    node.model_id = selected.id
    node.estimated_cost_usd = round(node_cost(node, selected), 6)
    label = CAPABILITY_LABELS.get(node.capability, node.capability)
    fit = f"{label} {selected.scores.score_for(node.capability):g} ≥ 门槛 {node.quality_threshold:g}"
    rule = "选分最高的" if node.difficulty == "hard" else "选最便宜的"
    if cli_needed:
        api = [model for model in available if model.channel != "cli"]
        best_api = max((model.scores.score_for(node.capability) for model in api), default=None)
        why = (
            "没有可用的 API 模型能做这一步" if best_api is None
            else f"API 模型最高只有 {label} {best_api:g}，达不到门槛"
        )
        node.routing_note = f"{why}，改用会员 CLI（{rule}）。{fit}。"
    elif settings.subtasks_prefer_api and selected.channel != "cli":
        node.routing_note = f"子任务优先用 API 模型，在达标的 API 模型中{rule}。{fit}。"
    else:
        order = "优先会员 CLI，其次最便宜" if node.difficulty != "hard" else "选分最高的"
        node.routing_note = f"在达到门槛的模型中{order}。{fit}。"
    return node


def spread_to(
    node: TaskNode, models: list[ModelProfile], configured: set[str], busy: set[str],
    needs_vision: bool = False,
) -> Optional[ModelProfile]:
    """Another model for ``node`` while the providers in ``busy`` carry enough of the task's
    running steps: one of another API provider that clears the node's threshold, chosen as
    route_node would choose, for at most ``settings.spread_cost_factor`` times the cost of the
    node's own model. None when there is no such model: the node then stays where it is."""
    current = next((model for model in models if model.id == node.model_id), None)
    if current is None:
        return None
    ceiling = node_cost(node, current) * settings.spread_cost_factor
    pool = [
        model for model in eligible_for_node(node, models, configured, needs_vision)
        if model.channel != "cli" and model.provider not in busy
        and model.scores.score_for(node.capability) >= node.quality_threshold
        and node_cost(node, model) <= ceiling + 1e-12
    ]
    if not pool:
        return None
    if node.difficulty == "hard":
        return min(pool, key=lambda model: (-model.scores.score_for(node.capability), -model.scores.reliability,
                                            node_cost(node, model)))
    return min(pool, key=lambda model: (node_cost(node, model), -model.scores.score_for(node.capability)))


def strongest_for(
    node: TaskNode, models: list[ModelProfile], configured: set[str], needs_vision: bool = False,
) -> Optional[ModelProfile]:
    """The best model for a retry: highest score for the capability, cost only breaking ties.
    Under the API-first policy a CLI is used only if no API model clears the threshold."""
    available = eligible_for_node(node, models, configured, needs_vision)
    if not available:
        return None
    meeting = [m for m in available if m.scores.score_for(node.capability) >= node.quality_threshold]
    pool, _ = prefer_api(meeting) if meeting else (available, False)
    return min(pool, key=lambda model: (
        -model.scores.score_for(node.capability), -model.scores.reliability,
        model.execution_priority, node_cost(node, model),
    ))


def stronger_than(
    node: TaskNode, models: list[ModelProfile], configured: set[str], current: Optional[str],
    needs_vision: bool = False, exclude: set[str] | frozenset[str] = frozenset(),
) -> Optional[ModelProfile]:
    """The best other model for a node its current model (and any in ``exclude``) keeps failing
    at. The API-first policy does not apply: a membership CLI is often the way out."""
    available = [m for m in eligible_for_node(node, models, configured, needs_vision)
                 if m.id != current and m.id not in exclude]
    if not available:
        return None
    return min(available, key=lambda model: (
        model.scores.score_for(node.capability) < node.quality_threshold,
        -model.scores.score_for(node.capability), -model.scores.reliability,
        model.channel != "cli", node_cost(node, model),
    ))


def pick_reviewer(
    models: list[ModelProfile], configured: set[str], exclude: Optional[str], tokens: int,
) -> Optional[ModelProfile]:
    """Who checks a result (or whether an attempt is stuck): preferably not the model that
    produced it; then API models under the API-first policy (membership CLIs first otherwise);
    then the cheapest model at least as capable in general as the one checked, since a weaker
    one cannot be trusted to judge its work; failing that, the most capable. Reliability breaks
    ties."""
    available = [
        model for model in models
        if model.enabled and model.routing_ready and model.id in configured
        and model.context_window >= tokens
    ]
    if not available:
        return None
    checked = next((model for model in models if model.id == exclude), None)
    floor = checked.scores.overall if checked else 0.0

    def key(model):
        able = model.scores.overall >= floor
        return (
            model.id == exclude,
            (model.channel == "cli") if settings.subtasks_prefer_api else model.execution_priority,
            not able,
            (model.input_per_million + model.output_per_million) if able else -model.scores.overall,
            -model.scores.reliability,
        )
    return min(available, key=key)


def brief_writer(models: list[ModelProfile], configured: set[str]) -> Optional[ModelProfile]:
    """Who writes the project brief (briefing.py): the cheapest API model with a large enough
    context, the most reliable among equally cheap ones. Plain summaries need no strong model."""
    available = [
        model for model in models
        if model.enabled and model.routing_ready and model.id in configured and model.channel != "cli"
        and model.provider != "mock" and (model.context_window or 0) >= BRIEF_CONTEXT
        and model.input_per_million is not None and model.output_per_million is not None
    ]
    return min(available, key=lambda model: (
        model.input_per_million + model.output_per_million, -model.scores.reliability,
    ), default=None)


def diagnosers(models: list[ModelProfile], configured: set[str]) -> list[ModelProfile]:
    """Strong models to diagnose a stuck task, best first: membership CLIs (they can look at
    the files and cost nothing extra), then the best planners, then the most reliable."""
    available = [
        model for model in models
        if model.enabled and model.routing_ready and model.id in configured and model.provider != "mock"
    ]
    return sorted(available, key=lambda model: (
        model.channel != "cli", -model.scores.planning, -model.scores.reliability,
    ))


def pick_diagnoser(models: list[ModelProfile], configured: set[str]) -> Optional[ModelProfile]:
    candidates = diagnosers(models, configured)
    return candidates[0] if candidates else None


def fit_and_route(node: TaskNode, models: list[ModelProfile], configured: set[str], needs_vision: bool = False) -> Optional[str]:
    """Route ``node``, first lowering its quality threshold to the best available score when no
    available model reaches it. Returns a note saying so, or None."""
    note = None
    available = eligible_for_node(node, models, configured, needs_vision)
    if available:
        best = max(model.scores.score_for(node.capability) for model in available)
        if node.quality_threshold > best:
            note = (f"Adjusted {node.title} {node.capability} threshold from {node.quality_threshold:g} "
                    f"to {best:g} for available models")
            node.quality_threshold = best
    route_node(node, models, configured, needs_vision)
    return note


def part_estimate(part, in_project: bool, models: list[ModelProfile], configured: set[str],
                  needs_vision: bool = False) -> float:
    """What a major part not yet split may cost: its expected number of ordinary tasks of its main
    capability, routed as such."""
    cost = 0.0
    for index in range(max(1, part.size or 3)):
        probe = TaskNode(id=f"{part.id}-{index}", title=part.title, instructions=part.goal or part.title,
                         capability=part.capability, needs_workspace=in_project, difficulty="normal",
                         quality_threshold=DIFFICULTY_THRESHOLDS["normal"])
        fit_and_route(probe, models, configured, needs_vision)
        cost += probe.estimated_cost_usd
    return round(cost, 6)


def reestimate(run) -> None:
    """The task's estimate again, after its plan changed: steps added by splitting a major part
    or by the lead, and parts still to split."""
    plan = run.plan
    plan.estimate = estimate(plan.nodes, run.budget_usd, plan.planning_cost_usd, plan.unsplit_cost())


def estimate(nodes: list[TaskNode], budget: float, planning_cost: float = 0.0, extra: float = 0.0) -> CostEstimate:
    base = planning_cost + extra + sum(node.estimated_cost_usd for node in nodes)
    retry = base * .25
    tools = max(.01, base * .10)
    low = base + tools
    high = base + retry + tools
    return CostEstimate(
        base_usd=round(base, 6), retry_reserve_usd=round(retry, 6),
        tool_reserve_usd=round(tools, 6), low_usd=round(low, 6),
        high_usd=round(high, 6), hard_limit_usd=round(budget, 2),
    )

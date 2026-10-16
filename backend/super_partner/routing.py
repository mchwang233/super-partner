from typing import Optional

from .config import settings
from .models import CostEstimate, ModelProfile, TaskNode


class NoEligibleModelError(ValueError):
    pass


def node_cost(node: TaskNode, model: ModelProfile) -> float:
    if model.input_per_million is None or model.output_per_million is None:
        raise NoEligibleModelError(f"Pricing is not available for {model.id}")
    return (
        node.estimated_input_tokens * model.input_per_million
        + node.estimated_output_tokens * model.output_per_million
    ) / 1_000_000


def eligible_for_node(
    node: TaskNode, models: list[ModelProfile], configured: set[str], needs_vision: bool = False,
) -> list[ModelProfile]:
    return [
        model for model in models
        if model.enabled
        and model.routing_ready
        and model.id in configured
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
    needs_vision: bool = False,
) -> Optional[ModelProfile]:
    """The best other model for a node its current model keeps failing at (running out of
    turns, say). The API-first policy does not apply: a membership CLI is often the way out."""
    available = [m for m in eligible_for_node(node, models, configured, needs_vision) if m.id != current]
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
    """Who checks a result: preferably not the model that produced it; then API models under
    the API-first policy (membership CLIs first otherwise), then the most reliable, then the
    cheapest."""
    available = [
        model for model in models
        if model.enabled and model.routing_ready and model.id in configured
        and model.context_window >= tokens
    ]
    if not available:
        return None
    return min(available, key=lambda model: (
        model.id == exclude,
        (model.channel == "cli") if settings.subtasks_prefer_api else model.execution_priority,
        -model.scores.reliability,
        model.input_per_million + model.output_per_million,
    ))


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


def estimate(nodes: list[TaskNode], budget: float, planning_cost: float = 0.0) -> CostEstimate:
    base = planning_cost + sum(node.estimated_cost_usd for node in nodes)
    retry = base * .25
    tools = max(.01, base * .10)
    low = base + tools
    high = base + retry + tools
    return CostEstimate(
        base_usd=round(base, 6), retry_reserve_usd=round(retry, 6),
        tool_reserve_usd=round(tools, 6), low_usd=round(low, 6),
        high_usd=round(high, 6), hard_limit_usd=round(budget, 2),
    )

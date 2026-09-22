from .models import CostEstimate, ModelProfile, TaskNode


class NoEligibleModelError(ValueError):
    pass


def node_cost(node: TaskNode, model: ModelProfile) -> float:
    return (
        node.estimated_input_tokens * model.input_per_million
        + node.estimated_output_tokens * model.output_per_million
    ) / 1_000_000


def eligible_for_node(
    node: TaskNode, models: list[ModelProfile], configured: set[str],
) -> list[ModelProfile]:
    return [
        model for model in models
        if model.enabled
        and model.id in configured
        and model.context_window >= node.estimated_input_tokens + node.estimated_output_tokens
    ]


def route_node(node: TaskNode, models: list[ModelProfile], configured: set[str]) -> TaskNode:
    available = eligible_for_node(node, models, configured)
    eligible = [
        model for model in available
        if model.scores.score_for(node.capability) >= node.quality_threshold
    ]
    if not eligible:
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
    selected = min(
        eligible,
        key=lambda model: (
            model.execution_priority,
            node_cost(node, model),
            -model.scores.score_for(node.capability),
            -model.scores.reliability,
        ),
    )
    node.model_id = selected.id
    node.estimated_cost_usd = round(node_cost(node, selected), 6)
    return node


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

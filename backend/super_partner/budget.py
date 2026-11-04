"""Request reservations shared by all calls of a run, including reviews and repairs."""
from __future__ import annotations

import json
import math


class ProviderError(RuntimeError):
    pass


class BudgetExceeded(ProviderError):
    pass


class StepLimitExceeded(ProviderError):
    """A declared step limit, independent of the run's budget approval flow."""


class StepReservationBusy(ProviderError):
    """A progress check cannot wait on the worker that is waiting on that check."""


def token_cost(model, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
    """What a call cost: uncached input at the input price, input read from the provider's prompt
    cache at its cache price (the input price when the catalog has none), and output."""
    if not model or model.input_per_million is None or model.output_per_million is None:
        return 0.0
    cached = min(max(cached_tokens or 0, 0), input_tokens)
    cached_price = getattr(model, "cached_input_per_million", None)
    cached_price = model.input_per_million if cached_price is None else cached_price
    return ((input_tokens - cached) * model.input_per_million + cached * cached_price
            + output_tokens * model.output_per_million) / 1_000_000


class RequestBudget:
    def __init__(self, models, remaining, reservations, charge, changed,
                 step_remaining=None, turn_remaining=None, use_turn=None, scope=None, wait_for_step=True):
        self.models, self.remaining = models, remaining
        self.reservations, self.charge = reservations, charge
        self.changed = changed
        self.spent = 0.0
        self.step_remaining = step_remaining
        self.turn_remaining, self.use_turn = turn_remaining, use_turn
        self.scope = scope
        self.wait_for_step = wait_for_step

    async def begin_turn(self):
        if self.turn_remaining is not None:
            if self.turn_remaining() <= 0:
                raise StepLimitExceeded("达到声明轮次上限，停止工作包，禁止自动续跑。")
            await self.use_turn()

    def __call__(self, *_):
        # Usage is already charged by record(); equality permits returning a final reply.
        if self.step_remaining is not None and self.step_remaining() < -0.0000005:
            raise StepLimitExceeded("达到声明费用上限，停止工作包，禁止自动续跑。")
        return self.remaining() >= -0.0000005

    async def reserve(self, model_id: str, payload: dict, maximum: int = 4096) -> int:
        model = self.models.get(model_id)
        if not model or model.input_per_million is None or model.output_per_million is None:
            raise BudgetExceeded(f"缺少 {model_id} 的价格，无法核验请求预算。")
        # UTF-8 bytes plus protocol overhead conservatively bound textual input tokens.
        # Inline images are deliberately included, rather than silently budgeted as free.
        input_bound = len(json.dumps(payload, ensure_ascii=False).encode()) + 256
        input_cost = input_bound * model.input_per_million / 1_000_000
        output_price = model.output_per_million / 1_000_000
        while True:
            other = sum(value for key, value in self.reservations.items() if key is not self)
            remaining = self.remaining()
            available = remaining - other
            if self.step_remaining is not None:
                step_left = self.step_remaining()
                if step_left < input_cost + output_price:
                    raise StepLimitExceeded("声明的步骤费用不足以发起下一轮请求，停止工作包并保留诊断。")
                held = sum(value for key, value in self.reservations.items()
                           if key is not self and self.scope is not None and getattr(key, "scope", None) == self.scope)
                if held and not self.wait_for_step and min(available, step_left - held) < input_cost + output_price:
                    raise StepReservationBusy("本步骤的请求仍占用预算，跳过本次付费进度检查。")
                available = min(available, step_left - held)
            output = min(maximum, math.floor((available - input_cost) / output_price)) if output_price else maximum
            if input_cost <= available and output >= 1:
                self.reservations[self] = input_cost + output * output_price
                return output
            if other and remaining >= input_cost + output_price:
                # Another call may release unused output reservations. Wait automatically;
                # this is contention, not a request for the user to raise the budget.
                self.changed.clear()
                await self.changed.wait()
                continue
            raise BudgetExceeded("剩余预算不足以发起下一轮模型请求，任务已暂停。")

    async def record(self, model_id: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0):
        model = self.models.get(model_id)
        if model and model.input_per_million is not None and model.output_per_million is not None:
            cost = token_cost(model, input_tokens, output_tokens, cached_tokens)
            await self.charge(cost)
            self.spent += cost
        self.release()

    def release(self):
        self.reservations.pop(self, None)
        self.changed.set()

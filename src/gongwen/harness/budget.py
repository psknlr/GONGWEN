"""任务预算：模型调用次数、令牌、修订轮次与时长。超出时显式失败，不悄悄降级。"""

from __future__ import annotations

import time

from ..kernel.config import BudgetConfig
from ..schemas.state import BudgetUsage


class BudgetExceeded(RuntimeError):
    pass


class BudgetGuard:
    def __init__(self, config: BudgetConfig, usage: BudgetUsage | None = None):
        self.config = config
        self.usage = usage or BudgetUsage()
        self._started = time.monotonic()

    def charge_model_call(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.usage.model_calls += 1
        self.usage.input_tokens += input_tokens
        self.usage.output_tokens += output_tokens
        self.check()

    def precheck_model_call(self) -> None:
        if self.usage.model_calls + 1 > self.config.max_model_calls:
            raise BudgetExceeded(f"模型调用次数将超出预算（{self.config.max_model_calls}）")

    def charge_revision_round(self) -> None:
        self.usage.revision_rounds += 1

    def revision_rounds_left(self) -> int:
        return max(0, self.config.max_revision_rounds - self.usage.revision_rounds)

    def tick(self) -> None:
        now = time.monotonic()
        self.usage.wall_seconds += now - self._started
        self._started = now

    def check(self) -> None:
        c, u = self.config, self.usage
        if u.model_calls > c.max_model_calls:
            raise BudgetExceeded(f"模型调用次数超出预算（{u.model_calls}/{c.max_model_calls}）")
        if u.input_tokens > c.max_input_tokens:
            raise BudgetExceeded(f"输入令牌超出预算（{u.input_tokens}/{c.max_input_tokens}）")
        if u.output_tokens > c.max_output_tokens:
            raise BudgetExceeded(f"输出令牌超出预算（{u.output_tokens}/{c.max_output_tokens}）")
        if u.wall_seconds > c.max_wall_seconds:
            raise BudgetExceeded(f"处理时长超出预算（{u.wall_seconds:.0f}s/{c.max_wall_seconds:.0f}s）")

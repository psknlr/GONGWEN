"""阶段5：事实账本 FactLedger（技能5 的输出）。

六种状态（设计 §5 阶段5）决定写作约束：
已核实事实 / 材料记载 / 计算结果 / 拟议内容 / 已批准事项 / 未知或冲突。
“拟建设20个示范点”永远不能被写成“已建成20个示范点”。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field

from .common import GWModel, Locator


class FactStatus(str, Enum):
    VERIFIED = "已核实事实"
    RECORDED = "材料记载"
    COMPUTED = "计算结果"
    PROPOSED = "拟议内容"
    APPROVED = "已批准事项"
    UNKNOWN = "未知"
    CONFLICT = "冲突"

    @property
    def usable_as_fact(self) -> bool:
        return self in (FactStatus.VERIFIED, FactStatus.RECORDED, FactStatus.COMPUTED, FactStatus.APPROVED)


class Progress(str, Enum):
    """事项进展语义：用于检测“拟—已”状态升级。"""

    PLANNED = "计划/拟议"
    ONGOING = "推进中"
    COMPLETED = "已完成"
    NONE = "不适用"

    @property
    def rank(self) -> int:
        return {"计划/拟议": 0, "推进中": 1, "已完成": 2, "不适用": -1}[self.value]


class Verification(GWModel):
    method: str = Field(description="来源核对/程序计算/人工确认/多源一致")
    by: str = "system"
    at: datetime | None = None
    note: str = ""


class Formula(GWModel):
    expression: str = Field(description="如 F-003 + F-004、F-010 / F-011")
    inputs: list[str]
    unit: str = ""
    caliber: str = Field(default="", description="统计口径")
    result_repr: str = ""


class Fact(GWModel):
    fact_id: str
    statement: str = Field(description="规范化陈述")
    subject: str = ""
    attribute: str = Field(default="", description="指标/属性名，用于跨来源比对")
    value: float | str | None = None
    unit: str = ""
    kind: str = Field(default="text", description="money/count/percent/date/duration/text/org/person")
    as_of: str = Field(default="", description="统计时点")
    caliber: str = Field(default="", description="统计口径/范围，如“仅统计已验收项目”")
    status: FactStatus
    progress: Progress = Progress.NONE
    sources: list[Locator] = Field(default_factory=list)
    verification: Verification | None = None
    formula: Formula | None = None
    approval_ref: str | None = Field(default=None, description="已批准事项须绑定真实审批记录")
    depends_on: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    notes: str = ""

    def display_value(self) -> str:
        if self.value is None:
            return ""
        if isinstance(self.value, float):
            v = f"{self.value:.4f}".rstrip("0").rstrip(".")
        else:
            v = str(self.value)
        return f"{v}{self.unit}"


class FactConflict(GWModel):
    conflict_id: str
    attribute: str
    fact_ids: list[str]
    description: str
    resolution: str | None = None


class CalcCheck(GWModel):
    check_id: str
    kind: str = Field(description="sum/ratio/yoy/unit/caliber")
    description: str
    expected: str
    actual: str
    ok: bool
    locator: Locator | None = None
    severity: str = "重要"


class FactLedger(GWModel):
    facts: list[Fact] = Field(default_factory=list)
    conflicts: list[FactConflict] = Field(default_factory=list)
    calc_checks: list[CalcCheck] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)

    def get(self, fact_id: str) -> Fact | None:
        for f in self.facts:
            if f.fact_id == fact_id:
                return f
        return None

    def by_status(self, *statuses: FactStatus) -> list[Fact]:
        return [f for f in self.facts if f.status in statuses]

    def dependents(self, fact_id: str) -> list[Fact]:
        """所有直接或间接依赖某事实的计算结果（用于定向修订的影响范围分析）。"""
        out: list[Fact] = []
        frontier = {fact_id}
        seen: set[str] = set()
        while frontier:
            nxt: set[str] = set()
            for f in self.facts:
                if f.fact_id in seen:
                    continue
                if frontier & set(f.depends_on):
                    out.append(f)
                    seen.add(f.fact_id)
                    nxt.add(f.fact_id)
            frontier = nxt
        return out

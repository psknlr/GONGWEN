"""阶段6：提纲与措施表 OutlinePlan（技能6 的输出）。

措施统一表示为：主体—行为—对象—条件—时限—义务强度—例外—依据（设计 §6.2）。
系统可以提示缺失、提出候选，但不能擅自确定责任和承诺。
"""

from __future__ import annotations

from pydantic import Field

from .common import EvidenceRef, GWModel


class Measure(GWModel):
    measure_id: str
    subject: str = Field(default="", description="主体（谁做）")
    action: str = Field(default="", description="行为（做什么）")
    obj: str = Field(default="", description="对象")
    condition: str = ""
    deadline: str = ""
    obligation: str = Field(default="", description="必须/应当/要/原则上/可以/鼓励/研究探索")
    exceptions: str = ""
    basis: list[EvidenceRef] = Field(default_factory=list)
    status: str = Field(default="拟议", description="已批准/拟议/候选（系统建议）")
    origin: str = Field(default="材料记载", description="用户确认/材料记载/系统建议")
    resources: list[str] = Field(default_factory=list, description="涉及资源的事实 ID")
    acceptance: str = Field(default="", description="如何验收")
    confirmed: bool = False
    text: str = Field(default="", description="原始表述")


class ParagraphPlan(GWModel):
    para_id: str
    function: str = Field(description="依据/事实/分析/措施/条件/要求/请求/结语/背景")
    purpose: str
    core: str = Field(default="", description="要表达的核心内容")
    refs: list[EvidenceRef] = Field(default_factory=list)
    measure_ids: list[str] = Field(default_factory=list)
    confirmed_items: list[str] = Field(default_factory=list)
    suggested_items: list[str] = Field(default_factory=list, description="仍为建议、待确认的内容")
    budget_chars: int = 200


class SectionPlan(GWModel):
    section_id: str
    label: str = Field(default="", description="层次序数，如 一、")
    heading: str = ""
    role: str = Field(default="", description="内容契约中的要素，如 请示事项、执行要求")
    paragraphs: list[ParagraphPlan] = Field(default_factory=list)


class AlternativePlan(GWModel):
    key: str
    name: str
    applicable_when: str
    resources: str = ""
    risks: str = ""
    differences: list[str] = Field(default_factory=list)


class OutlinePlan(GWModel):
    genre: str | None
    material_type: str | None = None
    title: str
    sections: list[SectionPlan] = Field(default_factory=list)
    opening: ParagraphPlan | None = None
    closing: ParagraphPlan | None = None
    measures: list[Measure] = Field(default_factory=list)
    alternatives: list[AlternativePlan] = Field(default_factory=list)
    chosen_alternative: str | None = None
    attachments: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    contract_missing: list[str] = Field(default_factory=list, description="文种内容契约中尚缺的要素")
    total_budget_chars: int = 1500

    def measure(self, measure_id: str) -> Measure | None:
        for m in self.measures:
            if m.measure_id == measure_id:
                return m
        return None

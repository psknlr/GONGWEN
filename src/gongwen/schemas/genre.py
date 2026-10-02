"""阶段3：文种、行文关系、权限与必要程序判断 GenreDecision（技能3 的输出）。"""

from __future__ import annotations

from pydantic import Field

from .common import GWModel, RuleLevel


class RuleCitation(GWModel):
    """规则依据引用。level 决定对用户的措辞（“条例规定”还是“实务惯例”）。"""

    rule_id: str
    level: RuleLevel
    source: str = Field(description="如 《党政机关公文处理工作条例》第十五条（四）")
    policy_id: str | None = None
    article: str | None = None

    def phrase(self) -> str:
        if self.level in (RuleLevel.PRACTICE, RuleLevel.UNIT):
            return f"（{self.level.value}：{self.source}）"
        if self.level == RuleLevel.PENDING:
            return f"（待核：{self.source}，存在分歧，请核对本单位现行细则）"
        return f"（依据：{self.source}）"


class ProcedureRequirement(GWModel):
    """需要衔接的真实程序。系统只识别“需要什么”，绝不生成“已通过”结论。"""

    code: str = Field(description="LEGALITY_REVIEW/FAIR_COMPETITION/CONSULTATION/LEVEL_APPROVAL/COLLECTIVE_DECISION/PRINCIPAL_SIGN/...")
    name: str
    trigger: str = Field(description="触发理由")
    basis: list[RuleCitation] = Field(default_factory=list)
    status: str = Field(default="需人工判断是否适用", description="需人工判断是否适用/需真实程序及材料/不适用（人工确认）")
    materials_needed: list[str] = Field(default_factory=list)


class AuthorityFinding(GWModel):
    code: str
    message: str
    severity: str = "重要"
    basis: list[RuleCitation] = Field(default_factory=list)
    out_of_authority: bool = False


class GenreDecision(GWModel):
    layer: str
    direction: str
    suggested_genre: str | None = Field(description="法定文种；事务材料为 None")
    material_type: str | None = Field(default=None, description="事务材料类型，如 工作方案、讲话稿")
    format_type: str = Field(default="general", description="general/letter/command/jiyao")
    issue_vehicle: str | None = Field(default=None, description="事务材料需正式下发时的载体，如“通知（印发类）”")
    requested_genre: str | None = None
    conflicts: list[str] = Field(default_factory=list, description="与用户字面要求不一致之处及理由")
    reasons: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    citations: list[RuleCitation] = Field(default_factory=list)
    authority_findings: list[AuthorityFinding] = Field(default_factory=list)
    procedures: list[ProcedureRequirement] = Field(default_factory=list)
    confidence: float = 0.0
    needs_human: bool = False
    single_matter_check: list[str] = Field(default_factory=list, description="请示一文一事检测到的事项")

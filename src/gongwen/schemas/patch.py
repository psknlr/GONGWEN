"""阶段8：定向修订 PatchSet（技能10 的输出）。

每处修改都要交代：修改位置、修改内容、修改原因、依据、关联影响、重新检查结果。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from .common import EvidenceRef, GWModel, utcnow


class SemanticChange(GWModel):
    """语义强度变化：义务强度、事实状态、范围、决策状态、例外条件、时限、主体。"""

    dimension: str
    before: str
    after: str
    direction: str = Field(default="", description="增强/减弱/改变")
    severity: str = "重要"
    needs_human: bool = True


class Patch(GWModel):
    patch_id: str
    doc_id: str
    target: str = Field(description="句子 ID、块 ID 或要素字段，如 s-012 / b-004 / title")
    op: str = Field(description="replace/insert_after/delete/set_field")
    before: str = ""
    after: str = ""
    reason: str
    issue_ids: list[str] = Field(default_factory=list)
    basis: list[EvidenceRef] = Field(default_factory=list)
    impact: list[str] = Field(default_factory=list, description="关联影响的节点或文稿")
    semantic_changes: list[SemanticChange] = Field(default_factory=list)
    author: str = Field(default="system", description="system/model/human")
    status: str = Field(default="proposed", description="proposed/applied/rejected/needs_human")
    created_at: datetime = Field(default_factory=utcnow)


class FactChange(GWModel):
    """人工发起的关键事实变更（如经费 100 万元改为 80 万元）。"""

    fact_id: str
    new_value: float | str
    unit: str | None = None
    reason: str = ""
    by: str = "human"


class RecheckResult(GWModel):
    target: str
    checks: list[str] = Field(default_factory=list)
    remaining_issue_ids: list[str] = Field(default_factory=list)
    ok: bool = True


class PatchSet(GWModel):
    doc_id: str
    round: int
    from_version: int
    to_version: int | None = None
    patches: list[Patch] = Field(default_factory=list)
    fact_changes: list[FactChange] = Field(default_factory=list)
    affected_nodes: list[str] = Field(default_factory=list)
    affected_docs: list[str] = Field(default_factory=list)
    recheck: list[RecheckResult] = Field(default_factory=list)
    escalated_issue_ids: list[str] = Field(default_factory=list, description="自动修订后仍未解决、转交人工的问题")

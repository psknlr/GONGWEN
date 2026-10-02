"""任务状态、人工审核节点与事项模型。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import Field

from .common import DocStatus, GWModel, IdAllocator, utcnow


class Stage(str, Enum):
    """设计 §8.1 状态机。"""

    ADMISSION = "材料准入"
    TASK_CONFIRM = "任务确认"
    PARSING = "材料解析"
    EVIDENCE = "依据与事实准备"
    OUTLINE_CONFIRM = "提纲确认"
    DRAFTING = "起草"
    REVIEW = "审校"
    REVISION = "定向修订"
    LAYOUT = "排版检查"
    HUMAN_REVIEW = "人工送审"
    # 异常状态：失败必须可见，不能悄悄降级为猜测
    NEED_MATERIAL = "待补材料"
    CONFLICT = "发现冲突"
    OUT_OF_AUTHORITY = "超出权限"
    FAILED = "处理失败"
    BLOCKED = "禁止进入"
    # 终态
    SUBMITTED = "已形成送审材料"
    APPROVED = "已绑定审批记录"

    @property
    def is_exception(self) -> bool:
        return self in (Stage.NEED_MATERIAL, Stage.CONFLICT, Stage.OUT_OF_AUTHORITY, Stage.FAILED, Stage.BLOCKED)


class CheckpointKind(str, Enum):
    MATERIAL_CONFIRM = "材料准入确认"
    TASK_CONFIRM = "任务契约确认"
    OUTLINE_CONFIRM = "提纲与措施确认"
    CONFLICT = "冲突处理"
    AUTHORITY = "权限与行文关系确认"
    NEED_MATERIAL = "补充材料"
    REVIEW_ESCALATION = "审校问题人工处理"
    SEMANTIC_CHANGE = "关键语义变化确认"
    HUMAN_REVIEW = "人工送审"


class CheckpointOption(GWModel):
    key: str
    label: str
    effect: str = ""


class Checkpoint(GWModel):
    cp_id: str
    kind: CheckpointKind
    stage: Stage
    question: str
    details: list[str] = Field(default_factory=list)
    options: list[CheckpointOption] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    status: str = Field(default="pending", description="pending/resolved/cancelled")
    resolution: dict[str, Any] | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    human_only: bool = Field(default=True, description="只能由人通过人工通道处理，模型通道不可代为确认")
    auto_acceptable: bool = Field(default=False, description="无头模式下是否允许按显式参数自动接受默认项")
    created_at: datetime = Field(default_factory=utcnow)


class ApprovalRecord(GWModel):
    """真实审批记录：由有权限的人导入，绑定到具体文稿版本哈希。"""

    approval_id: str
    approver: str
    approver_title: str = ""
    approved_at: str
    scope: str = Field(description="批准范围")
    doc_id: str
    doc_version: int
    doc_hash: str
    source: str = Field(description="审批记录来源，如 OA 流程号、签批单扫描件")
    imported_by: str
    imported_at: datetime = Field(default_factory=utcnow)


class BudgetUsage(GWModel):
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    revision_rounds: int = 0
    wall_seconds: float = 0.0


class StageRecord(GWModel):
    stage: Stage
    entered_at: datetime = Field(default_factory=utcnow)
    exited_at: datetime | None = None
    outcome: str = ""


class TaskState(GWModel):
    task_id: str
    matter_id: str
    created_at: datetime = Field(default_factory=utcnow)
    created_by: str = "unknown"
    stage: Stage = Stage.ADMISSION
    previous_stage: Stage | None = None
    history: list[StageRecord] = Field(default_factory=list)
    checkpoints: list[Checkpoint] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict, description="产物名 → 内容哈希")
    doc_ids: list[str] = Field(default_factory=list)
    current_version: int = 0
    doc_status: DocStatus = DocStatus.DISCUSSION
    budget: BudgetUsage = Field(default_factory=BudgetUsage)
    ids: IdAllocator = Field(default_factory=IdAllocator)
    errors: list[str] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)
    approvals: list[ApprovalRecord] = Field(default_factory=list)
    exception_reason: str = ""

    def pending_checkpoints(self) -> list[Checkpoint]:
        return [c for c in self.checkpoints if c.status == "pending"]

    def checkpoint(self, cp_id: str) -> Checkpoint | None:
        for c in self.checkpoints:
            if c.cp_id == cp_id:
                return c
        return None


class MatterDocument(GWModel):
    doc_id: str
    genre: str | None
    role: str = Field(description="如 请示正文、可行性说明、经费表、实施方案、汇报摘要")
    version: int = 1


class MatterModel(GWModel):
    """事项级数据模型：同一事项的多份文稿共享经确认的事实与措施。"""

    matter_id: str
    title: str = ""
    task_ids: list[str] = Field(default_factory=list)
    documents: list[MatterDocument] = Field(default_factory=list)
    fact_ledger_hash: str | None = None
    approvals: list[ApprovalRecord] = Field(default_factory=list)

"""阶段9：送审打包 ReviewPackage（技能12 的输出）。

标准交付 = 文稿 + 事实依据表 + 规范检查结果 + 待确认事项 + 版本修改记录（设计 §1.2）。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from .common import DocStatus, GWModel, utcnow
from .genre import ProcedureRequirement
from .layout import LayoutReport, OutputFile


class EvidenceRow(GWModel):
    sentence_id: str
    location: str
    text: str
    refs: list[dict[str, str]] = Field(default_factory=list, description="[{kind,id,label,status,source}]")
    confirmation: str = Field(default="", description="确认状态摘要")


class PendingItem(GWModel):
    item_id: str
    kind: str
    description: str
    location: str = ""
    blocking: bool = False


class VersionEntry(GWModel):
    version: int
    created_at: datetime
    author: str
    summary: str
    patch_ids: list[str] = Field(default_factory=list)
    doc_hash: str
    semantic_changes: list[str] = Field(default_factory=list)


class ReviewPackage(GWModel):
    task_id: str
    matter_id: str
    doc_id: str
    doc_version: int
    doc_hash: str
    status: DocStatus
    status_reasons: list[str] = Field(default_factory=list)
    genre: str | None
    title: str
    outputs: list[OutputFile] = Field(default_factory=list)
    evidence_table: list[EvidenceRow] = Field(default_factory=list)
    issue_counts: dict[str, int] = Field(default_factory=dict)
    open_issue_ids: list[str] = Field(default_factory=list)
    pending: list[PendingItem] = Field(default_factory=list)
    procedures: list[ProcedureRequirement] = Field(default_factory=list)
    versions: list[VersionEntry] = Field(default_factory=list)
    layout: LayoutReport | None = None
    admission_summary: list[str] = Field(default_factory=list)
    rule_versions: dict[str, str] = Field(default_factory=dict)
    generated_at: datetime = Field(default_factory=utcnow)
    disclaimers: list[str] = Field(
        default_factory=lambda: [
            "本材料为附带可核验证据包的草稿，不代表机器已证明全文合法正确。",
            "系统不形成审批结论；成文日期、发文字号、签发信息须来自真实办理流程。",
            "涉及合法性审核、公平竞争审查等专门程序的，须按适用规定另行办理。",
        ]
    )

"""阶段0/2：材料准入结果与可定位来源 SourceBundle（技能2 材料解析的输出）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from .common import AdmissionDecision, Clearance, GWModel, Locator, utcnow


class AdmissionFinding(GWModel):
    code: str = Field(description="如 SECRET_MARK、WORK_SECRET_MARK、HIDDEN_TEXT、TRACKED_CHANGES、PII_ID_CARD")
    detail: str
    where: str = Field(default="", description="正文/文件名/批注/修订痕迹/隐藏字段/页眉页脚/元数据/隐藏工作表")
    clearance: Clearance = Clearance.UNKNOWN


class AdmissionResult(GWModel):
    material_id: str
    filename: str
    decision: AdmissionDecision
    detected_clearance: Clearance
    declared_clearance: Clearance | None = None
    findings: list[AdmissionFinding] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None


class Material(GWModel):
    """材料登记信息。原始文件以内容哈希存放且不可变。"""

    material_id: str
    filename: str
    media_type: str
    sha256: str
    size: int
    zone: str = "matter"
    matter_id: str
    declared_clearance: Clearance | None = None
    clearance: Clearance = Clearance.UNKNOWN
    admission: AdmissionDecision | None = None
    role: str = Field(default="material", description="material/approval_record/statistics/meeting_record/template/example")
    authoritative: bool = Field(default=False, description="经人工确认的权威来源（如已发布统计数据）")
    uploaded_by: str = "unknown"
    created_at: datetime = Field(default_factory=utcnow)
    description: str = ""


class SourceUnit(GWModel):
    """可定位的最小来源单元：段落、表格单元格、页面文本、批注等。"""

    unit_id: str
    material_id: str
    kind: str
    text: str
    locator: Locator
    parent_id: str | None = None
    attrs: dict[str, str] = Field(default_factory=dict)


class SourceRelation(GWModel):
    """正文—附件—表格注释之间的关系，例如表格脚注限定统计口径。"""

    kind: str = Field(description="footnote_of/attachment_of/note_of/caption_of/header_of")
    src: str
    dst: str
    note: str = ""


class TableData(GWModel):
    table_id: str
    material_id: str
    title: str = ""
    header: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    locator_prefix: str = ""
    notes: list[str] = Field(default_factory=list, description="表注，如“仅统计已验收项目”")


class SourceBundle(GWModel):
    materials: list[Material] = Field(default_factory=list)
    units: list[SourceUnit] = Field(default_factory=list)
    tables: list[TableData] = Field(default_factory=list)
    relations: list[SourceRelation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def unit(self, unit_id: str) -> SourceUnit | None:
        for u in self.units:
            if u.unit_id == unit_id:
                return u
        return None

    def text_of(self, material_id: str) -> str:
        return "\n".join(u.text for u in self.units if u.material_id == material_id and u.kind != "comment")

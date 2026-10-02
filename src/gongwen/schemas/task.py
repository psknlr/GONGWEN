"""阶段1：办文任务契约 TaskSpec（技能1 任务建模的输出）。"""

from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import Field

from .common import GWModel, Slot, slot


class TaskLayer(str, Enum):
    """设计 §1.1 三种写作任务层级。"""

    FORMAL = "正式公文拟制"
    AFFAIRS = "公务事务材料写作"
    PROCEDURAL = "专门程序性文件"


class Purpose(str, Enum):
    """办文意图：希望受文对象知悉、执行、批准还是反馈（设计 §2.1 意图层）。"""

    APPROVE = "请求批准"
    INSTRUCT = "请求指示"
    REPORT = "汇报情况"
    REPLY_UPPER = "答复上级询问"
    EXECUTE = "部署执行"
    INFORM = "告知周知"
    NEGOTIATE = "商洽工作"
    INQUIRE = "询问答复"
    REPLY_LOWER = "答复下级请示"
    RECORD = "记载会议"
    COMMEND = "表彰批评通报"
    ISSUE_PLAN = "印发方案制度"
    ANNOUNCE = "公开宣布"
    DECIDE = "决策部署"
    OPINION = "提出见解办法"
    PLAN = "制订工作方案"
    SPEECH = "讲话稿"
    SUMMARY = "工作总结"
    RESEARCH = "调研报告"
    BRIEFING = "汇报材料"


class Direction(str, Enum):
    UP = "上行文"
    DOWN = "下行文"
    PARALLEL = "平行文"
    PUBLIC = "公布性"
    MEETING = "会议文书"
    INTERNAL = "内部材料"
    UNKNOWN = "待核实"


class GapImpact(str, Enum):
    """缺口影响程度：只有影响文种、权限或重要事实的缺口才需要向用户提问。"""

    GENRE = "影响文种"
    AUTHORITY = "影响权限"
    KEY_FACT = "影响重要事实"
    MINOR = "不影响当前阶段"


class Gap(GWModel):
    field: str
    description: str
    impact: GapImpact
    ask_user: bool = False
    question: str = ""
    searched_materials: bool = Field(default=False, description="提问前是否已检索已有材料")


class Organ(GWModel):
    """机关/单位。type 用于行文关系与权限判断。"""

    name: str
    type: str = Field(default="未知", description="党委/政府/政府部门/部门内设机构/人大/政协/事业单位/医院/高校/科研机构/企业/办公厅（室）/未知")
    level: str = Field(default="未知", description="中央/省/市/县/乡/单位内部/未知")
    parent: str | None = None
    short_name: str | None = None


class TaskSpec(GWModel):
    task_id: str
    request_text: str
    layer: Slot = Field(default_factory=lambda: slot(TaskLayer.FORMAL.value, "推断"))
    purposes: list[str] = Field(default_factory=list, description="Purpose 值列表")
    issuer: Slot = Field(default_factory=slot, description="发文主体 Organ")
    recipients: Slot = Field(default_factory=slot, description="主送 list[Organ]")
    cc: Slot = Field(default_factory=slot, description="抄送 list[Organ]")
    relation: Slot = Field(default_factory=slot, description="行文关系 Direction")
    requested_genre: str | None = Field(default=None, description="用户字面要求的文种/材料类型")
    suggested_genre: Slot = Field(default_factory=slot)
    subject: Slot = Field(default_factory=slot, description="事由")
    policy_as_of: date = Field(default_factory=date.today, description="政策适用时点")
    policy_mode: str = Field(default="current", description="current=按当前有效政策；historical=按指定时点政策背景")
    region: Slot = Field(default_factory=slot, description="适用地域")
    material_ids: list[str] = Field(default_factory=list, description="已准入材料清单")
    approved_item_refs: list[str] = Field(default_factory=list, description="仅引用真实批准记录 ID")
    proposed_items: list[str] = Field(default_factory=list, description="拟议事项，须与已批准事项分开")
    forbidden_fill: list[str] = Field(
        default_factory=lambda: ["发文字号", "成文日期", "签发人", "审批结论", "印发日期", "份号", "印章"],
        description="禁止补写的字段：正式值只能来自真实办理流程",
    )
    resource_mentions: list[str] = Field(default_factory=list, description="涉及经费、编制、用地等资源")
    gaps: list[Gap] = Field(default_factory=list)
    target_output: str = Field(default="供人工审阅的草稿")
    constraints: dict[str, str] = Field(default_factory=dict)
    unit_profile: str = "party_gov"
    notes: list[str] = Field(default_factory=list)

    def blocking_gaps(self) -> list[Gap]:
        return [g for g in self.gaps if g.ask_user]

"""通用枚举与基础结构。

所有阶段产物都由 Pydantic 模型承载，便于：
1. 作为技能的输入输出契约（Agent Skills 中的 Schema 由此生成）；
2. 在会话日志中以内容哈希引用，满足“可重建、可追溯”；
3. 让确定性校验器与模型输出校验共用同一套结构。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def today() -> date:
    return date.today()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def stable_hash(obj: Any) -> str:
    """对任意可 JSON 化对象计算稳定哈希（键排序）。"""
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return sha256_text(payload)


class GWModel(BaseModel):
    """项目内所有结构化对象的基类。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=False)

    def digest(self) -> str:
        return stable_hash(self)


class Severity(str, Enum):
    """问题严重程度（设计 §5 阶段7 问题报告格式）。"""

    BLOCKING = "阻断送审"
    MAJOR = "重要"
    MINOR = "一般"
    INFO = "提示"

    @property
    def rank(self) -> int:
        return {"阻断送审": 3, "重要": 2, "一般": 1, "提示": 0}[self.value]


class RuleLevel(str, Enum):
    """规则来源层级。不得把“实务”“待核”表述为“国标/条例规定”。"""

    LAW = "法律法规"
    REGULATION = "条例"  # 《党政机关公文处理工作条例》等党内法规、行政法规
    NATIONAL_STANDARD = "国标"  # GB/T 9704—2012
    STANDARD = "标准"  # GB/T 15834、GB/T 15835 等
    POLICY = "政策文件"
    PRACTICE = "实务"
    UNIT = "单位制度"
    PENDING = "待核"
    OBSOLETE = "旧规"


class Clearance(str, Enum):
    """材料属性（准入分级）。涉密材料一律不进入本系统。"""

    PUBLIC = "公开"
    INTERNAL = "内部"
    SENSITIVE = "敏感"
    WORK_SECRET = "工作秘密"
    CLASSIFIED = "涉密"
    UNKNOWN = "未确认"

    @property
    def rank(self) -> int:
        return {
            "公开": 0,
            "内部": 1,
            "敏感": 2,
            "工作秘密": 3,
            "涉密": 9,
            "未确认": 8,
        }[self.value]


class AdmissionDecision(str, Enum):
    ALLOW = "允许处理"
    NEED_CONFIRM = "需人工确认"
    FORBID = "禁止进入当前环境"


class EnvironmentRoute(str, Enum):
    """设计 §7.1 三条产品路线。"""

    PUBLIC_DEV = "public_dev"  # 公开材料研发版
    UNIT_APPROVED = "unit_approved"  # 单位批准的业务环境
    CLASSIFIED = "classified"  # 涉密应用：不在本系统能力承诺范围内


class DocStatus(str, Enum):
    """文稿状态（设计 §1.2）。APPROVED_FOR_ISSUE 只能由真实审批记录绑定形成。"""

    DISCUSSION = "讨论稿"
    SUBMISSION = "送审稿"
    APPROVED_FOR_ISSUE = "经批准的待印发版本"


class Locator(GWModel):
    """原文定位：段落、页码、单元格、时间段等。"""

    material_id: str
    kind: str = Field(description="paragraph/heading/table_cell/sheet_cell/page/comment/footnote/line/time")
    path: str = Field(description="如 p12、page3、Sheet1!B5、table2.r3.c4、00:12:30-00:13:10")
    excerpt: str = ""

    def label(self) -> str:
        return f"{self.material_id}#{self.path}"


class EvidenceRef(GWModel):
    """文稿表述到证据对象的引用。"""

    kind: str = Field(description="fact/policy/task/material/approval/measure")
    id: str
    note: str = ""


class Slot(GWModel):
    """带确认状态的字段值：避免把推断值伪装成已确认值。"""

    value: Any = None
    status: str = Field(default="待确认", description="已确认/材料记载/推断/待确认/缺失")
    source: str = ""

    @property
    def known(self) -> bool:
        return self.value not in (None, "", []) and self.status != "缺失"


def slot(value: Any = None, status: str | None = None, source: str = "") -> Slot:
    if status is None:
        status = "待确认" if value not in (None, "", []) else "缺失"
    return Slot(value=value, status=status, source=source)


class IdAllocator(GWModel):
    """任务内顺序编号，保证编号可复现（便于审阅时口头引用，如 R-023）。"""

    counters: dict[str, int] = Field(default_factory=dict)

    def next(self, prefix: str, width: int = 3, sep: str = "-") -> str:
        n = self.counters.get(prefix, 0) + 1
        self.counters[prefix] = n
        return f"{prefix}{sep}{n:0{width}d}"

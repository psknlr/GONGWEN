"""阶段7-8：一致性检查与独立审校的问题报告（技能8、技能9 的输出）。"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from .common import EvidenceRef, GWModel, Severity
from .genre import RuleCitation


class IssueType(str, Enum):
    # 事实与证据
    STATUS_UPGRADE = "事实状态升级"
    UNSOURCED_NUMBER = "无来源数字"
    FACT_MISMATCH = "与材料不一致"
    CALC_ERROR = "计算错误"
    CALIBER_MISMATCH = "统计口径不一致"
    EXAMPLE_AS_FACT = "示例数据当作事实"
    CONFLICT_USED = "使用了存在冲突的事实"
    # 依据
    CITATION_MISSING = "引用依据不存在"
    CITATION_NOT_APPLICABLE = "依据不适用（时点/地域/主体）"
    CITATION_UNSUPPORTED = "依据不支持该表述"
    CITATION_FORMAT = "引用格式不规范"
    # 文种与行文
    GENRE_MISMATCH = "文种不当"
    MIXED_REQUEST = "报告夹带请示事项"
    MULTI_MATTER = "请示未一文一事"
    ROUTING = "行文关系问题"
    AUTHORITY = "超出权限"
    CLOSING_MISMATCH = "结束语与文种或方向不符"
    ADDRESS_MISMATCH = "称谓与行文方向不符"
    PROCEDURE = "需衔接专门程序"
    # 语义强度
    OBLIGATION_CHANGE = "义务强度变化"
    SCOPE_EXPANSION = "实施范围扩大"
    EXCEPTION_REMOVED = "条件或例外被删除"
    DECISION_UPGRADE = "讨论升级为决定"
    NEW_COMMITMENT = "擅自新增承诺或任务"
    # 必要性与负担
    BURDEN = "新增报送或考核负担"
    NECESSITY = "发文必要性存疑"
    # 表达
    EMPTY_PHRASE = "空泛表述"
    PARAGRAPH_FUNCTION = "段落缺少明确功能"
    RELATIVE_TIME = "相对时间表述"
    PUNCTUATION = "标点用法"
    NUMBER_USAGE = "数字用法"
    NUMBERING = "层次序数"
    WORDING = "字词规范"
    # 结构与格式
    REQUIRED_MISSING = "必填要素缺失"
    PLACEHOLDER = "存在待补占位"
    ATTACHMENT_MISMATCH = "附件说明与附件不对应"
    CROSS_REF = "交叉引用错误"
    FORMAT = "格式规范"
    # 安全
    INJECTION = "疑似提示词注入"
    SENSITIVE = "敏感信息"


class IssueLocation(GWModel):
    doc_id: str
    block_id: str | None = None
    sentence_id: str | None = None
    span: tuple[int, int] | None = None
    label: str = Field(default="", description="人类可读位置，如 第二部分第三段")
    field: str | None = Field(default=None, description="非正文要素，如 title、recipients、header.doc_number")


class ReviewIssue(GWModel):
    issue_id: str
    location: IssueLocation
    type: IssueType
    severity: Severity
    original: str = ""
    evidence: list[EvidenceRef] = Field(default_factory=list)
    evidence_text: str = Field(default="", description="对应材料原文摘录")
    suggestion: str = ""
    impact: list[str] = Field(default_factory=list, description="影响范围：正文、摘要、附件进度表等")
    rule: RuleCitation | None = None
    channel: str = Field(default="deterministic", description="deterministic/semantic/model/human")
    status: str = Field(default="open", description="open/resolved/accepted_risk/rejected")
    needs_human: bool = False
    auto_fixable: bool = False
    fix_hint: dict[str, str] = Field(default_factory=dict, description="确定性修订提示，如 replace/with")

    def render(self) -> str:
        lines = [
            f"问题编号：{self.issue_id}",
            f"位置：{self.location.label or self.location.field or self.location.block_id}",
            f"类型：{self.type.value}",
            f"严重程度：{self.severity.value}",
        ]
        if self.original:
            lines.append(f"原文：“{self.original}”")
        if self.evidence_text:
            lines.append(f"对应材料：“{self.evidence_text}”")
        if self.suggestion:
            lines.append(f"建议：{self.suggestion}")
        if self.impact:
            lines.append(f"影响范围：{'、'.join(self.impact)}")
        if self.rule:
            lines.append(f"依据：{self.rule.source}（{self.rule.level.value}）")
        return "\n".join(lines)


class ConsistencyFinding(GWModel):
    finding_id: str
    kind: str = Field(description="数值不一致/状态不一致/口径不一致/编号不一致/附件不对应/交叉引用")
    key: str
    occurrences: list[dict[str, str]] = Field(default_factory=list, description="[{doc_id, location, value}]")
    message: str
    severity: Severity = Severity.MAJOR


class ConsistencyReport(GWModel):
    findings: list[ConsistencyFinding] = Field(default_factory=list)
    checked: list[str] = Field(default_factory=list, description="已检查的对象对，如 正文↔附件1")


class ReviewReport(GWModel):
    round: int = 0
    doc_id: str
    doc_version: int
    issues: list[ReviewIssue] = Field(default_factory=list)
    checks_run: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    independent_context: bool = Field(default=True, description="审校通道是否使用独立上下文重新读取证据")

    def open_issues(self, min_severity: Severity = Severity.INFO) -> list[ReviewIssue]:
        return [i for i in self.issues if i.status == "open" and i.severity.rank >= min_severity.rank]

    def blocking(self) -> list[ReviewIssue]:
        return self.open_issues(Severity.BLOCKING)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for i in self.issues:
            if i.status == "open":
                out[i.severity.value] = out.get(i.severity.value, 0) + 1
        return out

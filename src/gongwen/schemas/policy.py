"""知识与证据层：权威依据记录、适用性判断与证据包 PolicyPack（技能4 的输出）。

关键区分（设计 §4.3）：
* 文件何时有效（effective_date / expiry_date / repeal_date）
* 系统何时获得该文件（fetched_at）
二者不能混用；网页转载日期不能当作施行日期。
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field

from .common import GWModel, RuleLevel


class PolicyArticle(GWModel):
    article_no: str = Field(description="条款号，如 第十五条、7.3.5.1")
    text: str
    chapter: str = ""
    tags: list[str] = Field(default_factory=list)
    verbatim: bool = Field(default=True, description="是否为逐字原文；False 表示要点转写")
    verification: str = Field(default="待核", description="已核（多源一致）/已核（单源）/待核")


class PolicyDocument(GWModel):
    policy_id: str
    title: str
    issuers: list[str] = Field(default_factory=list)
    doc_number: str | None = None
    level: RuleLevel = RuleLevel.POLICY
    publish_date: date | None = None
    effective_date: date | None = None
    expiry_date: date | None = None
    repeal_date: date | None = None
    status: str = Field(default="现行有效", description="现行有效/已修订/已废止/失效/尚未施行/待核")
    regions: list[str] = Field(default_factory=lambda: ["全国"])
    subjects: list[str] = Field(default_factory=lambda: ["党政机关"], description="适用主体")
    subject_mode: str = Field(default="依照", description="依照/参照：其他机关和单位参照执行时不能视为同等权限")
    matters: list[str] = Field(default_factory=list, description="事项范围标签")
    version: str = "1"
    supersedes: list[str] = Field(default_factory=list)
    superseded_by: list[str] = Field(default_factory=list)
    source_url: str | None = None
    fetched_at: datetime | None = Field(default=None, description="系统获得该文件的时间，不等于施行日期")
    content_hash: str | None = None
    verification: str = Field(default="待核", description="人工核验状态")
    synthetic: bool = Field(default=False, description="示例/合成数据，不得作为真实依据")
    basis_role: str = Field(
        default="substantive",
        description="substantive=可作为事项的实体依据；procedural=约束行文、格式、程序与安全的规范，一般不在正文中作为实体依据引用",
    )
    articles: list[PolicyArticle] = Field(default_factory=list)
    notes: str = ""

    def article(self, no: str) -> PolicyArticle | None:
        for a in self.articles:
            if a.article_no == no:
                return a
        return None

    def cite(self, article_no: str | None = None) -> str:
        head = f"《{self.title}》"
        if self.doc_number:
            head += f"（{self.doc_number}）"
        if article_no:
            head += article_no
        return head


class Applicability(GWModel):
    applicable: bool | None = Field(description="None=无法判断，需人工")
    as_of: date
    reasons: list[str] = Field(default_factory=list)
    temporal_ok: bool | None = None
    region_ok: bool | None = None
    subject_ok: bool | None = None
    matter_ok: bool | None = None


class PolicyEvidence(GWModel):
    evidence_id: str
    policy_id: str
    article_no: str | None = None
    quote: str
    citation: str
    applicability: Applicability
    retrieval: str = Field(description="exact_docno/exact_title/bm25/semantic/manual")
    score: float = 0.0
    role: str = Field(default="support", description="support/constraint/exception/counter")
    supports_claims: list[str] = Field(default_factory=list)
    support_status: str = Field(default="未判定", description="支持/部分支持/不支持/未判定")
    version: str = "1"
    verification: str = "待核"


class PolicyConflict(GWModel):
    policies: list[str]
    point: str
    conditions: str = ""
    route_to_human: bool = True


class PolicyPack(GWModel):
    query_terms: list[str] = Field(default_factory=list)
    as_of: date
    region: str | None = None
    subject_type: str | None = None
    items: list[PolicyEvidence] = Field(default_factory=list)
    excluded: list[PolicyEvidence] = Field(default_factory=list, description="检索到但不适用的依据（附理由）")
    conflicts: list[PolicyConflict] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)

    def get(self, evidence_id: str) -> PolicyEvidence | None:
        for e in self.items:
            if e.evidence_id == evidence_id:
                return e
        return None

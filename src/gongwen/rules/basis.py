"""依据检查：引用是否存在、是否在给定时点/地域/主体下适用、是否真正支持具体表述。

“有引用”与“引用真正支持内容”分别检查（借鉴 DeepTRACE 等研究的声明级分析思路）。
确定性初判使用词元覆盖率；有模型时由独立审校通道复核支持关系。
"""

from __future__ import annotations

import re

from ..knowledge.retrieval import coverage, find_doc_numbers, find_titles
from ..schemas.common import EvidenceRef, Severity
from ..schemas.review import IssueType, ReviewIssue
from .base import CheckContext

_CITE_ORDER_BAD = re.compile(r"[〔\[]\d{4}[〕\]]\d+号\s*《")
_CITE_RE = re.compile(r"《([^《》]+)》\s*(（([^（）]*〔\d{4}〕\d+号|[^（）]*第\d+号)）)?")
SUPPORT_THRESHOLD = 0.30
_BASIS_LEAD = re.compile(r"(根据|依据|按照|依照|遵照|贯彻落实|落实)\s*《([^《》]+)》")


def _about(title: str, p) -> bool:
    """文稿本身是否就是关于该规范所管事项的（如“关于规范公文处理工作的通知”可以引用条例）。"""
    keys = {k for m in p.matters for k in (m, m[:2]) if k} | {"公文", "文件", "格式", "发文"}
    return any(k in (title or "") for k in keys)


def _claim_text(sentence: str) -> str:
    t = _CITE_RE.sub("", sentence)
    t = re.sub(r"^(根据|依据|按照|依照|参照|为贯彻落实|为落实|按|据)[，,]?", "", t)
    return t


def check_citations(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    lib = ctx.policy_library
    as_of = ctx.task.policy_as_of if ctx.task else None
    region = None
    if ctx.task and ctx.task.region.known:
        region = str(ctx.task.region.value)
    subject_types = ctx.profile.get("subject_types") if ctx.profile else None
    for b, s in ctx.ir.iter_sentences():
        if _CITE_ORDER_BAD.search(s.text):
            out.append(ctx.issue("GW-BASIS-004", IssueType.CITATION_FORMAT, "引用公文应先引标题、后引发文字号，如《××关于××的通知》（××〔2026〕5号）", block=b, sentence=s))
        policy_refs = [r for r in s.refs if r.kind == "policy"]
        titles = find_titles(s.text)
        numbers = find_doc_numbers(s.text)
        if lib is None:
            continue
        cited = []
        for t in titles:
            p = lib.lookup_title(t)
            if p is None:
                # 未入库的引用：无法核验是否存在
                if any(k in t for k in ("条例", "办法", "规定", "意见", "通知", "法", "决定", "指引", "规划", "方案", "细则", "标准")):
                    out.append(
                        ctx.issue(
                            "GW-BASIS-001",
                            IssueType.CITATION_MISSING,
                            f"引用的《{t}》不在依据库中，无法核验其是否存在、是否现行有效。请补充原文与版本信息后再引用",
                            block=b,
                            sentence=s,
                            needs_human=True,
                        )
                    )
                continue
            cited.append(p)
        for n in numbers:
            p = lib.lookup_number(n)
            if p is None:
                out.append(ctx.issue("GW-BASIS-001", IssueType.CITATION_MISSING, f"发文字号“{n}”在依据库中查无对应文件，请核对", block=b, sentence=s, needs_human=True))
            elif p not in cited:
                cited.append(p)
        for r in policy_refs:
            ev = ctx.policies.get(r.id) if ctx.policies else None
            if ev is not None:
                p = lib.get(ev.policy_id)
                if p and p not in cited:
                    cited.append(p)
        basis_titles = {m.group(2) for m in _BASIS_LEAD.finditer(s.text)}
        if basis_titles:
            for p in cited:
                if p.title not in basis_titles and not any(lib.lookup_title(t) is p for t in basis_titles):
                    continue
                if p.basis_role == "procedural" and p.status == "现行有效" and not _about(ctx.ir.title, p):
                    out.append(
                        ctx.issue(
                            "GW-BASIS-005",
                            IssueType.CITATION_UNSUPPORTED,
                            f"{p.cite()}规范的是公文处理、格式或程序等事项，一般不作为“{ctx.ir.title or '本事项'}”的实体依据；请改引与事项内容直接相关的政策文件，或删去该依据",
                            block=b,
                            sentence=s,
                            evidence=[EvidenceRef(kind="policy", id=p.policy_id)],
                            needs_human=True,
                        )
                    )
        if not ctx.features.temporal_check or as_of is None:
            continue
        for p in cited:
            ap = lib.applicability(p, as_of, region, subject_types)
            ev = [EvidenceRef(kind="policy", id=p.policy_id)]
            if ap.applicable is False:
                out.append(
                    ctx.issue(
                        "GW-BASIS-002",
                        IssueType.CITATION_NOT_APPLICABLE,
                        f"{p.cite()}在适用时点 {as_of} 不适用：{'；'.join(ap.reasons)}",
                        block=b,
                        sentence=s,
                        evidence=ev,
                        needs_human=True,
                    )
                )
            elif ap.applicable is None:
                out.append(
                    ctx.issue(
                        "GW-BASIS-002",
                        IssueType.CITATION_NOT_APPLICABLE,
                        f"{p.cite()}的适用性需人工确认：{'；'.join(ap.reasons)}",
                        block=b,
                        sentence=s,
                        evidence=ev,
                        severity=Severity.MAJOR,
                        needs_human=True,
                    )
                )
            if p.verification.startswith("待核"):
                out.append(ctx.issue("GW-BASIS-001", IssueType.CITATION_MISSING, f"{p.cite()}在依据库中的核验状态为“待核”，送审前须人工核对原文", block=b, sentence=s, evidence=ev, severity=Severity.MAJOR, needs_human=True))
    return out


def check_support(ctx: CheckContext) -> list[ReviewIssue]:
    """引用存在 ≠ 引用支持该表述：比对声明与所引条款原文。"""
    out: list[ReviewIssue] = []
    if not ctx.policies:
        return out
    for b, s in ctx.ir.iter_sentences():
        refs = [r for r in s.refs if r.kind == "policy"]
        if not refs:
            continue
        claim = _claim_text(s.text)
        if len(claim) < 6:
            continue
        best = 0.0
        best_ev = None
        for r in refs:
            ev = ctx.policies.get(r.id)
            if ev is None:
                continue
            cov = coverage(claim, ev.quote)
            if cov > best:
                best, best_ev = cov, ev
        if best_ev is not None and best < SUPPORT_THRESHOLD and s.function not in ("依据", "背景"):
            out.append(
                ctx.issue(
                    "GW-BASIS-003",
                    IssueType.CITATION_UNSUPPORTED,
                    f"所引{best_ev.citation}与该表述的内容重合度低（{best:.0%}），引用可能不支持具体表述。请核对条款原文",
                    block=b,
                    sentence=s,
                    evidence=[EvidenceRef(kind="policy", id=best_ev.evidence_id)],
                    evidence_text=best_ev.quote[:200],
                    needs_human=True,
                )
            )
    return out


CHECKERS = [check_citations, check_support]

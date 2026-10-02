"""事实状态与语义强度检查（设计 §6.2“语义强度保持”）。

把每项措施表示为：主体—行为—对象—条件—时限—义务强度—例外—依据，
修订前后比较这一结构，比只计算文本相似度更贴近公文风险。
语义结构的自动抽取也可能出错，因此关键变化一律呈现给审核人确认，而不是机械判定违法。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..knowledge import kb
from ..schemas.common import EvidenceRef, Severity
from ..schemas.facts import Fact, FactStatus, Progress
from ..schemas.patch import SemanticChange
from ..schemas.review import IssueType, ReviewIssue
from .base import CheckContext
from .textutil import DATE_RE, extract_numbers, same_quantity

APPROVAL_CLAIM = re.compile(r"(经[^，。；]{0,20}(批准|同意|审定|审议通过))|(已(获|经)?(批准|批复|同意|立项))|(研究决定)|(批准同意)")
VERIFY_CLAIM = re.compile(r"(经(核实|核查|审核确认|审计确认))|(经核定)")
AGGREGATE = re.compile(r"(共计|合计|总计|累计|总共|共有|总数)")


@dataclass
class SemFeatures:
    obligation: float = 0.0
    obligation_words: list[str] = field(default_factory=list)
    progress: Progress = Progress.NONE
    scope: str = ""  # narrow/broad/""
    decision: str = ""  # discussion/decided/""
    hedges: set[str] = field(default_factory=set)
    dates: set[str] = field(default_factory=set)


_REQUIREMENT_CUES = ("确保", "要", "应当", "应", "须", "必须", "务必", "力争", "争取", "推动", "目标")


_DONE_MARK = re.compile(r"(已经|业已|已)(经)?(新建|建成|完成|开展|实施|启动|投入|建设|落实|实现|新增|安排|拨付|下达|批准|同意|印发|出台|竣工|验收)")


def done_marks(text: str) -> set[str]:
    """句中明确的“已……”完成标记（按小句判断，避免被同句中的“计划于”掩盖）。"""
    return {m.group(0) for m in _DONE_MARK.finditer(text)}


def progress_of(text: str) -> Progress:
    """判断句子对事项进展的陈述：拟议 / 推进中 / 已完成 / 不涉及。

    “确保按时完成”“要实现”属于要求而非完成陈述；只有出现“已/已经”等明确标记，
    或在没有要求性语境时出现“建成”“完成”等完成动词，才视为“已完成”。
    """
    lex = kb.lexicon()["progress"]
    if any(w in text for w in lex["planned"]):
        return Progress.PLANNED
    explicit = any(w in text for w in ("已", "已经", "业已"))
    done_verbs = any(w in text for w in lex["done"] if not w.startswith("已"))
    requirement = any(w in text for w in _REQUIREMENT_CUES)
    if explicit or (done_verbs and not requirement):
        return Progress.COMPLETED
    if any(w in text for w in lex["ongoing"]):
        return Progress.ONGOING
    return Progress.NONE


def features(text: str) -> SemFeatures:
    lex = kb.lexicon()
    f = SemFeatures()
    words = kb.obligation_pattern().findall(text)
    # “不得”“禁止”等禁止性用语也计入义务强度
    if words:
        f.obligation_words = words
        f.obligation = max(kb.obligation_strength(w) for w in words)
    f.progress = progress_of(text)
    if any(w in text for w in lex["scope"]["broad"]):
        f.scope = "broad"
    elif any(w in text for w in lex["scope"]["narrow"]):
        f.scope = "narrow"
    if any(w in text for w in lex["decision"]["decided"]):
        f.decision = "decided"
    elif any(w in text for w in lex["decision"]["discussion"]):
        f.decision = "discussion"
    f.hedges = {h for h in lex["hedges"] if h in text}
    if re.search(r"除[^，。；]{1,30}外", text):
        f.hedges.add("除……外")
    f.dates = {m.group(0) for m in DATE_RE.finditer(text)}
    return f


def semantic_diff(before: str, after: str) -> list[SemanticChange]:
    a, b = features(before), features(after)
    out: list[SemanticChange] = []
    if a.obligation_words or b.obligation_words:
        if abs(a.obligation - b.obligation) >= 0.5:
            out.append(
                SemanticChange(
                    dimension="义务强度",
                    before="、".join(a.obligation_words) or "（无）",
                    after="、".join(b.obligation_words) or "（无）",
                    direction="增强" if b.obligation > a.obligation else "减弱",
                )
            )
    if a.progress != b.progress and Progress.NONE not in (a.progress, b.progress):
        out.append(
            SemanticChange(
                dimension="事实状态",
                before=a.progress.value,
                after=b.progress.value,
                direction="升级" if b.progress.rank > a.progress.rank else "降级",
                severity="阻断送审" if b.progress.rank > a.progress.rank else "重要",
            )
        )
    elif a.progress == Progress.PLANNED and b.progress == Progress.NONE and progress_of(after) != Progress.PLANNED:
        # 删除“拟”字但未出现完成词：语义由拟议变为陈述，需确认
        out.append(SemanticChange(dimension="事实状态", before=a.progress.value, after="陈述（未标明拟议）", direction="升级", severity="重要"))
    if a.scope == "narrow" and b.scope == "broad":
        out.append(SemanticChange(dimension="实施范围", before="试点/部分", after="全面/全部", direction="扩大"))
    if a.decision == "discussion" and b.decision == "decided":
        out.append(SemanticChange(dimension="决策状态", before="讨论/建议", after="决定/议定", direction="升级", severity="阻断送审"))
    lost = a.hedges - b.hedges
    if lost:
        out.append(SemanticChange(dimension="条件与例外", before="、".join(sorted(lost)), after="（删除）", direction="删除"))
    if a.dates and b.dates and a.dates != b.dates:
        out.append(SemanticChange(dimension="时限", before="、".join(sorted(a.dates)), after="、".join(sorted(b.dates)), direction="改变"))
    return out


# --------------------------------------------------------------------- 检查器
def _facts_for(ctx: CheckContext, s) -> list[Fact]:
    if not ctx.ledger:
        return []
    out = []
    for r in s.refs:
        if r.kind == "fact":
            f = ctx.ledger.get(r.id)
            if f:
                out.append(f)
    return out


def _source_excerpt(f: Fact) -> str:
    return f.sources[0].excerpt if f.sources else f.statement


def check_fact_status(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    if not ctx.ledger or not ctx.features.fact_ledger:
        return out
    for b, s in ctx.ir.iter_sentences():
        p = progress_of(s.text)
        for f in _facts_for(ctx, s):
            ev = [EvidenceRef(kind="fact", id=f.fact_id)]
            if "example" in f.tags:
                out.append(ctx.issue("GW-FACT-005", IssueType.EXAMPLE_AS_FACT, f"{f.fact_id} 来自示例/模板数据，不能作为本次事项事实", block=b, sentence=s, evidence=ev, evidence_text=_source_excerpt(f)))
                continue
            if f.status in (FactStatus.UNKNOWN, FactStatus.CONFLICT):
                out.append(ctx.issue("GW-FACT-006", IssueType.CONFLICT_USED, f"{f.fact_id} 状态为“{f.status.value}”，未核清前不得使用", block=b, sentence=s, evidence=ev, evidence_text=_source_excerpt(f), needs_human=True))
                continue
            planned = f.status == FactStatus.PROPOSED or f.progress == Progress.PLANNED
            new_done = done_marks(s.text) - done_marks(f.statement)
            if planned and (p in (Progress.COMPLETED, Progress.ONGOING) or new_done):
                out.append(
                    ctx.issue(
                        "GW-FACT-001",
                        IssueType.STATUS_UPGRADE,
                        "材料中为拟议事项，文稿写成了已开展/已完成。请按拟议语义表述（如“拟……”），未核清前不得写成已完成",
                        block=b,
                        sentence=s,
                        evidence=ev,
                        evidence_text=_source_excerpt(f),
                        impact=["正文", "摘要", "附件进度表"],
                        auto_fixable=True,
                        needs_human=s.origin == "human",
                        fix_hint={"op": "downgrade_progress", "fact": f.fact_id},
                    )
                )
            elif f.progress == Progress.ONGOING and p == Progress.COMPLETED:
                out.append(
                    ctx.issue(
                        "GW-FACT-001",
                        IssueType.STATUS_UPGRADE,
                        "材料显示该事项仍在推进中，文稿写成已完成",
                        block=b,
                        sentence=s,
                        evidence=ev,
                        evidence_text=_source_excerpt(f),
                        impact=["正文", "摘要", "附件进度表"],
                        auto_fixable=True,
                        fix_hint={"op": "downgrade_progress", "fact": f.fact_id},
                    )
                )
            if f.status == FactStatus.RECORDED and VERIFY_CLAIM.search(s.text):
                out.append(ctx.issue("GW-FACT-001", IssueType.STATUS_UPGRADE, f"{f.fact_id} 为“材料记载”，未经独立核实，不能表述为“经核实”", block=b, sentence=s, evidence=ev, evidence_text=_source_excerpt(f), severity=Severity.MAJOR))
    return out


def check_approval_claims(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    if ctx.ir.genre == "批复":
        return out
    for b, s in ctx.ir.iter_sentences():
        m = APPROVAL_CLAIM.search(s.text)
        if not m:
            continue
        approved = [f for f in _facts_for(ctx, s) if f.status == FactStatus.APPROVED and f.approval_ref]
        if not approved:
            out.append(
                ctx.issue(
                    "GW-SEM-006",
                    IssueType.STATUS_UPGRADE,
                    f"出现“{m.group(0)}”，但未绑定真实批准记录。已批准事项须引用真实审批记录，拟议内容不得写成已批准",
                    block=b,
                    sentence=s,
                    needs_human=True,
                )
            )
    return out


def _sourced(ctx: CheckContext, mention, refs_facts: list[Fact]) -> tuple[bool, Fact | None]:
    pools = list(refs_facts)
    if ctx.ledger:
        pools += [f for f in ctx.ledger.facts if f not in refs_facts]
    for f in pools:
        if isinstance(f.value, (int, float)) and same_quantity(mention, float(f.value), f.unit):
            return True, f
    # 依据原文中出现的数字（如引用条款中的期限）
    if ctx.policies:
        for e in ctx.policies.items:
            if mention.raw.replace(" ", "") in e.quote:
                return True, None
    if ctx.task and mention.raw.replace(" ", "") in ctx.task.request_text:
        return True, None
    return False, None


def check_numbers_sourced(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    for b, s in ctx.ir.iter_sentences():
        if s.has_placeholder and not extract_numbers(s.text):
            continue
        facts = _facts_for(ctx, s)
        for m in extract_numbers(s.text):
            ok, f = _sourced(ctx, m, facts)
            if not ok:
                out.append(
                    ctx.issue(
                        "GW-FACT-002",
                        IssueType.UNSOURCED_NUMBER,
                        f"数字“{m.raw}”在事实账本、任务说明和依据中均找不到来源。请补充来源或删除，不得以估计数、示例数代替",
                        block=b,
                        sentence=s,
                        severity=Severity.BLOCKING if m.kind == "money" else Severity.MAJOR,
                        span=(m.start, m.end),
                        needs_human=True,
                    )
                )
            elif f is not None and f.status in (FactStatus.CONFLICT, FactStatus.UNKNOWN):
                out.append(ctx.issue("GW-FACT-006", IssueType.CONFLICT_USED, f"数字“{m.raw}”对应的事实 {f.fact_id} 存在冲突或未知", block=b, sentence=s, evidence=[EvidenceRef(kind="fact", id=f.fact_id)], needs_human=True))
            elif f is not None and f not in facts and s.origin != "human":
                # 数字有来源但句子未引用该事实：补充引用便于追溯
                s.refs.append(EvidenceRef(kind="fact", id=f.fact_id, note="auto-linked"))
    return out


def check_caliber(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    for b, s in ctx.ir.iter_sentences():
        if not AGGREGATE.search(s.text):
            continue
        cals = {f.caliber for f in _facts_for(ctx, s) if f.caliber}
        if len(cals) > 1:
            out.append(ctx.issue("GW-FACT-004", IssueType.CALIBER_MISMATCH, f"汇总的数据口径不一致（{'；'.join(sorted(cals))}），不能直接合并", block=b, sentence=s, needs_human=True))
    if ctx.ledger:
        for c in ctx.ledger.calc_checks:
            if not c.ok:
                out.append(
                    ctx.issue(
                        "GW-FACT-003" if c.kind != "caliber" else "GW-FACT-004",
                        IssueType.CALC_ERROR if c.kind != "caliber" else IssueType.CALIBER_MISMATCH,
                        f"材料计算核验未通过：{c.description}（应为 {c.expected}，材料为 {c.actual}）",
                        field_name=c.locator.label() if c.locator else "materials",
                        evidence_text=c.locator.excerpt if c.locator else "",
                        needs_human=True,
                    )
                )
    return out


def check_meeting_decisions(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    if ctx.ir.genre != "纪要":
        return out
    decided_words = kb.lexicon()["decision"]["decided"]
    discussion_words = kb.lexicon()["decision"]["discussion"]
    for b, s in ctx.ir.iter_sentences():
        if not any(w in s.text for w in ("会议决定", "会议议定", "会议同意", "会议明确", "会议要求", "会议确定")):
            continue
        facts = _facts_for(ctx, s)
        if not facts:
            out.append(ctx.issue("GW-SEM-004", IssueType.DECISION_UPGRADE, "该议定事项未关联会议记录来源，请核对是否确已议定", block=b, sentence=s, needs_human=True, severity=Severity.MAJOR))
            continue
        for f in facts:
            src = " ".join(l.excerpt for l in f.sources) + f.statement
            if any(w in src for w in discussion_words) and not any(w in src for w in decided_words):
                out.append(
                    ctx.issue(
                        "GW-SEM-004",
                        IssueType.DECISION_UPGRADE,
                        "会议记录中为讨论或个人建议，纪要写成了会议决定。应写入“主要情况”或“待研究事项”",
                        block=b,
                        sentence=s,
                        evidence=[EvidenceRef(kind="fact", id=f.fact_id)],
                        evidence_text=_source_excerpt(f),
                    )
                )
    return out


def check_measures(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    if not ctx.outline:
        return out
    for b, s in ctx.ir.iter_sentences():
        if not s.measure_id:
            continue
        m = ctx.outline.measure(s.measure_id)
        if not m:
            continue
        ev = [EvidenceRef(kind="measure", id=m.measure_id)]
        if m.origin == "系统建议" and not m.confirmed:
            out.append(ctx.issue("GW-SEM-005", IssueType.NEW_COMMITMENT, "该措施为系统建议，尚未经用户确认，不能作为正式措施写入", block=b, sentence=s, evidence=ev, needs_human=True))
        source_text = m.text or f"{m.obligation}{m.action}{m.obj}"
        for ch in semantic_diff(source_text, s.text):
            rid = {"义务强度": "GW-SEM-001", "实施范围": "GW-SEM-002", "条件与例外": "GW-SEM-003", "决策状态": "GW-SEM-004", "事实状态": "GW-FACT-001", "时限": "GW-SEM-001"}[ch.dimension]
            itype = {
                "义务强度": IssueType.OBLIGATION_CHANGE,
                "实施范围": IssueType.SCOPE_EXPANSION,
                "条件与例外": IssueType.EXCEPTION_REMOVED,
                "决策状态": IssueType.DECISION_UPGRADE,
                "事实状态": IssueType.STATUS_UPGRADE,
                "时限": IssueType.OBLIGATION_CHANGE,
            }[ch.dimension]
            if ch.dimension == "事实状态" and ch.direction == "降级":
                continue
            out.append(
                ctx.issue(
                    rid,
                    itype,
                    f"与措施来源相比，{ch.dimension}发生变化：“{ch.before}”→“{ch.after}”（{ch.direction}）。请确认是否符合发文机关意图",
                    block=b,
                    sentence=s,
                    evidence=ev,
                    evidence_text=source_text,
                    needs_human=True,
                )
            )
    return out


def check_version_drift(ctx: CheckContext) -> list[ReviewIssue]:
    """多轮修改后的语义漂移：与上一版本逐句比较。"""
    out: list[ReviewIssue] = []
    if not ctx.previous:
        return out
    prev = {s.sid: s.text for _, s in ctx.previous.iter_sentences()}
    for b, s in ctx.ir.iter_sentences():
        old = prev.get(s.sid)
        if old is None or old == s.text:
            continue
        for ch in semantic_diff(old, s.text):
            if ch.dimension == "事实状态" and ch.direction == "降级":
                continue
            out.append(
                ctx.issue(
                    "GW-SEM-001" if ch.dimension in ("义务强度", "时限") else ("GW-SEM-002" if ch.dimension == "实施范围" else ("GW-SEM-003" if ch.dimension == "条件与例外" else ("GW-SEM-004" if ch.dimension == "决策状态" else "GW-FACT-001"))),
                    IssueType.OBLIGATION_CHANGE if ch.dimension in ("义务强度", "时限") else IssueType.SCOPE_EXPANSION if ch.dimension == "实施范围" else IssueType.EXCEPTION_REMOVED if ch.dimension == "条件与例外" else IssueType.DECISION_UPGRADE if ch.dimension == "决策状态" else IssueType.STATUS_UPGRADE,
                    f"修订改变了{ch.dimension}：“{ch.before}”→“{ch.after}”（{ch.direction}），需审核人确认",
                    block=b,
                    sentence=s,
                    evidence_text=old,
                    needs_human=True,
                    channel="semantic",
                )
            )
    return out


CHECKERS = [check_fact_status, check_approval_claims, check_numbers_sourced, check_caliber, check_meeting_decisions, check_measures, check_version_drift]

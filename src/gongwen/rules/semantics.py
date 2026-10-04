"""事实状态与语义强度检查（设计 §6.2“语义强度保持”）。

把每项措施表示为：主体—行为—对象—条件—时限—义务强度—例外—依据，
修订前后比较这一结构，比只计算文本相似度更贴近公文风险。
语义结构的自动抽取也可能出错，因此关键变化一律呈现给审核人确认，而不是机械判定违法。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from ..knowledge import kb
from ..schemas.common import EvidenceRef, Severity
from ..schemas.facts import Fact, FactStatus, Progress
from ..schemas.patch import SemanticChange
from ..schemas.review import IssueType, ReviewIssue
from .base import CheckContext
from .textutil import DATE_RE, clause_span, clauses, extract_numbers, mention_of, same_quantity

APPROVAL_CLAIM = re.compile(r"(经[^，。；]{0,20}(批准|同意|审定|审议通过))|(已(获|经)?(批准|批复|同意|立项))|(研究决定)|(批准同意)")
VERIFY_CLAIM = re.compile(r"经(过)?([^，。；]{0,6}?)(认真|逐一|实地)?(核实|核查|查实|核定|核对|审核确认|审计确认)")
AGGREGATE = re.compile(r"(共计|合计|总计|累计|总共|共有|总数)")
_PLACEHOLDER = re.compile(r"【待[^】]*】")


def _claim_text(text: str) -> str:
    """去掉【待补：……】占位：占位中的说明文字（如“须依据真实研究决定”）不是文稿表述。"""
    return _PLACEHOLDER.sub("", text)


@dataclass
class SemFeatures:
    obligation: float = 0.0
    obligation_words: list[str] = field(default_factory=list)
    progress: Progress = Progress.NONE
    scope: str = ""  # narrow/broad/""
    decision: str = ""  # discussion/decided/""
    hedges: set[str] = field(default_factory=set)
    dates: set[str] = field(default_factory=set)
    broad: set[str] = field(default_factory=set)
    narrow: set[str] = field(default_factory=set)


# 要求性语境（“确保”“应当”“要”）；单字“要”“应”须排除“主要”“重要”“应急”“相应”等合成词
_REQUIREMENT_RE = re.compile(
    r"确保|应当|必须|务必|力争|争取|推动|目标|须|(?<![主重需纪摘概提只想紧不])要(?![素点闻害])|(?<![相适响反供对答呼感效理])应(?![急用对])"
)
# 拟议标记；“待”须排除“接待”“对待”“期待”等
_PLANNED_RE = None


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
    if _planned_re().search(text):
        return Progress.PLANNED
    if any(w in text for w in lex["ongoing"] if w.startswith("已")):
        return Progress.ONGOING  # “已启动”是推进中，不是已完成
    explicit = any(w in text for w in ("已", "已经", "业已"))
    done_verbs = any(w in text for w in lex["done"] if not w.startswith("已"))
    requirement = bool(_REQUIREMENT_RE.search(text))
    if explicit or (done_verbs and not requirement):
        return Progress.COMPLETED
    if any(w in text for w in lex["ongoing"]):
        return Progress.ONGOING
    if done_verbs and requirement:
        return Progress.PLANNED  # “确保建成12个”是目标要求，尚未完成
    return Progress.NONE


def _planned_re() -> re.Pattern:
    global _PLANNED_RE
    if _PLANNED_RE is None:
        words = sorted(kb.lexicon()["progress"]["planned"], key=len, reverse=True)
        alts = [r"(?<![接对招期看款优善等])待(?![遇])" if w == "待" else re.escape(w) for w in words]
        _PLANNED_RE = re.compile("|".join(alts))
    return _PLANNED_RE


def progress_at(text: str, pos: int) -> Progress:
    """句中某个数字所在小句的进展：本小句没有进展标记时，承接前面小句的陈述
    （“已建成8个，覆盖5个区”）；主语在前、谓语在后且后一小句没有自己的数字时，取后一小句
    （“示范点12个，拟于2026年建成”）。避免“已建成8个，拟新建12个”整句判为同一状态。"""
    a, z = clause_span(text, pos)
    p = progress_of(text[a:z])
    if p != Progress.NONE:
        return p
    spans = clauses(text)
    for ca, cz in reversed([c for c in spans if c[1] <= a]):
        q = progress_of(text[ca:cz])
        if q != Progress.NONE:
            return q
    nxt = [c for c in spans if c[0] >= z]
    if nxt:
        ca, cz = nxt[0]
        if not [m for m in extract_numbers(text[ca:cz]) if m.kind in ("money", "count", "percent", "measure")]:
            return progress_of(text[ca:cz])
    return Progress.NONE


_MEETING_DECIDED = re.compile(r"会议(研究)?(决定|议定|同意|原则同意|明确|确定|要求|通过|批准)")
_NEGATED_DECISION = re.compile(r"(未|尚未|没有|不|暂不|未能)(作出?|形成|予)?(决定|议定|同意|批准|通过|明确|确定)|(未作决定|未达成一致|待研究|再研究|另行研究|进一步研究|会后研究|暂缓)")


def meeting_decision(text: str) -> str:
    """会议记录语句的决策状态：decided / discussion / ""。

    先看否定与待定（“会议未作决定”“经费来源尚未确定”），再看“会议决定……”；
    其余按先出现者判断：“张某建议进一步明确分工”是建议，不是议定事项。
    """
    lex = kb.lexicon()["decision"]
    if _NEGATED_DECISION.search(text):
        return "discussion"
    if _MEETING_DECIDED.search(text):
        return "decided"
    first = {}
    for kind, words in (("decided", lex["decided"]), ("discussion", lex["discussion"])):
        pos = [text.find(w) for w in words if w in text]
        if pos:
            first[kind] = min(pos)
    if not first:
        return ""
    return min(first, key=first.get)


def obligation_words(text: str) -> list[str]:
    """义务强度词；单字“应”“要”“可”“须”“需”排除合成词（应急、相应、主要、可能、需求等）。"""
    out = []
    for m in kb.obligation_pattern().finditer(text):
        w, a, z = m.group(0), m.start(), m.end()
        prev, nxt = text[a - 1 : a], text[z : z + 1]
        if w == "应" and (prev in "相适响反供对答呼感效理" or nxt in "急用对"):
            continue
        if w == "要" and (prev in "主重需纪摘概提只想紧不" or nxt in "素点闻害"):
            continue
        if w == "可" and (nxt in "能行靠见观信爱口" or prev in "认许宁不"):
            continue
        if w == "需" and nxt in "求":
            continue
        if w == "研究" and prev in "经":
            continue
        out.append(w)
    return out


def features(text: str) -> SemFeatures:
    lex = kb.lexicon()
    f = SemFeatures()
    words = obligation_words(text)
    # “不得”“禁止”等禁止性用语也计入义务强度
    if words:
        f.obligation_words = words
        f.obligation = max(kb.obligation_strength(w) for w in words)
    f.progress = progress_of(text)
    f.broad = {w for w in lex["scope"]["broad"] if w in text}
    f.narrow = {w for w in lex["scope"]["narrow"] if w in text}
    if f.broad:
        f.scope = "broad"
    elif f.narrow:
        f.scope = "narrow"
    f.decision = meeting_decision(text)
    f.hedges = {h for h in lex["hedges"] if h in text}
    if re.search(r"除[^，。；]{1,30}外", text):
        f.hedges.add("除……外")
    f.dates = {m.group(0) for m in DATE_RE.finditer(text)}
    return f


def semantic_diff(before: str, after: str) -> list[SemanticChange]:
    a, b = features(before), features(after)
    out: list[SemanticChange] = []
    if a.obligation_words or b.obligation_words:
        # 比较增删的义务词，而不是整句最大值：“可以”改“必须”时，句中原有的“必须”不掩盖这一变化
        added = Counter(b.obligation_words) - Counter(a.obligation_words)
        removed = Counter(a.obligation_words) - Counter(b.obligation_words)
        up = max((kb.obligation_strength(w) for w in added), default=0.0)
        down = max((kb.obligation_strength(w) for w in removed), default=0.0)
        if (added or removed) and abs(up - down) >= 0.5:
            out.append(
                SemanticChange(
                    dimension="义务强度",
                    before="、".join(removed.elements()) or "（无）",
                    after="、".join(added.elements()) or "（无）",
                    direction="增强" if up > down else "减弱",
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
    new_broad = b.broad - a.broad
    if new_broad and (a.narrow - b.narrow or (a.scope == "narrow" and b.scope == "broad")):
        # 新增“全面”“全部”等用语且删去了“试点”“选取”等限定：实施范围扩大
        out.append(SemanticChange(dimension="实施范围", before="、".join(sorted(a.narrow)) or "试点/部分", after="、".join(sorted(new_broad)), direction="扩大"))
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
        for f in _facts_for(ctx, s):
            ev = [EvidenceRef(kind="fact", id=f.fact_id)]
            # 按事实所在小句判断进展：同句中的“将进一步”“计划”不掩盖“全部建成”
            m = mention_of(s.text, float(f.value), f.unit) if isinstance(f.value, (int, float)) else None
            if m is not None:
                p = progress_at(s.text, m.start)
                a, z = clause_span(s.text, m.start)
                clause = s.text[a:z]
                sm = mention_of(f.statement, float(f.value), f.unit)
                src_clause = f.statement[slice(*clause_span(f.statement, sm.start))] if sm else f.statement
            else:
                p, clause, src_clause = progress_of(s.text), s.text, f.statement
            if "example" in f.tags:
                out.append(ctx.issue("GW-FACT-005", IssueType.EXAMPLE_AS_FACT, f"{f.fact_id} 来自示例/模板数据，不能作为本次事项事实", block=b, sentence=s, evidence=ev, evidence_text=_source_excerpt(f)))
                continue
            if f.status in (FactStatus.UNKNOWN, FactStatus.CONFLICT):
                out.append(ctx.issue("GW-FACT-006", IssueType.CONFLICT_USED, f"{f.fact_id} 状态为“{f.status.value}”，未核清前不得使用", block=b, sentence=s, evidence=ev, evidence_text=_source_excerpt(f), needs_human=True))
                continue
            planned = f.status == FactStatus.PROPOSED or f.progress == Progress.PLANNED
            new_done = done_marks(clause) - done_marks(src_clause)
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
            if f.status == FactStatus.RECORDED and VERIFY_CLAIM.search(_claim_text(s.text)):
                out.append(ctx.issue("GW-FACT-001", IssueType.STATUS_UPGRADE, f"{f.fact_id} 为“材料记载”，未经独立核实，不能表述为“经核实”", block=b, sentence=s, evidence=ev, evidence_text=_source_excerpt(f), severity=Severity.MAJOR))
    return out


def check_approval_claims(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    if ctx.ir.genre == "批复":
        return out
    for b, s in ctx.ir.iter_sentences():
        text = _claim_text(s.text)
        m = APPROVAL_CLAIM.search(text)
        if not m:
            continue
        a, _ = clause_span(text, m.start())
        if re.search(r"(须|需|需要|应|应当|必须|要|报|报请|提请|待|拟)经?$", text[a : m.start()].strip()) or re.search(r"(须|需|应当?|必须|报请?|提请)经", text[a : m.end()]):
            continue  # “确需延期的，须经××批准”是程序要求，不是“已获批准”的陈述
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
    # 依据原文、任务说明中出现的数字（按数值与单位比对，不做子串匹配：“200万元”不因“1200万元”而有来源）
    texts = [e.quote for e in ctx.policies.items] if ctx.policies else []
    if ctx.task:
        texts.append(ctx.task.request_text)
    for t in texts:
        if any(same_quantity(mention, n.value, n.unit) for n in extract_numbers(t)):
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
    for b, s in ctx.ir.iter_sentences():
        claim = re.sub(r"现将会议议定事项", "", s.text)  # 纪要开头的固定说法不是议定内容
        if not any(w in claim for w in ("会议决定", "会议议定", "会议同意", "会议明确", "会议要求", "会议确定")):
            continue
        facts = _facts_for(ctx, s)
        if not facts:
            out.append(ctx.issue("GW-SEM-004", IssueType.DECISION_UPGRADE, "该议定事项未关联会议记录来源，请核对是否确已议定", block=b, sentence=s, needs_human=True, severity=Severity.MAJOR))
            continue
        for f in facts:
            if meeting_decision(f.statement) == "discussion":
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

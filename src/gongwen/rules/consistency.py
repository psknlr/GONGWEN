"""跨材料一致性检查（技能8）：正文↔附件、附件说明↔附件、交叉引用、表格合计、
同一事项多份文稿之间的数值与状态一致性。"""

from __future__ import annotations

import re

from ..schemas.common import EvidenceRef, IdAllocator, Severity
from ..schemas.facts import Progress
from ..schemas.review import ConsistencyFinding, ConsistencyReport, IssueType, ReviewIssue
from .base import CheckContext
from .semantics import progress_of
from .textutil import NON_ADDITIVE_HEADER, extract_numbers, is_subtotal_row, is_total_row, label_column, same_quantity

_ATT_REF = re.compile(r"附件\s*(\d+)")


def _num(s: str) -> float | None:
    s = s.replace(",", "").replace("，", "").strip()
    m = re.fullmatch(r"-?\d+(?:\.\d+)?", s)
    return float(s) if m else None


def check_attachments(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    ir = ctx.ir
    notes = {n.seq: n.name for n in ir.attachment_notes}
    atts = {a.seq: a.title for a in ir.attachments}
    for seq, name in notes.items():
        if name and name[-1] in "。，；：.,;":
            out.append(ctx.issue("GW-FMT-003", IssueType.FORMAT, f"附件说明中附件{seq}名称后不加标点符号", field_name="attachment_notes", original=name, auto_fixable=True, fix_hint={"op": "strip_attachment_punct"}))
    for n in ir.attachment_notes:
        if n.label and not re.fullmatch(r"\d{1,2}\.", n.label):
            out.append(ctx.issue("GW-FMT-003", IssueType.FORMAT, f"附件顺序号“{n.label}”应写作“{n.seq}.”（如“附件：1.××××”）", field_name="attachment_notes", original=f"{n.label}{n.name}"))
        if atts and seq not in atts:
            out.append(ctx.issue("GW-FMT-004", IssueType.ATTACHMENT_MISMATCH, f"附件说明列有附件{seq}“{name}”，但未见对应附件", field_name="attachments", needs_human=True))
        elif atts and atts[seq].strip() != name.strip().rstrip("。"):
            out.append(ctx.issue("GW-FMT-004", IssueType.ATTACHMENT_MISMATCH, f"附件{seq}标题“{atts[seq]}”与附件说明“{name}”不一致", field_name="attachments", auto_fixable=True, fix_hint={"op": "sync_attachment_title", "seq": str(seq)}))
    for seq, title in atts.items():
        if seq not in notes:
            out.append(ctx.issue("GW-FMT-004", IssueType.ATTACHMENT_MISMATCH, f"附件{seq}“{title}”未在附件说明中列出", field_name="attachment_notes", auto_fixable=True, fix_hint={"op": "add_attachment_note", "seq": str(seq)}))
    for b, s in ir.iter_sentences(include_attachments=False):
        for m in _ATT_REF.finditer(s.text):
            n = int(m.group(1))
            if notes and n not in notes:
                out.append(ctx.issue("GW-FMT-004", IssueType.CROSS_REF, f"正文引用“附件{n}”，但附件说明中没有附件{n}", block=b, sentence=s))
        if "见附件" in s.text and not notes:
            out.append(ctx.issue("GW-FMT-004", IssueType.CROSS_REF, "正文提及“见附件”，但没有附件说明", block=b, sentence=s))
    return out


def check_tables(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    for _, b in ctx.ir.iter_blocks():
        if b.kind != "table" or not b.table or len(b.table) < 3:
            continue
        header, rows = b.table[0], b.table[1:]
        total_rows = [r for r in rows if r and is_total_row(r)]
        body_rows = [r for r in rows if r not in total_rows and not is_subtotal_row(r)]
        lc = label_column(header, body_rows)
        for tr in total_rows:
            for c in range(len(header)):
                if c == lc or NON_ADDITIVE_HEADER.search(header[c]):
                    continue  # 比率、单价、序号等列不可加总
                vals = [_num(r[c]) for r in body_rows if c < len(r)]
                vals = [v for v in vals if v is not None]
                tv = _num(tr[c]) if c < len(tr) else None
                if vals and tv is not None and abs(sum(vals) - tv) > 1e-6 * max(1, abs(tv)):
                    out.append(
                        ctx.issue(
                            "GW-FACT-003",
                            IssueType.CALC_ERROR,
                            f"表格“{header[c]}”列合计应为 {sum(vals):g}，表中为 {tv:g}",
                            block=b,
                            needs_human=True,
                        )
                    )
    return out


def check_fact_values(ctx: CheckContext) -> list[ReviewIssue]:
    """句子引用了某事实，但句中同类数字与该事实取值不一致。"""
    out: list[ReviewIssue] = []
    if not ctx.ledger:
        return out
    for b, s in ctx.ir.iter_sentences():
        mentions = extract_numbers(s.text)
        if not mentions:
            continue
        ref_facts = [ctx.ledger.get(r.id) for r in s.refs if r.kind == "fact"]
        ref_facts = [f for f in ref_facts if f is not None and isinstance(f.value, (int, float))]
        for r in s.refs:
            if r.kind != "fact":
                continue
            f = ctx.ledger.get(r.id)
            if f is None or not isinstance(f.value, (int, float)) or r.note == "auto-linked":
                continue
            same_kind = [m for m in mentions if (m.kind == "money") == (f.kind == "money") and m.kind != "plain"]
            if not same_kind or any(same_quantity(m, float(f.value), f.unit) for m in same_kind):
                continue
            # 句中的数字由本句引用的其他事实解释：说明该句已不再陈述 f，而不是数字写错
            others = [g for g in ref_facts if g.fact_id != f.fact_id]
            unexplained = [m for m in same_kind if not any(same_quantity(m, float(g.value), g.unit) for g in others)]
            if not unexplained:
                out.append(
                    ctx.issue(
                        "GW-FACT-002",
                        IssueType.FACT_MISMATCH,
                        f"该句引用了 {f.fact_id}（{f.attribute} {f.display_value()}），但句中已不再出现该数据：请确认是删除了相应内容（应同时去掉该证据引用），还是需要补回",
                        block=b,
                        sentence=s,
                        evidence=[EvidenceRef(kind="fact", id=f.fact_id)],
                        evidence_text=f.sources[0].excerpt if f.sources else f.statement,
                        severity=Severity.MINOR,
                        needs_human=True,
                    )
                )
                continue
            out.append(
                ctx.issue(
                    "GW-FACT-002",
                    IssueType.FACT_MISMATCH,
                    f"该句引用 {f.fact_id}（{f.display_value()}），但句中数字为“{'、'.join(m.raw for m in unexplained)}”",
                    block=b,
                    sentence=s,
                    evidence=[EvidenceRef(kind="fact", id=f.fact_id)],
                    evidence_text=f.sources[0].excerpt if f.sources else f.statement,
                    severity=Severity.BLOCKING if f.kind == "money" else Severity.MAJOR,
                    auto_fixable=len(unexplained) == 1,
                    fix_hint={"op": "replace_number", "fact": f.fact_id, "raw": unexplained[0].raw},
                )
            )
    return out


def check_siblings(ctx: CheckContext) -> list[ReviewIssue]:
    """同一事项多份文稿：同一事实的取值与状态应一致，拟议目标不能在另一文稿中成为已实现成绩。"""
    out: list[ReviewIssue] = []
    if not ctx.siblings or not ctx.features.consistency_check:
        return out
    mine: dict[str, list[str]] = {}
    for b, s in ctx.ir.iter_sentences():
        for r in s.refs:
            if r.kind == "fact":
                mine.setdefault(r.id, []).append(s.text)
    for sib in ctx.siblings:
        for b2, s2 in sib.iter_sentences():
            for r in s2.refs:
                if r.kind != "fact" or r.id not in mine:
                    continue
                f = ctx.ledger.get(r.id) if ctx.ledger else None
                p2 = progress_of(s2.text)
                for t in mine[r.id]:
                    p1 = progress_of(t)
                    if {p1, p2} == {Progress.PLANNED, Progress.COMPLETED}:
                        out.append(
                            ctx.issue(
                                "GW-FACT-001",
                                IssueType.STATUS_UPGRADE,
                                f"同一事项的《{sib.title}》中该事实为“{p2.value}”，本稿为“{p1.value}”，状态不一致",
                                field_name="siblings",
                                original=t,
                                evidence_text=s2.text,
                                evidence=[EvidenceRef(kind="fact", id=r.id)],
                                needs_human=True,
                            )
                        )
                    if f and isinstance(f.value, (int, float)):
                        n1 = [m for m in extract_numbers(t) if same_quantity(m, float(f.value), f.unit)]
                        n2 = [m for m in extract_numbers(s2.text) if same_quantity(m, float(f.value), f.unit)]
                        if bool(n1) != bool(n2) and extract_numbers(t) and extract_numbers(s2.text):
                            out.append(
                                ctx.issue(
                                    "GW-FACT-002",
                                    IssueType.FACT_MISMATCH,
                                    f"同一事实 {r.id} 在《{sib.title}》与本稿中的数值表述不一致",
                                    field_name="siblings",
                                    original=t,
                                    evidence_text=s2.text,
                                    evidence=[EvidenceRef(kind="fact", id=r.id)],
                                )
                            )
    return out


CHECKERS = [check_attachments, check_tables, check_fact_values, check_siblings]


def build_report(ctx: CheckContext, issues: list[ReviewIssue]) -> ConsistencyReport:
    ids = IdAllocator()
    findings = []
    kinds = {
        IssueType.ATTACHMENT_MISMATCH: "附件不对应",
        IssueType.CROSS_REF: "交叉引用",
        IssueType.CALC_ERROR: "数值不一致",
        IssueType.FACT_MISMATCH: "数值不一致",
        IssueType.STATUS_UPGRADE: "状态不一致",
        IssueType.CALIBER_MISMATCH: "口径不一致",
    }
    for i in issues:
        if i.type not in kinds:
            continue
        findings.append(
            ConsistencyFinding(
                finding_id=ids.next("C"),
                kind=kinds[i.type],
                key=i.location.sentence_id or i.location.field or i.location.block_id or "",
                occurrences=[{"doc_id": i.location.doc_id, "location": i.location.label, "value": i.original[:80]}]
                + ([{"doc_id": "related", "location": "", "value": i.evidence_text[:80]}] if i.evidence_text else []),
                message=i.suggestion,
                severity=i.severity,
            )
        )
    checked = ["正文↔附件说明", "附件说明↔附件", "正文↔事实账本", "表格合计"]
    checked += [f"本稿↔《{s.title}》" for s in ctx.siblings]
    return ConsistencyReport(findings=findings, checked=checked)

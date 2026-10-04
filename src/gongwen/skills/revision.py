"""技能10 定向修订：按问题位置做最小必要修改，而不是整篇重写。

每次修订输出：修改位置、修改内容、修改原因、依据、关联影响、重新检查结果。
金额、事实状态、义务强度、实施范围、主送机关等关键内容变化，触发相应复核；
已审批版本发生实质性修改时，必须回到复审程序（条例第二十五条（一））。
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Callable

from ..harness.injection import UNTRUSTED_NOTICE
from ..llm.base import ChatMessage, ModelRefused, ModelUnavailable
from ..rules import CheckContext, run_checks
from ..rules.semantics import progress_of, semantic_diff
from ..rules.textutil import clause_span, extract_numbers, mention_of, same_quantity
from ..schemas.common import EvidenceRef, sha256_text
from ..schemas.facts import FactLedger, FactStatus, Progress, Verification
from ..schemas.ir import AttachmentNote, DocumentIR, Sentence
from ..schemas.patch import FactChange, Patch, PatchSet, RecheckResult
from ..schemas.review import IssueType, ReviewReport
from ..schemas.state import Stage
from .base import Skill, SkillContext
from .drafting import planned_form

CJK = r"一-鿿"
_HALF = {",": "，", ";": "；", ":": "：", "?": "？", "!": "！"}


def _fullwidth_punct(t: str) -> str:
    t = re.sub(rf"(?<=[{CJK}”）])([,;:?!])", lambda m: _HALF[m.group(1)], t)
    return re.sub(rf"([,;:?!])(?=[{CJK}“（])", lambda m: _HALF[m.group(1)], t)


TRANSFORMS: dict[str, Callable[[str], str]] = {
    "fullwidth_punct": _fullwidth_punct,
    "fullwidth_paren": lambda t: re.sub(rf"\(([^()]*[{CJK}][^()]*)\)", r"（\1）", t),
    "fullwidth_period": lambda t: re.sub(rf"(?<=[{CJK}”）])\.(?!\d)", "。", t),
    "ellipsis": lambda t: re.sub(r"\.{3,}|。{2,}|…+", "……", t),
    "dedupe_punct": lambda t: re.sub(r"([，。、；：])\1+", r"\1", t),
    "deng": lambda t: re.sub(r"、(等|等等)", r"\1", t),
    "year_range": lambda t: re.sub(r"(\d{4})\s*[-－~～至到]\s*(\d{4})\s*年", r"\1—\2年", t),
    "num_range": lambda t: re.sub(r"(?<![\d年月])(\d+(?:\.\d+)?)\s*[-－~]\s*(\d+(?:\.\d+)?)\s*(天|个|人|次|项|家|万元|元|小时|分钟|岁|米|公里|%|％)", r"\1～\2\3", t),
    "pct_range": lambda t: re.sub(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[～~—\-至到]\s*(\d+(?:\.\d+)?)\s*([%％])", r"\1\3～\2\3", t),
    "wan_range": lambda t: re.sub(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[～~—\-至到]\s*(\d+(?:\.\d+)?)\s*(万|亿)(元)?", lambda m: f"{m.group(1)}{m.group(3)}{m.group(4) or ''}～{m.group(2)}{m.group(3)}{m.group(4) or ''}", t),
    "lead_zero": lambda t: re.sub(rf"(?<=[\s{CJK}，：])\.(\d+)", r"0.\1", t),
    "unpad_date": lambda t: re.sub(r"(\d{4}年)0(\d)月", r"\1\2月", re.sub(r"月0(\d)日", r"月\1日", t)),
    "label_punct": lambda t: re.sub(r"^（([一二三四五六七八九十]+)）[、，.．]", r"（\1）", re.sub(r"^(\d+)[、，．]", r"\1.", re.sub(r"^([一二三四五六七八九十]+)[.．，]", r"\1、", re.sub(r"^\(([一二三四五六七八九十\d]+)\)", r"（\1）", t)))),
}


def _anchor(f) -> str:
    """事实属性的核心词，用于判断未引用该事实的句子是否在说同一件事。"""
    a = f.attribute or ""
    if a.startswith("合计·"):
        return "合计"
    a = a.split("·")[0]
    return a[-4:] if len(a) > 4 else a


def _downgrade(text: str, ledger: FactLedger, fact_id: str) -> str:
    f = ledger.get(fact_id)
    nums = extract_numbers(text)
    if f is not None and f.statement and all(isinstance(f.value, (int, float)) and same_quantity(n, float(f.value), f.unit) for n in nums if n.kind == f.kind):
        src = f.statement.strip()
        return src if src.endswith(("。", "！", "？")) else src + "。"
    # 只改该事实所在的小句：同句中“已建成8个”等真实完成的内容保持不变
    m = mention_of(text, float(f.value), f.unit) if f is not None and isinstance(f.value, (int, float)) else None
    if m is not None:
        a, z = clause_span(text, m.start)
        clause = text[a:z]
        fixed = planned_form(clause) if progress_of(clause) != Progress.PLANNED else clause
        return text[:a] + fixed + text[z:]
    t = re.sub(r"已经|已", "", text, count=1)
    return planned_form(t)


class RevisionSkill(Skill):
    name = "gongwen-targeted-revision"
    number = 10
    title = "定向修订"
    stage = Stage.REVISION
    channel_name = "reviser"
    allowed_tools = ()
    output_artifact = "patchset"

    # ------------------------------------------------------------------ 提出修订
    def propose(self, sc: SkillContext, ir: DocumentIR, report: ReviewReport, ledger: FactLedger | None, round_no: int) -> PatchSet:
        ps = PatchSet(doc_id=ir.doc_id, round=round_no, from_version=ir.version)
        pending: dict[str, str] = {}  # sid -> 累积修改后的文本（同一句多处机械修订合并）
        issue_by_sid: dict[str, list[str]] = {}
        for i in report.open_issues():
            if not i.auto_fixable or not i.fix_hint:
                continue
            hint = i.fix_hint
            sid = i.location.sentence_id
            if sid:
                found = ir.find_sentence(sid)
                if not found:
                    continue
                _, s = found
                cur = pending.get(sid, s.text)
                new = cur
                op = hint.get("op")
                if op in TRANSFORMS:
                    new = TRANSFORMS[op](cur)
                elif op == "downgrade_progress" and ledger is not None:
                    new = _downgrade(cur, ledger, hint.get("fact", ""))
                elif op == "replace_number" and ledger is not None:
                    f = ledger.get(hint.get("fact", ""))
                    if f is not None:
                        new = cur.replace(hint.get("raw", ""), f.display_value(), 1)
                elif "replace" in hint and "with" in hint:
                    new = cur.replace(hint["replace"], hint["with"])
                elif "regex" in hint:
                    new = re.sub(hint["regex"], hint["with"], cur)
                if new != cur:
                    pending[sid] = new
                    issue_by_sid.setdefault(sid, []).append(i.issue_id)
                if hint.get("append_sentence"):
                    ps.patches.append(Patch(patch_id=sc.ids.next("PA"), doc_id=ir.doc_id, target=sid, op="insert_after", after=hint["append_sentence"], reason=i.suggestion, issue_ids=[i.issue_id]))
                continue
            if i.location.block_id and "set_label" in hint:
                b = ir.find_block(i.location.block_id)
                if b:
                    ps.patches.append(Patch(patch_id=sc.ids.next("PA"), doc_id=ir.doc_id, target=b.bid, op="set_label", before=b.label, after=hint["set_label"], reason=i.suggestion, issue_ids=[i.issue_id]))
                continue
            field = i.location.field
            if field == "title" and "strip_end" in hint:
                ps.patches.append(Patch(patch_id=sc.ids.next("PA"), doc_id=ir.doc_id, target="title", op="set_field", before=ir.title, after=ir.title.rstrip("。，；：！？.,;:"), reason=i.suggestion, issue_ids=[i.issue_id]))
            elif field == "attachment_notes" and hint.get("op") == "strip_attachment_punct":
                for n in ir.attachment_notes:
                    if n.name and n.name[-1] in "。，；：.,;":
                        ps.patches.append(Patch(patch_id=sc.ids.next("PA"), doc_id=ir.doc_id, target=f"attachment_notes[{n.seq}]", op="set_field", before=n.name, after=n.name.rstrip("。，；：.,;"), reason=i.suggestion, issue_ids=[i.issue_id]))
            elif field == "attachments" and hint.get("op") == "sync_attachment_title":
                seq = int(hint["seq"])
                note = next((n for n in ir.attachment_notes if n.seq == seq), None)
                att = next((a for a in ir.attachments if a.seq == seq), None)
                if note and att:
                    ps.patches.append(Patch(patch_id=sc.ids.next("PA"), doc_id=ir.doc_id, target=f"attachments[{seq}].title", op="set_field", before=att.title, after=note.name.rstrip("。"), reason=i.suggestion, issue_ids=[i.issue_id]))
            elif field == "recipients" and hint.get("op") == "strip_recipient_punct":
                ps.patches.append(Patch(patch_id=sc.ids.next("PA"), doc_id=ir.doc_id, target="recipients", op="set_field", before="、".join(ir.recipients), after="、".join(r.rstrip("：:，。") for r in ir.recipients), reason=i.suggestion, issue_ids=[i.issue_id]))
        semantic_ops = {"downgrade_progress", "replace_number"}
        semantic_sids = {
            i.location.sentence_id
            for i in report.open_issues()
            if i.auto_fixable and i.fix_hint.get("op") in semantic_ops and i.location.sentence_id
        }
        for sid, new in pending.items():
            _, s = ir.find_sentence(sid)
            changes = semantic_diff(s.text, new)
            risky = [c for c in changes if c.direction in ("增强", "扩大", "删除", "升级")]
            # 人工撰写的句子：语义类修订只提出建议，不自动推翻人的修改
            if s.origin == "human" and sid in semantic_sids:
                risky = risky or changes or [None]
            ps.patches.append(
                Patch(
                    patch_id=sc.ids.next("PA"),
                    doc_id=ir.doc_id,
                    target=sid,
                    op="replace",
                    before=s.text,
                    after=new,
                    reason="；".join(next((i.suggestion for i in report.issues if i.issue_id == iid), "") for iid in issue_by_sid.get(sid, []))[:300],
                    issue_ids=issue_by_sid.get(sid, []),
                    basis=[r for r in s.refs],
                    semantic_changes=changes,
                    status="needs_human" if risky else "proposed",
                )
            )
        # 需要人工裁定的修订建议（不自动应用）：如报告中夹带的请示事项
        for i in report.open_issues():
            if i.type == IssueType.MIXED_REQUEST and i.location.sentence_id:
                ps.patches.append(
                    Patch(
                        patch_id=sc.ids.next("PA"),
                        doc_id=ir.doc_id,
                        target=i.location.sentence_id,
                        op="delete",
                        before=i.original,
                        reason="报告中不得夹带请示事项：建议删除该句，并就需批准事项另行请示",
                        issue_ids=[i.issue_id],
                        impact=["正文", "另行起草请示"],
                        status="needs_human",
                    )
                )
        return ps

    # ------------------------------------------------------------------ 应用修订
    def apply(self, sc: SkillContext, ir: DocumentIR, ps: PatchSet, statuses: tuple[str, ...] = ("proposed",)) -> DocumentIR:
        new = deepcopy(ir)
        new.version = ir.version + 1
        new.based_on_version = ir.version
        applied = 0
        for p in ps.patches:
            if p.status not in statuses:
                continue
            ok = self._apply_one(sc, new, p)
            p.status = "applied" if ok else "rejected"
            applied += int(ok)
            if ok:
                p.impact = p.impact or self._impact(new, p)
        ps.to_version = new.version
        ps.affected_nodes = [p.target for p in ps.patches if p.status == "applied"]
        sc.note("skill.revision.apply", {"doc_id": ir.doc_id, "from": ir.version, "to": new.version, "applied": applied, "skipped": len(ps.patches) - applied})
        return new

    def _apply_one(self, sc: SkillContext, ir: DocumentIR, p: Patch) -> bool:
        if p.op == "replace":
            found = ir.find_sentence(p.target)
            if not found:
                return False
            _, s = found
            s.text = p.after
            s.origin = "patch" if p.author != "human" else "human"
            return True
        if p.op == "insert_after":
            found = ir.find_sentence(p.target)
            if not found:
                return False
            b, s = found
            idx = b.sentences.index(s)
            b.sentences.insert(idx + 1, Sentence(sid=f"{p.target}a", text=p.after, function="结语", origin="patch"))
            return True
        if p.op == "delete":
            found = ir.find_sentence(p.target)
            if not found:
                return False
            b, s = found
            b.sentences.remove(s)
            return True
        if p.op == "set_label":
            b = ir.find_block(p.target)
            if not b:
                return False
            b.label = p.after
            return True
        if p.op == "set_cell":
            bid, r, c = p.target.rsplit(".", 2)
            blk = ir.find_block(bid)
            if not blk or not blk.table:
                return False
            blk.table[int(r[1:])][int(c[1:])] = p.after
            return True
        if p.op == "set_field":
            if p.target == "title":
                ir.title = p.after
            elif p.target == "recipients":
                ir.recipients = [x for x in p.after.split("、") if x]
            elif p.target.startswith("attachment_notes["):
                seq = int(p.target[p.target.index("[") + 1 : p.target.index("]")])
                for n in ir.attachment_notes:
                    if n.seq == seq:
                        n.name = p.after
            elif p.target.startswith("attachments["):
                seq = int(p.target[p.target.index("[") + 1 : p.target.index("]")])
                for a in ir.attachments:
                    if a.seq == seq:
                        a.title = p.after
            else:
                return False
            return True
        return False

    @staticmethod
    def _impact(ir: DocumentIR, p: Patch) -> list[str]:
        found = ir.find_sentence(p.target)
        if found:
            b, _ = found
            return [ir.location_label(b.bid)]
        return [p.target]

    def recheck(self, sc: SkillContext, ctx: CheckContext, ps: PatchSet) -> None:
        issues = run_checks(ctx)
        for p in ps.patches:
            if p.status != "applied":
                continue
            remaining = [i.issue_id for i in issues if i.location.sentence_id == p.target or i.location.block_id == p.target or i.location.field == p.target]
            ps.recheck.append(RecheckResult(target=p.target, checks=["全部确定性检查（受影响节点）"], remaining_issue_ids=remaining, ok=not remaining))

    # ------------------------------------------------------------------ 关键事实变更传播
    def fact_change(self, sc: SkillContext, ir: DocumentIR, ledger: FactLedger, change: FactChange, round_no: int) -> PatchSet:
        """更新事项账本中的事实（及依赖它的合计），并联动本稿中的句子与附件表格。"""
        olds = self.apply_fact_change(ledger, change)
        f = ledger.get(change.fact_id)
        reason = f"关键事实变更：{f.attribute} {olds['__before__']} → {f.display_value()}（{change.reason or '人工变更'}）"
        ps = self.propagate_values(sc, ir, ledger, olds, reason, round_no)
        ps.fact_changes = [change]
        return ps

    @staticmethod
    def apply_fact_change(ledger: FactLedger, change: FactChange) -> dict:
        """只改账本：返回变更前的取值（含受影响的计算结果），供本稿及同一事项其他文稿联动使用。"""
        f = ledger.get(change.fact_id)
        if f is None:
            raise KeyError(f"事实不存在：{change.fact_id}")
        olds: dict = {}
        if isinstance(f.value, (int, float)):
            olds[f.fact_id] = (float(f.value), f.unit)
        olds["__before__"] = f.display_value()
        f.value = float(change.new_value) if isinstance(change.new_value, (int, float)) or re.fullmatch(r"-?\d+(\.\d+)?", str(change.new_value)) else change.new_value
        if change.unit:
            f.unit = change.unit
        if f.status != FactStatus.PROPOSED:
            f.status = FactStatus.VERIFIED  # 人工更正的现状数据视为已核实；拟议内容改了数仍是拟议，不因更正而升级
        f.verification = Verification(method="人工变更", by=change.by, note=f"{olds['__before__']} → {f.display_value()}；{change.reason}")
        # 重新计算依赖它的计算结果（如合计）
        for dep in ledger.dependents(change.fact_id):
            if dep.formula and isinstance(dep.value, (int, float)):
                olds[dep.fact_id] = (float(dep.value), dep.unit)
                vals = [ledger.get(i).value for i in dep.formula.inputs if ledger.get(i) is not None]
                if all(isinstance(v, (int, float)) for v in vals):
                    dep.value = round(sum(float(v) for v in vals), 6)
                    dep.formula.result_repr = dep.display_value()
                    dep.statement = re.sub(r"合计[\d.]+", f"合计{dep.value:g}", dep.statement)
        return olds

    def propagate_values(self, sc: SkillContext, ir: DocumentIR, ledger: FactLedger, olds: dict, reason: str, round_no: int) -> PatchSet:
        """把账本中已变更的取值同步到文稿句子与附件表格（逐处生成补丁，留痕可复核）。"""
        ps = PatchSet(doc_id=ir.doc_id, round=round_no, from_version=ir.version)
        values = {k: v for k, v in olds.items() if not k.startswith("__")}
        changed_ids = set(values)
        for b, s in ir.iter_sentences():
            hit_refs = {r.id for r in s.refs if r.id in changed_ids}
            # 按位置替换（“8个”不会改到“18个”里），从后往前替换以保持位置有效
            edits: dict[int, tuple[int, str]] = {}
            for m in extract_numbers(s.text):
                for fid, (old_v, unit) in values.items():
                    nf = ledger.get(fid)
                    if nf is None or not same_quantity(m, old_v, unit) or m.unit not in (unit, ""):
                        continue
                    # 未引用该事实的句子：只在同一小句中出现该事实的属性名时才联动（“开展培训8次”不是“示范点8个”）
                    a, z = clause_span(s.text, m.start)
                    if fid in hit_refs or _anchor(nf) in s.text[a:z]:
                        edits[m.start] = (m.end, nf.display_value())
                        break
            new_text = s.text
            for start in sorted(edits, reverse=True):
                end, val = edits[start]
                new_text = new_text[:start] + val + new_text[end:]
            if new_text != s.text:
                ps.patches.append(
                    Patch(
                        patch_id=sc.ids.next("PA"),
                        doc_id=ir.doc_id,
                        target=s.sid,
                        op="replace",
                        before=s.text,
                        after=new_text,
                        reason=reason,
                        basis=[EvidenceRef(kind="fact", id=x) for x in sorted(changed_ids)],
                        author="human",
                        status="proposed",
                    )
                )
        # 附件表格中的对应单元格与合计行
        for att in ir.attachments:
            for blk in att.blocks:
                if blk.kind != "table" or not blk.table:
                    continue
                for r_idx, row in enumerate(blk.table):
                    for c_idx, cell in enumerate(row):
                        for fid, (old_v, unit) in values.items():
                            nf = ledger.get(fid)
                            try:
                                cv = float(cell.replace(",", ""))
                            except ValueError:
                                continue
                            label = row[0] if row else ""
                            if abs(cv - old_v) < 1e-9 and (label in nf.attribute or "合计" in label and "合计" in nf.attribute):
                                ps.patches.append(Patch(patch_id=sc.ids.next("PA"), doc_id=ir.doc_id, target=f"{blk.bid}.r{r_idx}.c{c_idx}", op="set_cell", before=cell, after=f"{float(nf.value):g}", reason=f"关键事实变更同步附件表格：{nf.attribute}", author="human", status="proposed"))
        ps.affected_docs = [ir.doc_id]
        return ps

    # ------------------------------------------------------------------ 人工修改与模型辅助修改
    def human_edit(self, sc: SkillContext, ir: DocumentIR, sid: str, new_text: str, by: str, round_no: int) -> PatchSet:
        found = ir.find_sentence(sid)
        if not found:
            raise KeyError(f"句子不存在：{sid}")
        _, s = found
        changes = semantic_diff(s.text, new_text)
        ps = PatchSet(doc_id=ir.doc_id, round=round_no, from_version=ir.version)
        ps.patches.append(
            Patch(
                patch_id=sc.ids.next("PA"),
                doc_id=ir.doc_id,
                target=sid,
                op="replace",
                before=s.text,
                after=new_text,
                reason=f"人工修改（{by}）",
                author="human",
                semantic_changes=changes,
                status="proposed",
            )
        )
        return ps

    def instruction(self, sc: SkillContext, ir: DocumentIR, instruction: str, ledger: FactLedger | None, round_no: int) -> PatchSet:
        """按修改意见做定向修订：需要模型；每条修改仍经确定性校验，强化语义的修改转人工确认。"""
        ps = PatchSet(doc_id=ir.doc_id, round=round_no, from_version=ir.version)
        if not sc.model_available("heavy"):
            ps.escalated_issue_ids.append("INSTRUCTION_NEEDS_HUMAN")
            sc.note("skill.revision.instruction", {"result": "no_model", "instruction_sha": sha256_text(instruction)})
            return ps
        sentences = [{"sid": s.sid, "text": s.text} for _, s in ir.iter_sentences()]
        schema = {
            "type": "object",
            "properties": {"edits": {"type": "array", "items": {"type": "object", "properties": {"sid": {"type": "string"}, "new_text": {"type": "string"}, "reason": {"type": "string"}}, "required": ["sid", "new_text", "reason"], "additionalProperties": False}}},
            "required": ["edits"],
            "additionalProperties": False,
        }
        system = "你是公文定向修订助手。只修改与修改意见直接相关的最少句子，不得新增事实、数字、任务或承诺，不得改变未被要求修改的义务强度、范围和条件。" + UNTRUSTED_NOTICE
        try:
            resp = sc.router.call("heavy", [ChatMessage("user", f"修改意见：{instruction}\n\n文稿逐句：\n{json.dumps(sentences, ensure_ascii=False)}")], system=system, json_schema=schema, clearances=sc.clearances, purpose="revision", template_id="revision.v1", object_refs=[s['sid'] for s in sentences])
            edits = resp.json().get("edits", [])
        except (ModelUnavailable, ModelRefused, ValueError) as exc:
            sc.note("skill.model_skipped", {"skill": self.name, "reason": str(exc)})
            ps.escalated_issue_ids.append("INSTRUCTION_NEEDS_HUMAN")
            return ps
        for e in edits:
            found = ir.find_sentence(e.get("sid", ""))
            if not found:
                continue
            _, s = found
            new_text = (e.get("new_text") or "").strip()
            if not new_text or new_text == s.text:
                continue
            # 新增数字必须来自事实账本
            bad = [n.raw for n in extract_numbers(new_text) if not (ledger and any(isinstance(f.value, (int, float)) and same_quantity(n, float(f.value), f.unit) for f in ledger.facts)) and n.raw not in s.text]
            changes = semantic_diff(s.text, new_text)
            risky = bad or any(c.direction in ("增强", "扩大", "删除", "升级") for c in changes)
            ps.patches.append(
                Patch(
                    patch_id=sc.ids.next("PA"),
                    doc_id=ir.doc_id,
                    target=s.sid,
                    op="replace",
                    before=s.text,
                    after=new_text,
                    reason=f"按修改意见：{e.get('reason', '')}" + (f"（含无来源数字：{'、'.join(bad)}）" if bad else ""),
                    author="model",
                    semantic_changes=changes,
                    basis=list(s.refs),
                    status="needs_human" if risky else "proposed",
                )
            )
        return ps

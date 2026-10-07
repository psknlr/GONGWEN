"""按提示词改写：先标定固定句，再结合用户的改写要求重写其余句子。

固定句（不得改写）分三个来源，优先级从高到低：
1. 人工：在工作台或命令行逐句锁定、解锁，人的决定始终优先；
2. 模型标定（可选）：模型按改写要求指出“改写后容易走样”的句子（任务分工、法定表述等），
   只能增加锁定，不能解除确定性规则锁定的句子；
3. 规则标定（确定性）：含数字金额日期、引用文件标题或文号、审批决定与会议议定、来文引用、
   惯用结束语、待补占位的句子。

改写只产生“建议”，不直接改稿：每条建议都经确定性校验——
固定句、待补占位、新增数字、新增文件标题或文号、新增审批或核实说法、疑似指令一律拒绝；
义务强度、事实状态、范围、决策状态、条件与时限发生变化的转人工确认。
人工采纳后作为修订进入新版本，并重新经过独立审校。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..harness.injection import UNTRUSTED_NOTICE, detect as detect_injection
from ..knowledge.retrieval import find_doc_numbers, find_titles
from ..llm.base import ChatMessage, ModelCallFailed, ModelRefused, ModelUnavailable
from ..rules.semantics import APPROVAL_CLAIM, VERIFY_CLAIM, semantic_diff
from ..rules.textutil import extract_numbers, same_quantity
from ..schemas.common import sha256_text
from ..schemas.facts import FactLedger
from ..schemas.ir import DocumentIR, Sentence
from ..schemas.patch import Patch
from .base import SkillContext

SKILL_NAME = "gongwen-targeted-revision"  # 改写属于定向修订的能力，不另设技能
_PLACEHOLDER = re.compile(r"【待[^】]*】")
_CLOSING = re.compile(r"^(特此(通知|报告|函告|函复|通报|通告|公告|批复)|此复|妥否|当否|以上(请示|报告|意见)|请予|请函复|盼复|现提请审议|专此函复)")
# 只有这些语义维度的变化需要人确认；其余（如语序、措辞）属于改写本身
_RISKY_DIRECTIONS = {"增强", "减弱", "扩大", "删除", "升级", "降级", "改变"}
# 时限与时间节点（数字抽取不含“6月底”“3个工作日内”等，单独比对）
_TIME = re.compile(r"(?:\d{4}年)?\d{1,2}月(?:\d{1,2}日)?(?:底|末|初|上旬|中旬|下旬)?|\d{4}年(?:底|末|内|年底)?|\d+个?(?:工作日|日|天|周|个月|小时)(?:内)?")


@dataclass
class Lock:
    sid: str
    locked: bool
    source: str  # auto/model/human
    reason: str = ""
    by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"sid": self.sid, "locked": self.locked, "source": self.source, "reason": self.reason, "by": self.by}


@dataclass
class RewriteOutcome:
    patches: list[Patch] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    model_used: bool = False


def body_sentences(ir: DocumentIR) -> list[tuple[Any, Sentence]]:
    """可改写的范围：正文句子（不含附件；标题、主送、落款等要素不在句子层面改写）。"""
    return list(ir.iter_sentences(include_attachments=False))


def auto_locks(ir: DocumentIR) -> dict[str, str]:
    """规则标定：返回 {句子编号: 锁定理由}。理由可解释、可复核，不依赖模型。"""
    out: dict[str, str] = {}
    for _, s in body_sentences(ir):
        t = s.text
        reasons = []
        if _PLACEHOLDER.search(t):
            reasons.append("含待补内容，须由真实材料或人工填写")
        if extract_numbers(t):
            reasons.append("含数字、金额、日期等事实数据")
        if find_titles(t) or find_doc_numbers(t):
            reasons.append("引用文件标题或发文字号")
        if APPROVAL_CLAIM.search(t) or any(r.kind in ("approval",) for r in s.refs):
            reasons.append("审批、决定事项")
        if any(r.kind == "fact" for r in s.refs) and re.search(r"会议|议定|决定|研究", t):
            reasons.append("会议议定或研究决定事项")
        if "收悉" in t or any(r.kind == "material" and r.note == "来文" for r in s.refs):
            reasons.append("引用来文")
        if any(r.kind == "policy" for r in s.refs):
            reasons.append("引用依据")
        if s.function == "结语" or _CLOSING.match(t):
            reasons.append("惯用结束语（与文种、行文方向对应）")
        if reasons:
            out[s.sid] = "；".join(dict.fromkeys(reasons))
    return out


def effective_locks(ir: DocumentIR, human: dict[str, dict], model: dict[str, str]) -> list[Lock]:
    """合成每句的锁定状态：人工 > 模型标定 > 规则标定。"""
    auto = auto_locks(ir)
    out: list[Lock] = []
    for _, s in body_sentences(ir):
        h = human.get(s.sid)
        if h is not None:
            reason = h.get("reason") or ("人工锁定" if h.get("locked") else "人工解除锁定")
            out.append(Lock(s.sid, bool(h.get("locked")), "human", reason, h.get("by", "")))
        elif s.sid in auto:
            out.append(Lock(s.sid, True, "auto", auto[s.sid]))
        elif s.sid in model:
            out.append(Lock(s.sid, True, "model", model[s.sid]))
        else:
            out.append(Lock(s.sid, False, "auto", ""))
    return out


def _context_rows(ir: DocumentIR, locks: dict[str, Lock]) -> list[dict[str, Any]]:
    rows = []
    for b, s in body_sentences(ir):
        lk = locks.get(s.sid)
        rows.append(
            {
                "sid": s.sid,
                "section": f"{b.label}{b.heading}".strip() or ("正文" if b.kind == "paragraph" else b.kind),
                "text": s.text,
                "locked": bool(lk and lk.locked),
                **({"lock_reason": lk.reason} if lk and lk.locked else {}),
            }
        )
    return rows


def calibrate(sc: SkillContext, ir: DocumentIR, prompt: str, locks: list[Lock]) -> tuple[dict[str, str], list[str]]:
    """模型标定：按改写要求，指出未锁定句中应保持原文的句子。只增不减；模型不可用时如实说明。"""
    notes: list[str] = []
    if not sc.model_available("heavy"):
        return {}, ["未配置可用模型，跳过模型标定（仍按规则标定与人工锁定执行）"]
    by_sid = {lk.sid: lk for lk in locks}
    free = [r for r in _context_rows(ir, by_sid) if not r["locked"]]
    if not free:
        return {}, ["没有未锁定的句子，无需模型标定"]
    schema = {
        "type": "object",
        "properties": {"keep": {"type": "array", "items": {"type": "object", "properties": {"sid": {"type": "string"}, "reason": {"type": "string"}}, "required": ["sid", "reason"], "additionalProperties": False}}},
        "required": ["keep"],
        "additionalProperties": False,
    }
    system = (
        "你是公文审校助手，负责在改写前标定“固定句”。给你一份公文中尚未锁定的句子和用户的改写要求。"
        "请指出其中改写后容易改变实质内容、因而应保持原文的句子：如任务分工与责任主体、法律法规或政策的规范表述、"
        "对方提出的请求事项、表彰批评与任免的具体表述、限定条件与例外。普通的背景、过渡与表态句不要列入。"
        "只能从给定句子中选择，理由用一句中文说明。" + UNTRUSTED_NOTICE
    )
    try:
        resp = sc.router.call(
            "heavy",
            [ChatMessage("user", f"改写要求：{prompt}\n\n未锁定的句子：\n{json.dumps(free, ensure_ascii=False)}")],
            system=system,
            json_schema=schema,
            clearances=sc.clearances,
            purpose="rewrite_calibration",
            template_id="rewrite.calibrate.v1",
            object_refs=[r["sid"] for r in free],
        )
        keep = resp.json().get("keep", [])
    except (ModelUnavailable, ModelRefused, ModelCallFailed, ValueError) as exc:
        sc.model_fallback(SKILL_NAME, exc)
        return {}, [f"模型标定未完成（{type(exc).__name__}），仍按规则标定与人工锁定执行"]
    free_ids = {r["sid"] for r in free}
    out: dict[str, str] = {}
    for k in keep if isinstance(keep, list) else []:
        sid = str(k.get("sid", "")) if isinstance(k, dict) else ""
        if sid in free_ids:
            out[sid] = "模型标定：" + str(k.get("reason", "")).strip()[:80]
    ignored = len(keep) - len(out) if isinstance(keep, list) else 0
    if ignored:
        notes.append(f"模型标定中 {ignored} 条不在可标定范围内（不存在或已锁定），已忽略")
    return out, notes


def _sourced(n, ledger: FactLedger | None) -> bool:
    return bool(ledger) and any(isinstance(f.value, (int, float)) and same_quantity(n, float(f.value), f.unit) for f in ledger.facts)


def validate_edit(before: str, after: str, ledger: FactLedger | None, known_titles: set[str]) -> tuple[str, list[str], list]:
    """校验一条改写：返回（状态, 理由, 语义变化）。状态为 proposed / needs_human / rejected。"""
    hard: list[str] = []
    if sorted(_PLACEHOLDER.findall(before)) != sorted(_PLACEHOLDER.findall(after)):
        hard.append("改动了待补占位（待补内容须由真实材料或人工填写）")
    new_nums = [n.raw for n in extract_numbers(after) if n.raw not in before and not _sourced(n, ledger)]
    if new_nums:
        hard.append(f"新增无来源数字：{'、'.join(new_nums)}")
    new_titles = [t for t in find_titles(after) if t not in before and t not in known_titles]
    new_numbers = [d for d in find_doc_numbers(after) if d not in before]
    if new_titles or new_numbers:
        hard.append(f"新增文件标题或文号：{'、'.join(new_titles + new_numbers)}")
    new_times = [t for t in _TIME.findall(after) if t not in before]
    if new_times:
        hard.append(f"新增或改动时限：{'、'.join(new_times)}")
    if APPROVAL_CLAIM.search(after) and not APPROVAL_CLAIM.search(before):
        hard.append("新增审批、同意或研究决定的说法")
    if VERIFY_CLAIM.search(after) and not VERIFY_CLAIM.search(before):
        hard.append("新增“经核实”等核验说法")
    if detect_injection(after):
        hard.append("改写结果含疑似指令性语句")
    if hard:
        return "rejected", hard, []
    if not after:
        return "needs_human", ["删除整句"], []
    changes = semantic_diff(before, after)
    risky = [c for c in changes if c.direction in _RISKY_DIRECTIONS]
    lost = [n.raw for n in extract_numbers(before) if n.raw not in after]
    lost_times = [t for t in _TIME.findall(before) if t not in after]
    soft = [f"{c.dimension}{c.direction}（{c.before}→{c.after}）" for c in risky] + ([f"删去了数字：{'、'.join(lost)}"] if lost else []) + ([f"删去了时限：{'、'.join(lost_times)}"] if lost_times else [])
    return ("needs_human" if soft else "proposed"), soft, changes


def rewrite(sc: SkillContext, ir: DocumentIR, prompt: str, locks: list[Lock], ledger: FactLedger | None, genre: str = "") -> RewriteOutcome:
    """结合改写要求重写未锁定的句子；每条改写都经确定性校验，只形成建议。"""
    out = RewriteOutcome()
    if not sc.model_available("heavy"):
        out.notes.append("未配置可用模型：AI 改写需要模型（见 gongwen model 或配置文件的模型设置）。仍可逐句人工修改。")
        sc.note("skill.revision.rewrite", {"result": "no_model", "prompt_sha": sha256_text(prompt)})
        return out
    by_sid = {lk.sid: lk for lk in locks}
    rows = _context_rows(ir, by_sid)
    editable = {r["sid"] for r in rows if not r["locked"]}
    if not editable:
        out.notes.append("所有句子都已锁定，没有可改写的句子")
        return out
    schema = {
        "type": "object",
        "properties": {"edits": {"type": "array", "items": {"type": "object", "properties": {"sid": {"type": "string"}, "new_text": {"type": "string"}, "reason": {"type": "string"}}, "required": ["sid", "new_text", "reason"], "additionalProperties": False}}},
        "required": ["edits"],
        "additionalProperties": False,
    }
    system = (
        f"你是党政机关公文的改写助手，文种：{genre or '公文'}。按用户的改写要求，只改写 locked=false 的句子；locked=true 的句子是固定句，只作上下文，一字不改。"
        "改写规则：保持每句的事实、对象、范围、条件、时限和义务强度（须、应、要、可以等）不变；不得新增事实、数字、任务、承诺、"
        "文件标题、文号，不得写入“经××批准/同意”“经核实”等说法；不得改动【待…】占位；用规范的公文语言，避免口语和空泛表态。"
        "只返回实际改动的句子，每句给出新文本和一句话理由；确属重复、可删除的句子，new_text 返回空字符串并说明理由。" + UNTRUSTED_NOTICE
    )
    try:
        resp = sc.router.call(
            "heavy",
            [ChatMessage("user", f"改写要求：{prompt}\n\n文稿逐句（按顺序）：\n{json.dumps(rows, ensure_ascii=False)}")],
            system=system,
            json_schema=schema,
            clearances=sc.clearances,
            purpose="rewrite",
            template_id="rewrite.v1",
            object_refs=sorted(editable),
        )
        edits = resp.json().get("edits", [])
    except (ModelUnavailable, ModelRefused, ModelCallFailed, ValueError) as exc:
        sc.model_fallback(SKILL_NAME, exc)
        out.notes.append(f"模型改写未完成（{type(exc).__name__}）：未产生任何建议，原稿未改动")
        return out
    out.model_used = True
    known_titles = {t for _, s in ir.iter_sentences() for t in find_titles(s.text)}
    seen: set[str] = set()
    for e in edits if isinstance(edits, list) else []:
        if not isinstance(e, dict):
            continue
        sid = str(e.get("sid", ""))
        found = ir.find_sentence(sid)
        if found is None or sid not in {r["sid"] for r in rows}:
            out.notes.append(f"模型返回了不存在或不在正文范围内的句子 {sid or '（空）'}，已忽略")
            continue
        if sid in seen:
            continue
        seen.add(sid)
        _, s = found
        after = str(e.get("new_text") or "").strip()
        if after == s.text:
            continue
        reason_m = str(e.get("reason") or "").strip()[:120]
        if sid not in editable:
            status, why, changes = "rejected", [f"固定句不得改写（{by_sid[sid].reason}）"], []
        else:
            status, why, changes = validate_edit(s.text, after, ledger, known_titles)
        out.patches.append(
            Patch(
                patch_id=sc.ids.next("PA"),
                doc_id=ir.doc_id,
                target=sid,
                op="delete" if not after else "replace",
                before=s.text,
                after=after,
                reason=f"按改写要求：{reason_m}" + (f"｜{'；'.join(why)}" if why else ""),
                author="model",
                semantic_changes=changes,
                basis=list(s.refs),
                status=status,
            )
        )
    sc.note(
        "skill.revision.rewrite",
        {
            "prompt_sha": sha256_text(prompt),
            "editable": len(editable),
            "locked": len(rows) - len(editable),
            "proposed": sum(p.status == "proposed" for p in out.patches),
            "needs_human": sum(p.status == "needs_human" for p in out.patches),
            "rejected": sum(p.status == "rejected" for p in out.patches),
        },
    )
    return out

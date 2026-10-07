"""评测运行器：回归用例、基线对比与消融实验（设计 §9）。

用例（YAML）分四组：文种与行文、事实与依据、语义与修订、格式与版式。五类用例：
* pipeline：完整办文流程（确定性路径），模拟人工在各审核节点的处理，核对文种、状态、文稿内容与问题；
* check：对外部文稿做规范检查，核对“埋入的问题”是否被检出、干净对照是否误报；
* admission：材料准入（密级标志、个人信息、提示注入）；
* revision：形成文稿后做人工修订或关键事实变更，核对联动与语义检查；
* model：用脚本化模型模拟虚构、越权等不当输出，核对校验器是否拦住。

变体：full（全部模块）、no_<模块>（逐项消融）、minimal（全部关闭，作为“无约束模板起草”基线）。
所有结果只说明“在这些合成用例上”的表现，不能外推为真实办文质量；人工评测须另行组织。
"""

from __future__ import annotations

import json
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from ..schemas.common import Clearance

CASES_DIR = Path(__file__).parent / "cases"
FLAGS = ["fact_ledger", "temporal_check", "independent_review", "targeted_revision", "consistency_check", "burden_check"]
GROUPS = {"genre_routing": "文种与行文", "facts_basis": "事实与依据", "semantic_revision": "语义与修订", "format_layout": "格式与版式"}
# 设计 §9.2 的任务组（600 例目标的四组构成）；用例同时按风险维度（GROUPS）和任务组统计
TASK_GROUPS = {"single": "高频单文稿", "multi": "多材料和跨文件", "temporal": "时间与政策适用", "adversarial": "不完整、冲突与对抗"}
DIRECT = "direct_llm"  # 基线：强模型直接写作（无状态机、无事实账本、无审校），须配置模型


def variants(ablate: list[str] | None = None, baselines: bool = False, direct: bool = False) -> dict[str, dict[str, bool]]:
    out: dict[str, dict[str, bool]] = {"full": {}}
    for f in ablate or []:
        if f not in FLAGS:
            raise ValueError(f"未知模块：{f}（可选：{'、'.join(FLAGS)}）")
        out[f"no_{f}"] = {f: False}
    if baselines:
        out["minimal"] = {f: False for f in FLAGS}
    if direct:
        out[DIRECT] = {}
    return out


def load_cases(path: str | Path | None = None) -> list[dict[str, Any]]:
    p = Path(path) if path else CASES_DIR
    files = sorted(p.glob("*.yaml")) if p.is_dir() else [p]
    cases: list[dict[str, Any]] = []
    for f in files:
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or []
        for c in data:
            c.setdefault("group", f.stem)
            cases.append(c)
    ids = [c["id"] for c in cases]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"用例编号重复：{sorted(dup)}")
    return cases


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class CaseResult:
    case_id: str
    group: str
    kind: str
    title: str
    variant: str
    passed: bool
    checks: list[Check] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0
    error: str = ""
    task_group: str = ""
    skipped: str = ""  # 非空表示该变体不适用于此用例（如直接写作基线不做准入用例）或缺少条件
    draft: str = ""  # 最终文稿（Markdown），用于盲评导出


# ------------------------------------------------------------------ 脚本化模型（不当输出）
def _drafting_responder(mutate: Callable[[str], str]):
    def responder(messages, system, tools, schema):
        if schema and "paragraphs" in schema.get("properties", {}):
            task = json.loads(messages[0].content.split("待改写段落：\n", 1)[1])
            return {"paragraphs": [{"para_id": p["para_id"], "sentences": [{"text": mutate(p["draft"]), "refs": p["refs"]}]} for p in task]}
        if schema and "issues" in schema.get("properties", {}):
            return {"issues": []}
        if schema and "facts" in schema.get("properties", {}):
            return {"facts": []}
        return {"purposes": [], "issuer": None, "recipients": [], "subject": None}

    return responder


def _fabricate(text: str) -> str:
    text = text.replace("拟新建", "已新建").replace("计划于", "已于")
    return text + "累计投入资金300万元。" if "示范点" in text else text


def _approval(text: str) -> str:
    return ("经市政府研究同意，" + text) if text.startswith(("2026", "拟")) else text


def _strengthen(text: str) -> str:
    return text.replace("可以", "必须").replace("原则上", "")


def _rewrite_mix(messages, system, tools, schema):
    """改写场景：一条合规改写、一条弱化义务、一条加审批说法、一条加无来源数字、一条改固定句。"""
    props = (schema or {}).get("properties", {})
    if "keep" in props:
        rows = json.loads(messages[0].content.split("未锁定的句子：\n", 1)[1])
        return {"keep": [{"sid": r["sid"], "reason": "职责分工表述"} for r in rows if "负责" in r["text"]]}
    if "edits" in props and "改写要求" in messages[0].content:
        rows = json.loads(messages[0].content.split("文稿逐句（按顺序）：\n", 1)[1])
        edits = []
        for r in rows:
            t = r["text"]
            if "负责统筹协调和技术指导" in t:
                edits.append({"sid": r["sid"], "new_text": t.replace("负责统筹协调和技术指导", "负责统筹协调，并做好技术指导工作"), "reason": "语句更顺"})
            elif "要在" in t:
                edits.append({"sid": r["sid"], "new_text": t.replace("要在", "可以在"), "reason": "语气缓和"})
            elif t.startswith("现就"):
                edits.append({"sid": r["sid"], "new_text": "经市政府同意，" + t, "reason": "增强权威"})
            elif "组织验收" in t:
                edits.append({"sid": r["sid"], "new_text": t.replace("组织验收", "组织验收，投入资金500万元"), "reason": "补充"})
            elif t == "特此通知。":
                edits.append({"sid": r["sid"], "new_text": "特此通知，请遵照执行。", "reason": "加强要求"})
        return {"edits": edits}
    return _drafting_responder(lambda x: x)(messages, system, tools, schema)


RESPONDERS: dict[str, Callable] = {
    "rewrite_mix": _rewrite_mix,
    "fabricate": _drafting_responder(_fabricate),
    "approval_claim": _drafting_responder(_approval),
    "strengthen": _drafting_responder(_strengthen),
}


def _refusal_provider():
    from ..llm import ScriptedProvider
    from ..llm.base import ModelResponse

    return ScriptedProvider(responder=lambda *a: ModelResponse(stop_reason="refusal", refused=True), endpoint="http://localhost/scripted")


# ------------------------------------------------------------------ 执行
def _engine(case: dict[str, Any], workdir: Path, flags: dict[str, bool]):
    from ..orchestrator import Engine
    from ..runtime import build_runtime

    providers = None
    model = case.get("model")
    if model == "refuse":
        providers = {"*": _refusal_provider()}
    elif model:
        from ..llm import ScriptedProvider

        providers = {"*": ScriptedProvider(responder=RESPONDERS[model], endpoint="http://localhost/scripted")}
    env = {"unit_name": "示例市卫生健康委员会", "region": "示例省", **(case.get("env") or {})}
    overrides = {
        "environment": {**env, "data_dir": str(workdir / ".gongwen")},
        "layout": {"render_check": bool(case.get("render", False))},
        "features": dict(flags),
    }
    extra = case.get("extra_policies") or []
    rt = build_runtime(workdir, overrides=overrides, model_providers=providers, allow_synthetic_policies=bool(extra))
    if extra:
        from ..schemas.policy import PolicyDocument

        for p in extra:  # 用例自带的合成依据：标记为示例数据，只在评测环境参与判断
            rt.policies.add(PolicyDocument.model_validate({**p, "synthetic": True}))
    return Engine(rt, providers)


def _clearance(v: str | None) -> Clearance | None:
    return next((c for c in Clearance if c.value == v), None) if v else None


def _materials(eng, task_id: str, case: dict[str, Any], user) -> list:
    out = []
    for m in case.get("materials") or []:
        if "rows" in m:
            from io import BytesIO

            from openpyxl import Workbook

            wb = Workbook()
            ws = wb.active
            ws.title = m.get("sheet", "Sheet1")
            for r in m["rows"]:
                ws.append(r)
            buf = BytesIO()
            wb.save(buf)
            data = buf.getvalue()
        elif "docx" in m:
            data = _docx_bytes(m["docx"])
        else:
            data = str(m["text"]).encode("utf-8")
        out.append(eng.add_material(task_id, m["name"], data, by=user, declared=_clearance(m.get("clearance", "公开"))))
    return out


def _docx_bytes(spec: dict[str, Any]) -> bytes:
    """用例中的 DOCX 材料：可见段落 + 隐藏文字 + 批注式备注（用于测试隐藏内容不进入事实账本）。"""
    from io import BytesIO

    from docx import Document

    doc = Document()
    for t in spec.get("paragraphs", []):
        doc.add_paragraph(t)
    for t in spec.get("hidden", []):
        run = doc.add_paragraph().add_run(t)
        run.font.hidden = True
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _human_loop(eng, task_id: str, case: dict[str, Any], user, max_rounds: int = 12):
    """按用例中的 human 脚本处理审核节点；未列出的节点保持待处理（模拟人尚未处理）。"""
    from ..schemas.facts import FactStatus

    actions = {a["at"]: a for a in case.get("human") or []}
    accept = set(case.get("accept") or [])
    st = eng.advance(task_id, by=user, auto_accept=accept)
    for _ in range(max_rounds):
        pend = st.pending_checkpoints()
        if not pend:
            break
        cp = pend[0]
        act = next((a for k, a in actions.items() if k == cp.kind.value or k == cp.kind.name.lower()), None)
        if act is None:
            break
        data = dict(act.get("data") or {})
        if act.get("confirm_money_facts"):
            ledger = eng.load_matter_ledger(st)
            data["confirm_facts"] = [f.fact_id for f in ledger.facts if f.kind == "money" and f.status == FactStatus.RECORDED] if ledger else []
        if act.get("confirm_all_materials"):
            data["materials"] = {m.material_id: "公开" for m in eng.rt.materials.list(st.matter_id)}
        eng.resolve_checkpoint(task_id, cp.cp_id, act["option"], by=user, data=data)
        actions.pop(next(k for k, a in actions.items() if a is act))
        st = eng.advance(task_id, by=user, auto_accept=accept)
    return st


def _revise(eng, task_id: str, rev: dict[str, Any], user):
    st = eng.load_state(task_id)
    ir = eng.current_ir(st)
    edits = []
    for e in rev.get("edits") or []:
        hit = next(((b, s) for b, s in ir.iter_sentences() if e["find"] in s.text), None)
        if hit is None:
            raise ValueError(f"修订脚本找不到句子：{e['find']}")
        edits.append({"sid": hit[1].sid, "text": hit[1].text.replace(e["find"], e["replace"])})
    facts = []
    ledger = eng.load_matter_ledger(st)
    for fc in rev.get("fact_changes") or []:
        f = next((x for x in ledger.facts if x.attribute.startswith(fc["attribute"])), None)
        if f is None:
            raise ValueError(f"修订脚本找不到事实：{fc['attribute']}")
        facts.append({"fact_id": f.fact_id, "new_value": fc["new_value"], "reason": fc.get("reason", "评测")})
    if rev.get("approve_first"):
        cp = next(c for c in st.pending_checkpoints() if c.kind.name == "HUMAN_REVIEW")
        eng.resolve_checkpoint(task_id, cp.cp_id, "submit", by=user)
        from ..harness.permissions import human

        eng.import_approval(task_id, by=human("eval-approver", "approver"), approver="评测签批人", approved_at="2026年10月8日", scope="同意", source="EVAL-1")
    eng.request_revision(task_id, by=user, edits=edits, fact_changes=facts)
    return eng.advance(task_id, by=user, auto_accept=set(rev.get("accept") or ["review_escalation"]))


def _rewrite(eng, task_id: str, rw: dict[str, Any], user):
    """按用例脚本锁定、改写并（可选）采纳：apply 为 safe/all/none。"""
    if rw.get("lock") or rw.get("unlock"):
        eng.set_sentence_locks(task_id, by=user, lock=[s for s in _find_sids(eng, task_id, rw.get("lock") or [])], unlock=[s for s in _find_sids(eng, task_id, rw.get("unlock") or [])])
    r = eng.rewrite(task_id, by=user, prompt=rw["prompt"], ai_calibrate=bool(rw.get("ai_calibrate")))
    if rw.get("apply") in ("safe", "all") and r["status"] == "pending":
        eng.apply_rewrite(task_id, r["rewrite_id"], by=user, include_flagged=rw["apply"] == "all")
        return eng.advance(task_id, by=user, auto_accept=set(rw.get("accept") or ["review_escalation"]))
    return eng.load_state(task_id)


def _find_sids(eng, task_id: str, needles: list[str]) -> list[str]:
    """用例按句子内容指定锁定对象（句号随起草而变）。"""
    locks = eng.sentence_locks(task_id)
    out = []
    for n in needles:
        hit = next((lk["sid"] for lk in locks if n in lk["text"]), None)
        if hit is None:
            raise ValueError(f"改写脚本找不到句子：{n}")
        out.append(hit)
    return out


def _expect_pipeline(eng, st, exp: dict[str, Any], admissions: list) -> tuple[list[Check], dict[str, Any]]:
    from ..schemas.genre import GenreDecision
    from ..schemas.review import ReviewReport

    checks: list[Check] = []
    add = lambda name, ok, detail="": checks.append(Check(name, bool(ok), detail))
    st = eng.load_state(st.task_id)
    ir = eng.current_ir(st)
    text = ""
    if ir:
        tables = "\n".join(" ".join(" ".join(r) for r in (b.table or [])) for _, b in ir.iter_blocks() if b.kind == "table")
        text = ir.full_text() + "\n" + tables
    report = eng.store.load_model(st.task_id, "review_report", ReviewReport)
    rules_all = {i.rule.rule_id for i in (report.issues if report else []) if i.rule}
    rules_open = {i.rule.rule_id for i in (report.open_issues() if report else []) if i.rule}
    g = eng.store.load_model(st.task_id, "genre_decision", GenreDecision)
    cps = st.checkpoints
    metrics: dict[str, Any] = {}
    if ir:
        nums = [s for _, s in ir.iter_sentences() if re.search(r"\d", s.text)]
        metrics["numeric_sentences"] = len(nums)
        metrics["unsourced_numeric_sentences"] = sum(1 for s in nums if not s.refs)
    metrics["final_stage"] = st.stage.value
    metrics["auto_passed_human_review"] = any(c.kind.name == "HUMAN_REVIEW" and c.status == "resolved" and (c.resolution or {}).get("note", "").startswith("无头模式") for c in cps)
    for k, v in exp.items():
        if k == "stage":
            add("stage", st.stage.value == v, f"实际 {st.stage.value}")
        elif k == "genre":
            actual = (ir.genre or ir.material_type) if ir else (g.genre if g else None)
            add("genre", actual == v, f"实际 {actual}")
        elif k == "title_suffix":
            add("title_suffix", bool(ir) and ir.title.endswith(v), ir.title if ir else "无文稿")
        elif k == "doc_status":
            add("doc_status", st.doc_status.value == v, f"实际 {st.doc_status.value}")
        elif k == "draft_contains":
            for s in v:
                add(f"contains:{s}", s in text)
        elif k == "draft_not_contains":
            for s in v:
                ok = s not in text
                add(f"not_contains:{s}", ok)
                metrics["leaks"] = metrics.get("leaks", 0) + (0 if ok else 1)
        elif k == "numbers_sourced":
            add("numbers_sourced", metrics.get("unsourced_numeric_sentences", 1) == 0, str(metrics.get("unsourced_numeric_sentences")))
        elif k == "checkpoint_kinds":
            kinds = {c.kind.value for c in cps}
            for x in v:
                add(f"checkpoint:{x}", x in kinds, "、".join(sorted(kinds)))
        elif k == "pending_kinds":
            kinds = {c.kind.value for c in st.pending_checkpoints()}
            for x in v:
                add(f"pending:{x}", x in kinds, "、".join(sorted(kinds)))
        elif k == "checkpoint_text":
            blob = "\n".join(c.question + "\n" + "\n".join(c.details) for c in cps)
            for x in v:
                add(f"checkpoint_text:{x}", x in blob)
        elif k == "authority_codes":
            codes = {f.code for f in (g.authority_findings if g else [])}
            for x in v:
                add(f"authority:{x}", x in codes, "、".join(sorted(codes)))
        elif k == "rewrite_counts":
            rr = eng.rewrite_result(st.task_id) or {"counts": {}}
            for status, n in v.items():
                add(f"rewrite:{status}", rr["counts"].get(status, 0) == n, f"实际 {rr['counts'].get(status, 0)}")
        elif k == "rewrite_reasons":
            rr = eng.rewrite_result(st.task_id) or {"patches": []}
            blob = "\n".join(f"{p['status']}:{p['reason']}" for p in rr["patches"])
            for x in v:
                add(f"rewrite_reason:{x}", x in blob, blob[:200])
        elif k == "rewrite_notes":
            rr = eng.rewrite_result(st.task_id) or {"notes": []}
            blob = "\n".join(rr.get("notes", []))
            for x in v:
                add(f"rewrite_note:{x}", x in blob, blob[:200])
        elif k == "rewrite_status":
            rr = eng.rewrite_result(st.task_id) or {}
            add("rewrite_status", rr.get("status") == v, f"实际 {rr.get('status')}")
        elif k == "max_occurrences":
            for phrase, n in v.items():
                add(f"max_occurrences:{phrase}", text.count(phrase) <= n, f"出现 {text.count(phrase)} 次")
        elif k == "procedures":
            codes = {x.code for x in (g.procedures if g else [])}
            for x in v:
                add(f"procedure:{x}", x in codes, "、".join(sorted(codes)))
        elif k == "signature_organs":
            add("signature_organs", bool(ir) and ir.signature.organs == v, "、".join(ir.signature.organs) if ir else "无文稿")
        elif k == "alternatives_include":
            alts = set(g.alternatives if g else [])
            for x in v:
                add(f"alternative:{x}", x in alts, "、".join(sorted(alts)))
        elif k == "rules_present":
            for r in v:
                add(f"rule:{r}", r in rules_all, "、".join(sorted(rules_all)))
            metrics["expected_rules"] = len(v)
            metrics["found_rules"] = sum(1 for r in v if r in rules_all)
        elif k == "rules_open":
            for r in v:
                add(f"open:{r}", r in rules_open, "、".join(sorted(rules_open)))
        elif k == "rules_absent_open":
            for r in v:
                add(f"absent_open:{r}", r not in rules_open)
        elif k == "conflicts":
            ledger = eng.load_matter_ledger(st)
            n = len(ledger.conflicts) if ledger else 0
            add("conflicts", (n > 0) == bool(v), f"冲突 {n} 项")
        elif k == "calc_failures":
            ledger = eng.load_matter_ledger(st)
            n = sum(1 for c in (ledger.calc_checks if ledger else []) if not c.ok)
            add("calc_failures", (n > 0) == bool(v), f"复算不一致 {n} 项")
        elif k == "errors_contain":
            for x in v:
                add(f"error:{x}", any(x in e for e in st.errors), "；".join(st.errors))
        elif k == "admission":
            dec = [a.decision.value for a in admissions]
            add("admission", dec == v, "、".join(dec))
        elif k == "log_events":
            types = {r["type"] for r in eng.log(st.task_id).replay()}
            for x in v:
                add(f"event:{x}", x in types)
        elif k == "finding_codes":
            continue  # 准入用例在 run_case 中单独核对
        elif k in ("section_contains", "section_not_contains"):
            secs = _sections(ir) if ir else {}
            for heading, items in v.items():
                body = secs.get(heading)
                if body is None:
                    add(f"section:{heading}", False, "无此章节：" + "、".join(secs))
                    continue
                for x in items:
                    ok = (x in body) if k == "section_contains" else (x not in body)
                    add(f"{'in' if k == 'section_contains' else 'not_in'}:{heading}:{x}", ok, body[:60])
        elif k == "max_occurrences":
            for x, n in v.items():
                cnt = text.count(x)
                add(f"occurrences:{x}", cnt <= n, f"{cnt} 次")
        elif k == "attachment_text_contains":
            att_text = " ".join(b.text() if b.kind != "table" else " ".join(" ".join(r) for r in (b.table or [])) for a in (ir.attachments if ir else []) for b in a.blocks)
            for x in v:
                add(f"attachment_text:{x}", x in att_text, att_text[:80])
        elif k == "attachment_table_contains":
            cells = " ".join(" ".join(r) for a in (ir.attachments if ir else []) for b in a.blocks if b.table for r in b.table)
            for x in v:
                add(f"attachment:{x}", x in cells, cells[:80])
        else:
            add(f"unknown_expectation:{k}", False, "用例写法错误")
    return checks, metrics


DIRECT_SYSTEM = (
    "你是中国内地公文写作助手。根据办文需求和材料，直接写出一份完整的公文（标题、主送机关、正文、落款），"
    "只输出公文文本，不要解释。"
)


def _material_text(case: dict[str, Any]) -> str:
    parts = []
    for m in case.get("materials") or []:
        if "rows" in m:
            body = "\n".join(",".join(str(c) for c in r) for r in m["rows"])
        elif "docx" in m:
            body = "\n".join(m["docx"].get("paragraphs", []))  # 隐藏文字不是可用来源
        else:
            body = str(m["text"])
        parts.append(f"【{m['name']}】\n{body}")
    return "\n\n".join(parts)


def _text_rows(text: str) -> list[list[str]]:
    """CSV 行或 Markdown 表格行（跳过 |---| 分隔行）。"""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not all(set(c) <= set("-: ") for c in cells):
                rows.append(cells)
        elif "," in line:
            rows.append(line.split(","))
    return rows


def _fabricated_numbers(case: dict[str, Any], text: str) -> list[str]:
    """输出中出现、而需求与材料中没有的数字（含由材料合计复算得到的数也视为有来源）。"""
    from ..rules.textutil import extract_numbers, is_subtotal_row, is_total_row

    source = case["request"] + "\n" + _material_text(case)
    nums = extract_numbers(source)
    known = {n.raw for n in nums} | {float(n.value) for n in nums if isinstance(n.value, (int, float))}
    changes = {fc["attribute"]: fc["new_value"] for fc in (case.get("revise") or {}).get("fact_changes", [])}
    known |= {float(v) for v in changes.values() if isinstance(v, (int, float))}  # 人工给出的更正值
    for m in case.get("materials") or []:  # 表格列合计：系统会复算（含人工更正后的复算），复算值不算虚构
        rows = [list(r) for r in (m.get("rows") or _text_rows(str(m.get("text", ""))))]
        for r in rows[1:]:
            for attr, v in changes.items():
                if r and str(r[0]).startswith(attr) and len(r) > 1:
                    r[1] = v
        for c in range(0, max((len(r) for r in rows), default=0)):
            vals = []
            for r in rows[1:]:
                try:
                    v = float(str(r[c]).replace(",", "")) if c < len(r) else None
                except ValueError:
                    continue
                known.add(v)  # 表格单元格本身就是材料中的数
                if not is_total_row([str(x) for x in r]) and not is_subtotal_row([str(x) for x in r]):
                    vals.append(v)
            if vals:
                known.add(float(sum(vals)))
    out = []
    for n in extract_numbers(text):
        v = float(n.value) if isinstance(n.value, (int, float)) else None
        if n.raw not in known and (v is None or v not in known):
            out.append(n.raw)
    return out


def _sections(ir) -> dict[str, str]:
    """一级标题 → 该节正文（用于核对内容是否落在正确的章节）。"""
    out: dict[str, str] = {}
    cur = None
    for b in ir.blocks:
        if b.kind == "heading" and b.level == 1:
            cur = b.heading
            out[cur] = "".join(s.text for s in b.sentences)
        elif cur is not None:
            out[cur] += b.text()
    return out


def run_direct(case: dict[str, Any], workdir: Path, providers: dict | None = None) -> CaseResult:
    """基线：把需求和材料直接交给模型写成稿，再用同一套检查与期望核对（只核对适用的期望）。"""
    from ..importer import check_external, ir_from_text
    from ..llm.base import ChatMessage

    res = CaseResult(case["id"], case["group"], case["kind"], case.get("title", ""), DIRECT, False, task_group=case.get("task_group", ""))
    if case["kind"] in ("check", "admission", "matter"):
        res.skipped = "不适用：该用例不涉及单篇起草" if case["kind"] != "matter" else "不适用：多文稿事项用例"
        return res
    eng = _engine({**case, "model": None}, workdir, {})
    router = eng.rt.router(providers=providers)
    if router.provider("heavy") is None:
        res.skipped = "未配置模型：直接写作基线不可运行"
        return res
    hints = case.get("hints") or {}
    prompt = f"办文需求：{case['request']}\n主送机关：{hints.get('recipients', '未指定')}\n发文机关：{eng.rt.config.environment.unit_name or '未指定'}\n\n材料：\n{_material_text(case)}"
    resp = router.call("heavy", [ChatMessage("user", prompt)], system=DIRECT_SYSTEM, clearances=[Clearance.PUBLIC], purpose="baseline_direct", template_id="baseline.direct.v1")
    text = resp.text.strip()
    res.draft = text
    ir = ir_from_text(text)
    exp = case.get("expect") or {}
    if "genre" in exp:
        actual = ir.genre or ir.material_type
        res.checks.append(Check("genre", actual == exp["genre"], f"实际 {actual}"))
    if "title_suffix" in exp:
        res.checks.append(Check("title_suffix", ir.title.endswith(exp["title_suffix"]), ir.title))
    for s_ in exp.get("draft_contains", []):
        res.checks.append(Check(f"contains:{s_}", s_ in text))
    leaks = 0
    for s_ in exp.get("draft_not_contains", []):
        ok = s_ not in text
        leaks += 0 if ok else 1
        res.checks.append(Check(f"not_contains:{s_}", ok))
    fabricated = _fabricated_numbers(case, ir.body_text())
    res.metrics.update({"leaks": leaks, "fabricated_numbers": len(fabricated), "fabricated_examples": fabricated[:5]})
    ex = check_external(ir, eng.rt)
    res.metrics["issues_major_plus"] = sum(1 for i in ex.issues if i.severity.rank >= 3)
    res.checks.append(Check("no_fabricated_numbers", not fabricated, "、".join(fabricated[:5])))
    return res


def run_case(case: dict[str, Any], variant: str, flags: dict[str, bool], providers: dict | None = None) -> CaseResult:
    if variant == DIRECT:
        t0 = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="gongwen-eval-") as tmp:
            try:
                r = run_direct(case, Path(tmp), providers)
            except Exception as exc:
                r = CaseResult(case["id"], case["group"], case["kind"], case.get("title", ""), DIRECT, False, task_group=case.get("task_group", ""), error=f"{type(exc).__name__}: {exc}")
        r.seconds = round(time.perf_counter() - t0, 3)
        r.passed = not r.skipped and not r.error and bool(r.checks) and all(c.ok for c in r.checks)
        return r
    return _run_case(case, variant, flags)


def _run_case(case: dict[str, Any], variant: str, flags: dict[str, bool]) -> CaseResult:
    from ..orchestrator import default_user

    res = CaseResult(case["id"], case["group"], case["kind"], case.get("title", ""), variant, False, task_group=case.get("task_group", ""))
    t0 = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix="gongwen-eval-") as tmp:
            work = Path(tmp)
            kind = case["kind"]
            if kind == "check":
                from ..importer import check_external, ir_from_text

                eng = _engine(case, work, flags)
                ir = ir_from_text(case["text"], genre=case.get("genre"))
                ex = check_external(ir, eng.rt, as_of=date.fromisoformat(case["as_of"]) if case.get("as_of") else None, region=case.get("region"))
                rules = {i.rule.rule_id for i in ex.issues if i.rule}
                exp = case.get("expect") or {}
                for r in exp.get("rules_present", []):
                    res.checks.append(Check(f"rule:{r}", r in rules, "、".join(sorted(rules))))
                for r in exp.get("rules_absent", []):
                    res.checks.append(Check(f"absent:{r}", r not in rules))
                for sev, n in (exp.get("max_issues") or {}).items():
                    cnt = sum(1 for i in ex.issues if i.severity.value == sev)
                    res.checks.append(Check(f"max:{sev}", cnt <= n, f"{cnt} 项：" + "；".join(f"{i.rule.rule_id if i.rule else ''}{i.suggestion[:20]}" for i in ex.issues if i.severity.value == sev)))
                if exp.get("genre"):
                    res.checks.append(Check("genre", (ir.genre or ir.material_type) == exp["genre"], str(ir.genre or ir.material_type)))
                expected = exp.get("rules_present", [])
                res.metrics.update(
                    {
                        "expected_rules": len(expected),
                        "found_rules": sum(1 for r in expected if r in rules),
                        "control": bool(case.get("control")),
                        "issues_major_plus": sum(1 for i in ex.issues if i.severity.rank >= 3),
                        "unexpected_major_plus": sum(1 for i in ex.issues if i.severity.rank >= 3 and (not i.rule or i.rule.rule_id not in expected)),
                    }
                )
            elif kind == "matter":
                # 同一事项的多份文稿：依次办理，可在其中一份上做修订，期望在指定任务上核对
                eng = _engine(case, work, flags)
                user = default_user("eval")
                states, admissions, matter = [], [], None
                for t in case["tasks"]:
                    st = eng.create_task(t["request"], by=user, matter_id=matter, hints=t.get("hints") or {})
                    matter = st.matter_id
                    admissions += _materials(eng, st.task_id, t, user)
                    states.append(_human_loop(eng, st.task_id, t, user))
                if case.get("revise"):
                    rv = case["revise"]
                    _revise(eng, states[rv.get("task", 0)].task_id, rv, user)
                target = eng.load_state(states[case.get("expect_task", -1)].task_id)
                checks, metrics = _expect_pipeline(eng, target, case.get("expect") or {}, admissions)
                res.checks += checks
                res.metrics.update(metrics)
                ir_final = eng.current_ir(target)
                if ir_final is not None:
                    res.draft = ir_final.to_markdown()
            else:
                eng = _engine(case, work, flags)
                user = default_user("eval")
                st = eng.create_task(case["request"], by=user, hints=case.get("hints") or {})
                admissions = _materials(eng, st.task_id, case, user)
                if kind == "admission":
                    st = eng.advance(st.task_id, by=user)
                else:
                    st = _human_loop(eng, st.task_id, case, user)
                    if kind == "revision":
                        st = _revise(eng, st.task_id, case["revise"], user)
                    elif kind == "rewrite":
                        st = _rewrite(eng, st.task_id, case["rewrite"], user)
                checks, metrics = _expect_pipeline(eng, st, case.get("expect") or {}, admissions)
                res.checks += checks
                res.metrics.update(metrics)
                ir_final = eng.current_ir(eng.load_state(st.task_id))
                if ir_final is not None:
                    res.draft = ir_final.to_markdown()
                    fab = _fabricated_numbers(case, ir_final.body_text(include_attachments=True))
                    res.metrics["fabricated_numbers"] = len(fab)
                    res.metrics["fabricated_examples"] = fab[:5]
                if kind == "admission":
                    exp = case.get("expect") or {}
                    codes = {f.code for a in admissions for f in a.findings}
                    for c in exp.get("finding_codes", []):
                        res.checks.append(Check(f"finding:{c}", c in codes, "、".join(sorted(codes))))
                    res.metrics["blocked"] = any(a.decision.value == "禁止进入当前环境" for a in admissions)
    except Exception as exc:  # 评测中任何异常都计为失败并记录，不吞掉
        res.error = f"{type(exc).__name__}: {exc}"
    res.seconds = round(time.perf_counter() - t0, 3)
    res.passed = not res.error and bool(res.checks) and all(c.ok for c in res.checks)
    return res


# ------------------------------------------------------------------ 汇总
def summarize(results: list[CaseResult]) -> dict[str, Any]:
    by_var: dict[str, list[CaseResult]] = {}
    for r in results:
        by_var.setdefault(r.variant, []).append(r)
    out: dict[str, Any] = {}
    for v, rs_all in by_var.items():
        rs = [r for r in rs_all if not r.skipped]
        if not rs:
            out[v] = {"cases": 0, "passed": 0, "pass_rate": 0, "skipped": sorted({r.skipped for r in rs_all}), "by_group": {}, "by_task_group": {},
                      "planted_issue_recall": None, "control_false_alarms_major_plus": 0, "unsourced_numeric_sentence_rate": None, "content_leaks": 0,
                      "fabricated_numbers": 0, "human_review_auto_passed": 0, "admission_as_expected": "0/0", "errors": [], "seconds": 0}
            continue
        checks = [r for r in rs if r.kind == "check" and not r.metrics.get("control")]
        controls = [r for r in rs if r.metrics.get("control")]
        pipes = [r for r in rs if r.kind in ("pipeline", "revision", "model", "rewrite")]
        exp = sum(r.metrics.get("expected_rules", 0) for r in checks)
        found = sum(r.metrics.get("found_rules", 0) for r in checks)
        nums = sum(r.metrics.get("numeric_sentences", 0) for r in pipes)
        uns = sum(r.metrics.get("unsourced_numeric_sentences", 0) for r in pipes)
        out[v] = {
            "cases": len(rs),
            "passed": sum(r.passed for r in rs),
            "pass_rate": round(sum(r.passed for r in rs) / len(rs), 3) if rs else 0,
            "by_group": {GROUPS.get(g, g): f"{sum(r.passed for r in rs if r.group == g)}/{sum(1 for r in rs if r.group == g)}" for g in sorted({r.group for r in rs})},
            "by_task_group": {TASK_GROUPS.get(g, g): f"{sum(r.passed for r in rs if r.task_group == g)}/{sum(1 for r in rs if r.task_group == g)}" for g in TASK_GROUPS if any(r.task_group == g for r in rs)},
            "skipped": len(rs_all) - len(rs),
            "fabricated_numbers": sum(r.metrics.get("fabricated_numbers", 0) for r in rs),
            "planted_issue_recall": round(found / exp, 3) if exp else None,
            "control_false_alarms_major_plus": sum(r.metrics.get("issues_major_plus", 0) for r in controls),
            "unsourced_numeric_sentence_rate": round(uns / nums, 3) if nums else None,
            "content_leaks": sum(r.metrics.get("leaks", 0) for r in pipes),
            "human_review_auto_passed": sum(1 for r in pipes if r.metrics.get("auto_passed_human_review")),
            "admission_as_expected": f"{sum(1 for r in rs if r.kind == 'admission' and r.passed)}/{sum(1 for r in rs if r.kind == 'admission')}",
            "errors": [f"{r.case_id}: {r.error}" for r in rs if r.error],
            "seconds": round(sum(r.seconds for r in rs), 2),
        }
    if "full" in by_var:
        full = {r.case_id: r.passed for r in by_var["full"]}
        for v, rs in by_var.items():
            if v != "full":
                out[v]["lost_vs_full"] = sorted(r.case_id for r in rs if full.get(r.case_id) and not r.passed and not r.skipped)
    return out


def to_markdown(summary: dict[str, Any], results: list[CaseResult]) -> str:
    lines = ["# 公文智能体评测报告", "", f"生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}", ""]
    lines += ["## 总览", "", "| 变体 | 通过 | 通过率 | 问题检出率 | 对照误报（重要以上） | 无来源数字句占比 | 内容泄漏 | 人工送审被自动通过 | 准入判定符合预期 |", "|---|---|---|---|---|---|---|---|---|"]
    for v, s in summary.items():
        if not s["cases"]:
            lines.append(f"| {v} | 未运行 | — | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| {v} | {s['passed']}/{s['cases']} | {s['pass_rate']:.0%} | {s['planted_issue_recall'] if s['planted_issue_recall'] is not None else '—'} | {s['control_false_alarms_major_plus']} | {s['unsourced_numeric_sentence_rate'] if s['unsourced_numeric_sentence_rate'] is not None else '—'} | {s['content_leaks']} | {s['human_review_auto_passed']} | {s['admission_as_expected']} |"
        )
    lines += ["", "## 分组通过情况（风险维度）", ""]
    for v, s in summary.items():
        lines.append(f"- **{v}**：" + ("；".join(f"{g} {x}" for g, x in s["by_group"].items()) or f"未运行（{'；'.join(s.get('skipped') or []) if isinstance(s.get('skipped'), list) else ''}）"))
    lines += ["", "## 分组通过情况（设计 §9.2 任务组）", ""]
    for v, s in summary.items():
        if s.get("by_task_group"):
            lines.append(f"- **{v}**：" + "；".join(f"{g} {x}" for g, x in s["by_task_group"].items()))
    lines += ["", "## 虚构数字（文稿中出现、需求与材料中都没有、也不是材料复算结果的数字）", ""]
    for v, s in summary.items():
        lines.append(f"- {v}：{s.get('fabricated_numbers', 0) if s['cases'] else '未运行'}")
    abl = {v: s.get("lost_vs_full") for v, s in summary.items() if s.get("lost_vs_full") is not None and s["cases"] and v != DIRECT}
    if abl:
        lines += ["", "## 消融：关闭模块后由通过变为失败的用例", ""]
        for v, ids in abl.items():
            lines.append(f"- {v}：{'、'.join(ids) if ids else '无变化（现有用例未覆盖该模块的作用，需补充用例）'}")
    failed = [r for r in results if not r.passed and r.variant == "full"]
    if failed:
        lines += ["", "## full 变体未通过的用例", ""]
        for r in failed:
            bad = [f"{c.name}（{c.detail}）" for c in r.checks if not c.ok]
            lines.append(f"- {r.case_id} {r.title}：{r.error or '；'.join(bad)}")
    lines += [
        "",
        "## 解读限制",
        "",
        "- 用例为合成材料，覆盖的是已知风险点的回归，不代表真实办文分布；",
        "- 默认离线确定性路径；脚本化模型只模拟特定不当输出，不能代表真实模型的整体质量；",
        "- 文风、可读性、领导意图契合度等需要人工评测（双人盲评、一致性检验），本报告不涉及。",
    ]
    return "\n".join(lines) + "\n"


def run_suite(cases: list[dict[str, Any]], var: dict[str, dict[str, bool]], progress: Callable[[CaseResult], None] | None = None, providers: dict | None = None) -> tuple[list[CaseResult], dict[str, Any]]:
    results = []
    for vname, flags in var.items():
        for c in cases:
            r = run_case(c, vname, flags, providers)
            results.append(r)
            if progress:
                progress(r)
    return results, summarize(results)


def export_blind_review(results: list[CaseResult], out: Path, seed: int | None = None) -> int:
    """设计 §9.5：导出隐藏系统名称的评审稿与评分表；对应关系单独保存，评审完成前不应交给评审人。"""
    import csv
    import random

    items = [r for r in results if r.draft and r.variant in ("full", DIRECT)]  # 完整方案与“直接写作”基线对比
    rng = random.Random(seed)
    rng.shuffle(items)
    sheets = out / "blind_review"
    sheets.mkdir(parents=True, exist_ok=True)
    key_rows = []
    for n, r in enumerate(items, 1):
        rid = f"R{n:03d}"
        (sheets / f"{rid}.md").write_text(f"# 评审稿 {rid}\n\n办文任务：{r.title}\n\n---\n\n{r.draft}\n", encoding="utf-8")
        key_rows.append({"稿件编号": rid, "用例": r.case_id, "变体": r.variant})
    with (out / "blind_review_key.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["稿件编号", "用例", "变体"])
        w.writeheader()
        w.writerows(key_rows)
    with (sheets / "评分表.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["稿件编号", "评审人", "重大问题数", "一般问题数", "轻微问题数", "可接受为送审稿（是/否）", "达到可接受稿预计修改用时（分钟）", "问题说明"])
        for row in key_rows:
            w.writerow([row["稿件编号"], "", "", "", "", "", "", ""])
    (sheets / "评审说明.md").write_text(
        "# 评审说明\n\n1. 每份稿件由两名专业人员独立评价，分歧由第三人裁定；\n"
        "2. 评审时不得查看 blind_review_key.csv；\n"
        "3. 问题分级：重大（文种或行文关系错误、虚构或错误事实、依据失效或不适用、越权表述、遗漏关键事项）、"
        "一般（结构或要素缺失、表述不准确、格式不规范）、轻微（标点、用词等）；\n"
        "4. 不以“像不像公文”打分；分别记录原始生成稿与人工修订后的结果。\n",
        encoding="utf-8",
    )
    return len(items)


def main(args) -> int:
    import sys

    cases = load_cases(args.cases)
    if args.limit:
        cases = cases[: args.limit]
    abl = FLAGS if (args.ablate or "") == "all" else [x for x in (args.ablate or "").split(",") if x]
    var = variants(abl, args.baselines, getattr(args, "direct", False))

    def progress(r: CaseResult) -> None:
        if not args.json:
            print(f"[{r.variant}] {r.case_id} {'通过' if r.passed else '未通过'} {r.title}", file=sys.stderr)

    results, summary = run_suite(cases, var, progress)
    out = Path(args.out) if args.out else Path(args.workspace or ".") / ".gongwen" / "eval" / datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    md = to_markdown(summary, results)
    (out / "report.md").write_text(md, encoding="utf-8")
    if getattr(args, "export_review", False):
        n = export_blind_review(results, out)
        md += f"\n已导出 {n} 份盲评稿：{out / 'blind_review'}（对应关系见 blind_review_key.csv，评审完成前不要交给评审人）\n"
    if args.json:
        print(json.dumps({"out": str(out), "summary": summary}, ensure_ascii=False))
    else:
        print(md)
        print(f"报告目录：{out}")
    full = summary.get("full", {})
    return 0 if full and full["passed"] == full["cases"] else 1


__all__ = ["DIRECT", "FLAGS", "GROUPS", "TASK_GROUPS", "export_blind_review", "load_cases", "run_case", "run_direct", "run_suite", "summarize", "to_markdown", "variants"]

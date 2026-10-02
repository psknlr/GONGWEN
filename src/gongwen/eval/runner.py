"""评测运行器：回归用例、基线对比与消融实验（设计 §10）。

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


def variants(ablate: list[str] | None = None, baselines: bool = False) -> dict[str, dict[str, bool]]:
    out: dict[str, dict[str, bool]] = {"full": {}}
    for f in ablate or []:
        if f not in FLAGS:
            raise ValueError(f"未知模块：{f}（可选：{'、'.join(FLAGS)}）")
        out[f"no_{f}"] = {f: False}
    if baselines:
        out["minimal"] = {f: False for f in FLAGS}
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


RESPONDERS: dict[str, Callable] = {
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
    rt = build_runtime(workdir, overrides=overrides, model_providers=providers)
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
        else:
            data = str(m["text"]).encode("utf-8")
        out.append(eng.add_material(task_id, m["name"], data, by=user, declared=_clearance(m.get("clearance", "公开"))))
    return out


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
        elif k == "attachment_table_contains":
            cells = " ".join(" ".join(r) for a in (ir.attachments if ir else []) for b in a.blocks if b.table for r in b.table)
            for x in v:
                add(f"attachment:{x}", x in cells, cells[:80])
        else:
            add(f"unknown_expectation:{k}", False, "用例写法错误")
    return checks, metrics


def run_case(case: dict[str, Any], variant: str, flags: dict[str, bool]) -> CaseResult:
    from ..orchestrator import default_user

    res = CaseResult(case["id"], case["group"], case["kind"], case.get("title", ""), variant, False)
    t0 = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix="gongwen-eval-") as tmp:
            work = Path(tmp)
            kind = case["kind"]
            if kind == "check":
                from ..importer import check_external, ir_from_text

                eng = _engine(case, work, flags)
                ir = ir_from_text(case["text"], genre=case.get("genre"))
                ex = check_external(ir, eng.rt, as_of=date.fromisoformat(case["as_of"]) if case.get("as_of") else None)
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
                checks, metrics = _expect_pipeline(eng, st, case.get("expect") or {}, admissions)
                res.checks += checks
                res.metrics.update(metrics)
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
    for v, rs in by_var.items():
        checks = [r for r in rs if r.kind == "check" and not r.metrics.get("control")]
        controls = [r for r in rs if r.metrics.get("control")]
        pipes = [r for r in rs if r.kind in ("pipeline", "revision", "model")]
        exp = sum(r.metrics.get("expected_rules", 0) for r in checks)
        found = sum(r.metrics.get("found_rules", 0) for r in checks)
        nums = sum(r.metrics.get("numeric_sentences", 0) for r in pipes)
        uns = sum(r.metrics.get("unsourced_numeric_sentences", 0) for r in pipes)
        out[v] = {
            "cases": len(rs),
            "passed": sum(r.passed for r in rs),
            "pass_rate": round(sum(r.passed for r in rs) / len(rs), 3) if rs else 0,
            "by_group": {GROUPS.get(g, g): f"{sum(r.passed for r in rs if r.group == g)}/{sum(1 for r in rs if r.group == g)}" for g in sorted({r.group for r in rs})},
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
                out[v]["lost_vs_full"] = sorted(r.case_id for r in rs if full.get(r.case_id) and not r.passed)
    return out


def to_markdown(summary: dict[str, Any], results: list[CaseResult]) -> str:
    lines = ["# 公文智能体评测报告", "", f"生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}", ""]
    lines += ["## 总览", "", "| 变体 | 通过 | 通过率 | 问题检出率 | 对照误报（重要以上） | 无来源数字句占比 | 内容泄漏 | 人工送审被自动通过 | 准入判定符合预期 |", "|---|---|---|---|---|---|---|---|---|"]
    for v, s in summary.items():
        lines.append(
            f"| {v} | {s['passed']}/{s['cases']} | {s['pass_rate']:.0%} | {s['planted_issue_recall'] if s['planted_issue_recall'] is not None else '—'} | {s['control_false_alarms_major_plus']} | {s['unsourced_numeric_sentence_rate'] if s['unsourced_numeric_sentence_rate'] is not None else '—'} | {s['content_leaks']} | {s['human_review_auto_passed']} | {s['admission_as_expected']} |"
        )
    lines += ["", "## 分组通过情况", ""]
    for v, s in summary.items():
        lines.append(f"- **{v}**：" + "；".join(f"{g} {x}" for g, x in s["by_group"].items()))
    abl = {v: s.get("lost_vs_full") for v, s in summary.items() if s.get("lost_vs_full") is not None}
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


def run_suite(cases: list[dict[str, Any]], var: dict[str, dict[str, bool]], progress: Callable[[CaseResult], None] | None = None) -> tuple[list[CaseResult], dict[str, Any]]:
    results = []
    for vname, flags in var.items():
        for c in cases:
            r = run_case(c, vname, flags)
            results.append(r)
            if progress:
                progress(r)
    return results, summarize(results)


def main(args) -> int:
    import sys

    cases = load_cases(args.cases)
    if args.limit:
        cases = cases[: args.limit]
    abl = FLAGS if (args.ablate or "") == "all" else [x for x in (args.ablate or "").split(",") if x]
    var = variants(abl, args.baselines)

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
    if args.json:
        print(json.dumps({"out": str(out), "summary": summary}, ensure_ascii=False))
    else:
        print(md)
        print(f"报告目录：{out}")
    full = summary.get("full", {})
    return 0 if full and full["passed"] == full["cases"] else 1


__all__ = ["FLAGS", "GROUPS", "load_cases", "run_case", "run_suite", "summarize", "to_markdown", "variants"]

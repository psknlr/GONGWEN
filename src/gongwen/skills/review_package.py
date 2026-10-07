"""技能12 送审打包与留痕：形成送审材料，维护版本和确认记录。

标准交付 = 文稿 + 事实依据表 + 规范检查结果 + 待确认事项 + 版本修改记录。
文稿状态：
* 讨论稿：允许存在明确标注的假设和待补材料；
* 送审稿：关键事实、依据和措施已经完成相应核验；
* 经批准的待印发版本：与真实审批记录绑定，不能由模型自行宣布形成。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..rules.registry import RULESET_VERSION
from ..schemas.common import DocStatus, Severity, sha256_bytes, stable_hash
from ..schemas.facts import FactLedger, FactStatus
from ..schemas.genre import GenreDecision
from ..schemas.ir import DocumentIR
from ..schemas.layout import LayoutReport, OutputFile
from ..schemas.outline import OutlinePlan
from ..schemas.package import EvidenceRow, PendingItem, ReviewPackage, VersionEntry
from ..schemas.patch import PatchSet
from ..schemas.policy import PolicyPack
from ..schemas.review import IssueType, ReviewReport
from ..schemas.sources import AdmissionResult
from ..schemas.state import Stage, TaskState
from ..schemas.task import TaskSpec
from ..workbench import build_page
from .base import Skill, SkillContext

KEY_KINDS = ("money", "count", "percent")


def assess_status(ir: DocumentIR, report: ReviewReport | None, ledger: FactLedger | None, policies: PolicyPack | None, state: TaskState) -> tuple[DocStatus, list[str]]:
    if any(a.doc_id == ir.doc_id and a.doc_version == ir.version and a.doc_hash == stable_hash(ir) for a in state.approvals):
        return DocStatus.APPROVED_FOR_ISSUE, ["已绑定真实审批记录（版本哈希一致）"]
    reasons: list[str] = []
    if report:
        blocking = report.open_issues(Severity.BLOCKING)
        if blocking:
            reasons.append(f"存在 {len(blocking)} 个阻断送审问题")
        major = [i for i in report.open_issues(Severity.MAJOR) if i.severity == Severity.MAJOR and i.type not in (IssueType.PROCEDURE,)]
        if major:
            reasons.append(f"存在 {len(major)} 个重要问题未处理或未经人工接受")
        ph = [i for i in report.open_issues() if i.type == IssueType.PLACEHOLDER and i.rule and i.rule.rule_id == "GW-PH-001"]
        if ph:
            reasons.append(f"存在 {len(ph)} 处内容性待补占位")
    else:
        reasons.append("尚未完成审校")
    if ledger:
        used = {r.id for _, s in ir.iter_sentences() for r in s.refs if r.kind == "fact"}
        unverified = [f.fact_id for f in ledger.facts if f.fact_id in used and f.kind in KEY_KINDS and f.status == FactStatus.RECORDED]
        if unverified:
            reasons.append(f"关键数据 {len(unverified)} 项仍为“材料记载”，未经核实（{'、'.join(unverified[:6])}）")
    if policies:
        used_p = {r.id for _, s in ir.iter_sentences() for r in s.refs if r.kind == "policy"}
        pend = [e.citation for e in policies.items if e.evidence_id in used_p and e.verification.startswith("待核")]
        if pend:
            reasons.append(f"引用依据待人工核验：{'、'.join(pend[:3])}")
    if state.pending_checkpoints():
        kinds = {c.kind.value for c in state.pending_checkpoints()} - {"人工送审"}
        if kinds:
            reasons.append(f"仍有待确认事项：{'、'.join(sorted(kinds))}")
    return (DocStatus.SUBMISSION if not reasons else DocStatus.DISCUSSION), reasons or ["关键事实、依据和措施已完成相应核验，无阻断或重要问题"]


class ReviewPackageSkill(Skill):
    name = "gongwen-review-package"
    number = 12
    title = "送审打包与留痕"
    stage = Stage.HUMAN_REVIEW
    channel_name = "packager"
    allowed_tools = ("gongwen_export",)
    output_artifact = "review_package"

    def run(
        self,
        sc: SkillContext,
        ir: DocumentIR,
        report: ReviewReport | None,
        ledger: FactLedger | None,
        policies: PolicyPack | None,
        outline: OutlinePlan | None,
        genre: GenreDecision | None,
        layout: LayoutReport | None,
        patchsets: list[PatchSet],
        admissions: list[AdmissionResult],
        out_dir: Path,
    ) -> tuple[ReviewPackage, dict[str, Any]]:
        state = sc.state
        status, reasons = assess_status(ir, report, ledger, policies, state)
        ir.status = status
        evidence_rows, evidence_map = self._evidence(sc, ir, ledger, policies, outline)
        pending = self._pending(sc, ir, report, ledger, policies, genre)
        versions = []
        for v in sc.store.versions(state.task_id, ir.doc_id):
            vir = sc.store.load_version(state.task_id, ir.doc_id, v, DocumentIR)
            if vir is None:
                continue
            ps = next((p for p in patchsets if p.to_version == v), None)
            versions.append(
                VersionEntry(
                    version=v,
                    created_at=vir.meta.get("created_at", ""),
                    author=vir.meta.get("author", "system"),
                    summary=vir.meta.get("summary", "初稿" if v == 1 else "修订"),
                    patch_ids=[p.patch_id for p in ps.patches if p.status == "applied"] if ps else [],
                    doc_hash=stable_hash(vir),
                    semantic_changes=[f"{c.dimension}：{c.before}→{c.after}（{c.direction}）" for p in (ps.patches if ps else []) for c in p.semantic_changes],
                )
            )
        outputs = list(layout.outputs) if layout else []
        pkg = ReviewPackage(
            task_id=state.task_id,
            matter_id=state.matter_id,
            doc_id=ir.doc_id,
            doc_version=ir.version,
            doc_hash=stable_hash(ir),
            status=status,
            status_reasons=reasons,
            genre=ir.genre or ir.material_type,
            title=ir.title,
            outputs=outputs,
            evidence_table=evidence_rows,
            issue_counts=report.counts() if report else {},
            open_issue_ids=[i.issue_id for i in report.open_issues()] if report else [],
            pending=pending,
            procedures=genre.procedures if genre else [],
            versions=versions,
            layout=layout,
            admission_summary=[f"{a.material_id} {a.filename}：{a.decision.value}（{a.detected_clearance.value}）" for a in admissions],
            rule_versions={"ruleset": RULESET_VERSION, "layout_profile": f"{layout.profile}:{layout.profile_version}" if layout else ""},
        )
        data = self.workbench_data(sc, ir, pkg, report, patchsets, evidence_map, layout, genre)
        page = build_page(ir, data)
        wb = out_dir / "workbench.html"
        wb.write_text(page, encoding="utf-8")
        pj = out_dir / "review_package.json"
        pj.write_text(pkg.model_dump_json(indent=2), encoding="utf-8")
        md = out_dir / "审阅说明.md"
        md.write_text(self.markdown(pkg, report), encoding="utf-8")
        for kind, p in (("html", wb), ("json", pj), ("md", md)):
            pkg.outputs.append(OutputFile(kind=kind, path=str(p), sha256=sha256_bytes(p.read_bytes())))
        sc.note("skill.package", {"status": status.value, "reasons": reasons, "pending": len(pending), "evidence_rows": len(evidence_rows)})
        return pkg, data

    # ------------------------------------------------------------------
    def _evidence(self, sc, ir, ledger, policies, outline) -> tuple[list[EvidenceRow], dict[str, Any]]:
        rows: list[EvidenceRow] = []
        emap: dict[str, Any] = {}
        mats = {m.material_id: m for m in sc.runtime.materials.list(sc.state.matter_id)}
        for b, s in ir.iter_sentences():
            refs = []
            for r in s.refs:
                if r.kind == "fact" and ledger and ledger.get(r.id):
                    f = ledger.get(r.id)
                    src = f.sources[0] if f.sources else None
                    mat = mats.get(src.material_id) if src else None
                    refs.append(
                        {
                            "kind": "fact",
                            "id": f.fact_id,
                            "label": f"{f.fact_id} {f.attribute}",
                            "status": f.status.value,
                            "detail": {
                                "陈述": f.statement,
                                "数值": f.display_value(),
                                "来源": f"{(mat.filename if mat else src.material_id) if src else ''} {src.path if src else ''}",
                                "原文": src.excerpt if src else "",
                                "口径": f.caliber,
                                "时点": f.as_of,
                                "公式": f"{f.formula.expression} = {f.formula.result_repr}" if f.formula else "",
                                "核验": f"{f.verification.method}（{f.verification.by}）" if f.verification else "",
                            },
                        }
                    )
                elif r.kind == "policy" and policies and policies.get(r.id):
                    e = policies.get(r.id)
                    refs.append(
                        {
                            "kind": "policy",
                            "id": e.evidence_id,
                            "label": e.citation,
                            "status": "适用" if e.applicability.applicable else ("不适用" if e.applicability.applicable is False else "需人工确认"),
                            "detail": {"条款": e.article_no or "", "原文": e.quote[:300], "适用性": "；".join(e.applicability.reasons), "核验": e.verification, "检索方式": e.retrieval},
                        }
                    )
                elif r.kind == "measure" and outline and outline.measure(r.id):
                    m = outline.measure(r.id)
                    refs.append(
                        {
                            "kind": "measure",
                            "id": m.measure_id,
                            "label": f"措施 {m.measure_id}",
                            "status": m.status,
                            "detail": {"主体": m.subject or "未明确", "行为": m.action, "对象": m.obj, "条件": m.condition, "时限": m.deadline, "义务强度": m.obligation, "例外": m.exceptions, "来源": m.origin, "原文": m.text},
                        }
                    )
                elif r.kind == "task":
                    spec = sc.load("task_spec", TaskSpec)
                    refs.append(
                        {
                            "kind": "task",
                            "id": r.id,
                            "label": "办文需求（事由）",
                            "status": spec.subject.status if spec else "任务契约",
                            "detail": {"事由": str(spec.subject.value) if spec else "", "需求原文": spec.request_text if spec else "", "说明": r.note},
                        }
                    )
                elif r.kind == "material":
                    mat = mats.get(r.id)
                    refs.append({"kind": "material", "id": r.id, "label": mat.filename if mat else r.id, "status": "材料", "detail": {"材料": mat.filename if mat else r.id}})
            conf = "；".join(sorted({x["status"] for x in refs})) if refs else "无关联证据"
            row = EvidenceRow(sentence_id=s.sid, location=ir.location_label(b.bid), text=s.text, refs=[{"kind": x["kind"], "id": x["id"], "label": x["label"], "status": x["status"]} for x in refs], confirmation=conf)
            rows.append(row)
            emap[s.sid] = {"location": row.location, "text": s.text, "refs": refs, "confirmation": conf}
        return rows, emap

    def _pending(self, sc, ir, report, ledger, policies, genre) -> list[PendingItem]:
        items: list[PendingItem] = []
        n = 0

        def add(kind, desc, loc="", blocking=False):
            nonlocal n
            n += 1
            items.append(PendingItem(item_id=f"Q-{n:03d}", kind=kind, description=desc, location=loc, blocking=blocking))

        for p in ir.placeholders:
            add("待补字段", f"{p.field}：{p.reason}", p.location, blocking=p.field not in ("header.doc_number", "signature.date", "header.signers"))
        if report:
            for i in report.open_issues():
                if i.needs_human:
                    add(f"人工审核：{i.type.value}", i.suggestion, i.location.label, blocking=i.severity in (Severity.BLOCKING,))
        if genre:
            for proc in genre.procedures:
                add("专门程序", f"{proc.name}（{proc.status}）：{proc.trigger}", "", blocking=False)
        if ledger:
            for c in ledger.conflicts:
                if c.resolution is None:
                    add("事实冲突", f"{c.attribute}：{c.description}", "", blocking=True)
        return items

    def workbench_data(self, sc, ir, pkg, report, patchsets, emap, layout, genre) -> dict[str, Any]:
        issues = []
        for i in report.open_issues() if report else []:
            issues.append(
                {
                    "id": i.issue_id,
                    "type": i.type.value,
                    "severity": i.severity.value,
                    "sid": i.location.sentence_id,
                    "bid": i.location.block_id,
                    "location": i.location.label or i.location.field or "",
                    "original": i.original,
                    "evidence_text": i.evidence_text,
                    "suggestion": i.suggestion,
                    "rule": f"{i.rule.source}（{i.rule.level.value}）" if i.rule else "",
                    "channel": i.channel,
                    "needs_human": i.needs_human,
                }
            )
        patches = []
        for ps in patchsets:
            for p in ps.patches:
                patches.append({"id": p.patch_id, "op": p.op, "target": p.target, "before": p.before, "after": p.after, "reason": p.reason, "status": p.status})
        cps = [
            {"cp_id": c.cp_id, "kind": c.kind.value, "question": c.question, "details": c.details, "options": [o.model_dump() for o in c.options]}
            for c in sc.state.pending_checkpoints()
        ]
        return {
            "task_id": sc.state.task_id,
            "doc_id": ir.doc_id,
            "version": ir.version,
            "status": pkg.status.value,
            "status_reasons": pkg.status_reasons,
            "genre": pkg.genre,
            "counts": pkg.issue_counts,
            "issues": issues,
            "evidence": emap,
            "pending": [p.model_dump() for p in pkg.pending],
            "checkpoints": cps,
            "versions": [v.model_dump(mode="json") for v in pkg.versions],
            "patches": patches,
            "layout": json.loads(layout.model_dump_json()) if layout else None,
        }

    @staticmethod
    def markdown(pkg: ReviewPackage, report: ReviewReport | None) -> str:
        lines = [f"# 审阅说明：{pkg.title}", "", f"- 文稿状态：**{pkg.status.value}**", f"- 文稿版本：第 {pkg.doc_version} 版（内容哈希 {pkg.doc_hash[:12]}）", f"- 规则库版本：{pkg.rule_versions.get('ruleset')}；版式配置：{pkg.rule_versions.get('layout_profile')}", ""]
        lines += ["## 状态说明", ""] + [f"- {r}" for r in pkg.status_reasons] + [""]
        if report:
            lines += ["## 审校问题", ""]
            for i in report.open_issues():
                lines += ["```", i.render(), "```", ""]
        if pkg.pending:
            lines += ["## 待确认事项", ""] + [f"- [{p.kind}] {p.description}{'（阻断）' if p.blocking else ''}" for p in pkg.pending] + [""]
        if pkg.procedures:
            lines += ["## 需要衔接的真实程序", ""] + [f"- {p.name}：{p.trigger}（{p.status}）" for p in pkg.procedures] + [""]
        if pkg.layout:
            r = pkg.layout.render
            lines += ["## 版式核验", "", f"- 实际渲染：{'是（' + r.renderer + '）' if r.rendered else '否'}；页数：{r.pages}"]
            lines += [f"- 字体替代：{s}" for s in r.substitutions]
            lines += [f"- {c.item}：{c.actual}（{c.status}，{c.clause}）" for c in pkg.layout.checks if c.status != "pass"] + [""]
        lines += ["## 声明", ""] + [f"- {d}" for d in pkg.disclaimers]
        return "\n".join(lines)

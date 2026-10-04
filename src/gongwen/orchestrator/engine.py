"""受控主编排器：程序化状态机控制任务状态、工具权限、预算、审批与失败退出。

模型只在允许的范围内提出计划、选择检索策略、组织文稿或提交修改建议；
状态转换、人工审核节点与失败退出完全由本模块的确定性代码决定。

状态：材料准入 → 任务确认 → 材料解析 → 依据与事实准备 → 提纲确认 → 起草 → 审校 → 定向修订 → 排版检查 → 人工送审
异常：待补材料 / 发现冲突 / 超出权限 / 处理失败 / 禁止进入。失败必须可见，不能悄悄降级为猜测。
"""

from __future__ import annotations

import json
import traceback
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..harness.budget import BudgetExceeded
from ..harness.permissions import Action, Principal, channel, human
from ..harness.session import SessionLog
from ..knowledge.stores import TaskStore, safe_id
from ..llm.base import ModelRefused
from ..parsing import parse_bytes
from ..parsing.admission import ScanInput, aggregation_risk, scan
from ..rules import CheckContext
from ..schemas.common import AdmissionDecision, Clearance, DocStatus, IdAllocator, sha256_text, stable_hash, utcnow
from ..schemas.facts import FactLedger, FactStatus
from ..schemas.genre import GenreDecision
from ..schemas.ir import DocumentIR
from ..schemas.layout import LayoutReport
from ..schemas.outline import OutlinePlan
from ..schemas.package import ReviewPackage
from ..schemas.patch import FactChange, PatchSet
from ..schemas.policy import PolicyPack
from ..schemas.review import ReviewReport
from ..schemas.sources import AdmissionResult, Material, SourceBundle
from ..schemas.state import ApprovalRecord, Checkpoint, CheckpointKind, Stage, StageRecord, TaskState
from ..schemas.task import Direction, TaskSpec
from ..skills.base import SkillContext
from ..skills.consistency_check import matter_siblings
from ..skills.fact_ledger import add_human_fact, confirm_facts
from . import checkpoints as cpk

TERMINAL = {Stage.SUBMITTED, Stage.APPROVED, Stage.FAILED, Stage.BLOCKED}


class AdmissionList:
    """准入结果列表的持久化包装（TaskStore 只存 Pydantic 模型）。"""

    @staticmethod
    def load(store: TaskStore, task_id: str) -> list[AdmissionResult]:
        p = store.task_dir(task_id) / "artifacts" / "admissions.jsonl"
        if not p.is_file():
            return []
        return [AdmissionResult.model_validate_json(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]

    @staticmethod
    def save(store: TaskStore, task_id: str, items: list[AdmissionResult]) -> None:
        d = store.task_dir(task_id) / "artifacts"
        d.mkdir(exist_ok=True)
        (d / "admissions.jsonl").write_text("\n".join(i.model_dump_json() for i in items) + ("\n" if items else ""), encoding="utf-8")


class Engine:
    def __init__(self, runtime, providers: dict | None = None):
        self.rt = runtime
        self.providers = providers

    # ================================================================ 基础
    @property
    def store(self) -> TaskStore:
        return self.rt.tasks

    def load_state(self, task_id: str) -> TaskState:
        if not self.store.exists(task_id):
            raise KeyError(f"任务不存在：{task_id}")
        p = self.store.task_dir(task_id) / "state.json"
        return TaskState.model_validate_json(p.read_text(encoding="utf-8"))

    def save_state(self, st: TaskState) -> None:
        (self.store.task_dir(st.task_id) / "state.json").write_text(st.model_dump_json(indent=2), encoding="utf-8")

    def log(self, task_id: str) -> SessionLog:
        return self.rt.session_log(task_id)

    def _clearances(self, st: TaskState) -> list[Clearance]:
        """本任务处理时会用到的全部材料的属性：本任务的准入结果 + 同一事项下已准入的材料（解析时一并使用）。"""
        cl = [a.detected_clearance if a.declared_clearance is None else max((a.detected_clearance, a.declared_clearance), key=lambda c: c.rank) for a in AdmissionList.load(self.store, st.task_id) if a.decision == AdmissionDecision.ALLOW]
        for m in self.rt.materials.list(st.matter_id):
            if m.admission == AdmissionDecision.ALLOW:
                cl.append(m.clearance if m.declared_clearance is None else max((m.clearance, m.declared_clearance), key=lambda c: c.rank))
        return cl or [Clearance.PUBLIC]

    def sc(self, st: TaskState, log: SessionLog) -> SkillContext:
        router = self.rt.router(log, st.budget, self.providers)
        return SkillContext(runtime=self.rt, state=st, log=log, router=router, clearances=self._clearances(st))

    def _require(self, by: Principal, action: Action, matter_id: str | None = None) -> None:
        self.rt.permissions.require(by, action, matter_id)

    # ================================================================ 任务与材料
    def create_task(self, request: str, *, by: Principal, matter_id: str | None = None, hints: dict | None = None, options: dict | None = None) -> TaskState:
        self._require(by, Action.TASK_WRITE)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")[:-3]
        task_id = f"T{stamp}"
        if matter_id:
            safe_id(matter_id, "事项标识")
        st = TaskState(task_id=task_id, matter_id=matter_id or f"M{stamp}", created_by=by.id, options={"request": request, "hints": hints or {}, **(options or {})})
        st.history.append(StageRecord(stage=Stage.ADMISSION))
        self.save_state(st)
        log = self.log(task_id)
        log.append("task.created", {"task_id": task_id, "matter_id": st.matter_id, "request": request, "hints": hints or {}}, actor=by.id, stage=st.stage.value)
        self.rt.hooks.run("SessionStart", task_id, {"task_id": task_id})
        return st

    def add_material(self, task_id: str, filename: str, data: bytes, *, by: Principal, declared: Clearance | None = None, role: str = "material", authoritative: bool = False, description: str = "") -> AdmissionResult:
        st = self.load_state(task_id)
        self._require(by, Action.MATERIAL_ADD, st.matter_id)
        if authoritative and not by.is_human:
            raise PermissionError("只有人工通道可以把材料标记为权威来源")
        # 材料按事项存放：编号在事项内分配，同一事项的多个任务不会互相覆盖
        mids = self._matter_ids(st)
        mid = mids.next("MAT")
        self._save_matter_ids(st, mids)
        mat = self.rt.materials.put(st.matter_id, mid, filename, data, by.id, declared, role, description)
        mat.authoritative = authoritative
        # 准入扫描：本地确定性规则，先于任何模型处理
        parsed = parse_bytes(mid, filename, data)
        res = scan(ScanInput(mid, filename, parsed, declared), self.rt.config.environment.route, self.rt.config.environment.accept_internal_materials)
        mat.clearance = res.detected_clearance
        mat.admission = res.decision
        log = self.log(task_id)
        if res.decision == AdmissionDecision.FORBID:
            self.rt.materials.purge(st.matter_id, mid)
            log.append("material.forbidden", {"material_id": mid, "filename": filename, "reasons": res.reasons, "findings": [f.code for f in res.findings]}, actor=by.id, stage=st.stage.value)
        else:
            self.rt.materials.save_meta(mat)
            log.append("material.added", {"material_id": mid, "filename": filename, "sha256": mat.sha256, "decision": res.decision.value, "clearance": res.detected_clearance.value, "findings": [f.code for f in res.findings]}, actor=by.id, stage=st.stage.value)
        items = AdmissionList.load(self.store, task_id)
        items.append(res)
        AdmissionList.save(self.store, task_id, items)
        # 解析之后追加材料：派生产物失效，回到准入阶段重新判断（任务契约保留，但须重新确认提纲）
        if st.stage not in (Stage.ADMISSION, Stage.NEED_MATERIAL, Stage.TASK_CONFIRM) and res.decision != AdmissionDecision.FORBID:
            st.options["materials_changed"] = True
            for cp in st.pending_checkpoints():
                cp.status = "cancelled"
            for name in ("source_bundle", "genre_decision", "policy_pack", "fact_ledger", "outline"):
                st.artifacts.pop(name, None)
                (self.store.task_dir(task_id) / "artifacts" / f"{name}.json").unlink(missing_ok=True)
            if st.stage not in TERMINAL:
                self._transition(st, log, Stage.ADMISSION, f"追加材料 {mid}，重新准入与解析")
        self.save_state(st)
        return res

    def materials(self, st: TaskState) -> list[Material]:
        return [m for m in self.rt.materials.list(st.matter_id) if m.admission == AdmissionDecision.ALLOW]

    # ================================================================ 主循环
    def advance(self, task_id: str, *, by: Principal | None = None, max_steps: int = 40, auto_accept: set[str] | None = None) -> TaskState:
        st = self.load_state(task_id)
        log = self.log(task_id)
        auto = {cpk.AUTO_ACCEPT_KEYS[k] for k in (auto_accept or set()) if k in cpk.AUTO_ACCEPT_KEYS}
        for _ in range(max_steps):
            if st.stage in TERMINAL:
                break
            if st.pending_checkpoints():
                handled = False
                for cp in st.pending_checkpoints():
                    if cp.kind in auto and cp.auto_acceptable and by is not None and by.is_human:
                        self._resolve(st, log, cp, cpk.AUTO_ACCEPT_DEFAULTS[cp.kind], by, "无头模式按 --accept 参数自动接受（启动者已授权）", {})
                        handled = True
                        break
                if not handled:
                    break
                continue
            handler = getattr(self, f"_h_{st.stage.name.lower()}", None)
            if handler is None:
                break
            try:
                self.rt.hooks.run("StageEnter", st.stage.value, {"task_id": task_id})
                nxt = handler(st, log)
            except BudgetExceeded as exc:
                nxt = self._fail(st, log, f"预算超限：{exc}")
            except ModelRefused as exc:
                nxt = self._fail(st, log, f"模型拒答，已停止而不是以空结果继续：{exc}")
            except PermissionError as exc:
                nxt = self._fail(st, log, f"权限拒绝：{exc}")
            except Exception as exc:  # 失败必须可见
                log.append("error", {"error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc(limit=4)}, stage=st.stage.value)
                nxt = self._fail(st, log, f"{type(exc).__name__}: {exc}")
            if nxt is not None and nxt != st.stage:
                self._transition(st, log, nxt)
            self.save_state(st)
            if nxt is None:
                auto_ok = by is not None and by.is_human and any(cp.kind in auto and cp.auto_acceptable for cp in st.pending_checkpoints())
                if not auto_ok:
                    break
        self.save_state(st)
        return st

    def _transition(self, st: TaskState, log: SessionLog, nxt: Stage, reason: str = "") -> None:
        if st.history:
            st.history[-1].exited_at = utcnow()
            st.history[-1].outcome = reason or f"→ {nxt.value}"
        self.rt.hooks.run("StageExit", st.stage.value, {"task_id": st.task_id, "next": nxt.value})
        log.append("stage.transition", {"from": st.stage.value, "to": nxt.value, "reason": reason}, stage=st.stage.value)
        st.previous_stage = st.stage
        st.stage = nxt
        st.history.append(StageRecord(stage=nxt))

    def _fail(self, st: TaskState, log: SessionLog, reason: str) -> Stage:
        st.errors.append(reason)
        st.exception_reason = reason
        log.append("task.failed", {"reason": reason}, stage=st.stage.value)
        return Stage.FAILED

    def _checkpoint(self, st: TaskState, log: SessionLog, kind: CheckpointKind, question: str, details: list[str] | None = None, payload: dict | None = None) -> Checkpoint:
        cp = cpk.make(st.ids, kind, st.stage, question, details, payload)
        st.checkpoints.append(cp)
        log.append("checkpoint.requested", {"cp_id": cp.cp_id, "kind": kind.value, "question": question}, stage=st.stage.value)
        self.rt.hooks.run("CheckpointRequested", kind.value, {"task_id": st.task_id, "cp_id": cp.cp_id})
        return cp

    def _resolved(self, st: TaskState, kind: CheckpointKind, stage: Stage | None = None) -> Checkpoint | None:
        """最近一次同类审核节点已被处理时返回它（重新规划后产生的新节点必须重新确认）。"""
        for cp in reversed(st.checkpoints):
            if cp.kind == kind and (stage is None or cp.stage == stage) and cp.status != "cancelled":
                return cp if cp.status == "resolved" else None
        return None

    # ================================================================ 状态处理
    def _h_admission(self, st: TaskState, log: SessionLog) -> Stage | None:
        items = AdmissionList.load(self.store, st.task_id)
        pending = [a for a in items if a.decision == AdmissionDecision.NEED_CONFIRM and a.confirmed_by is None]
        if pending and not any(c.kind == CheckpointKind.MATERIAL_CONFIRM and c.status == "pending" for c in st.checkpoints):
            details = [f"{a.material_id} {a.filename}：{'；'.join(a.reasons)}" for a in pending]
            self._checkpoint(st, log, CheckpointKind.MATERIAL_CONFIRM, "以下材料需要人工确认属性后才能进入处理（系统不会先上传模型再判断是否敏感）", details, {"materials": [a.material_id for a in pending]})
            return None
        if pending:
            return None
        agg = aggregation_risk([a for a in items if a.decision != AdmissionDecision.FORBID])
        if agg and not st.options.get("aggregation_ack"):
            st.options["aggregation_ack"] = "pending"
            self._checkpoint(st, log, CheckpointKind.MATERIAL_CONFIRM, agg, [], {"aggregation": True, "materials": []})
            return None
        st.options.pop("materials_changed", None)
        return Stage.TASK_CONFIRM

    def _h_task_confirm(self, st: TaskState, log: SessionLog) -> Stage | None:
        sc = self.sc(st, log)
        spec = sc.load("task_spec", TaskSpec)
        if spec is None:
            mats = self.materials(st)
            bundle = self.rt.skills.get("gongwen-material-parsing").run(sc, mats) if mats else None
            hints = dict(st.options.get("hints") or {})
            hints["material_ids"] = [m.material_id for m in mats]
            spec = self.rt.skills.get("gongwen-task-modeling").run(sc, st.options.get("request", ""), hints, bundle)
            sc.save("task_spec", spec)
            details = self._spec_details(spec)
            self._checkpoint(st, log, CheckpointKind.TASK_CONFIRM, "请确认办文任务契约（目的、发文主体、受文主体、行文关系、文种与适用时点）", details)
            return None
        if self._resolved(st, CheckpointKind.TASK_CONFIRM) is None:
            return None
        return Stage.PARSING

    @staticmethod
    def _spec_details(spec: TaskSpec) -> list[str]:
        def v(slot):
            val = slot.value
            if isinstance(val, dict):
                val = val.get("name")
            if isinstance(val, list):
                val = "、".join(x.get("name") if isinstance(x, dict) else str(x) for x in val)
            return f"{val or '（缺失）'}［{slot.status}］"

        d = [
            f"办文意图：{'、'.join(spec.purposes) or '（未识别）'}",
            f"任务层级：{v(spec.layer)}",
            f"发文主体：{v(spec.issuer)}",
            f"受文主体：{v(spec.recipients)}",
            f"行文关系：{v(spec.relation)}",
            f"建议文种：{v(spec.suggested_genre)}（用户字面要求：{spec.requested_genre or '未指定'}）",
            f"事由：{v(spec.subject)}",
            f"政策适用时点：{spec.policy_as_of}（{'按指定时点' if spec.policy_mode == 'historical' else '按当前有效政策'}）",
            f"禁止补写：{'、'.join(spec.forbidden_fill)}",
        ]
        d += [f"提示：{n}" for n in spec.notes]
        d += [f"需回答：{g.question}" for g in spec.gaps if g.ask_user]
        return d

    def _h_need_material(self, st: TaskState, log: SessionLog) -> Stage | None:
        if not any(c.kind == CheckpointKind.NEED_MATERIAL and c.status == "pending" for c in st.checkpoints):
            self._checkpoint(st, log, CheckpointKind.NEED_MATERIAL, "请补充材料后继续（可用 gongwen task add 追加）", st.options.get("need_material_details", []), {"resume_stage": Stage.ADMISSION.value})
        return None

    def _h_parsing(self, st: TaskState, log: SessionLog) -> Stage | None:
        sc = self.sc(st, log)
        bundle = self.rt.skills.get("gongwen-material-parsing").run(sc, self.materials(st))
        sc.save("source_bundle", bundle)
        return Stage.EVIDENCE

    def _h_evidence(self, st: TaskState, log: SessionLog) -> Stage | None:
        sc = self.sc(st, log)
        spec = sc.load("task_spec", TaskSpec)
        bundle = sc.load("source_bundle", SourceBundle) or SourceBundle()
        genre = sc.load("genre_decision", GenreDecision)
        if genre is None:
            genre = self.rt.skills.get("gongwen-genre-authority").run(sc, spec, bundle)
            sc.save("genre_decision", genre)
            blocking = [f for f in genre.authority_findings if f.out_of_authority or f.severity == "阻断送审"]
            if blocking:
                self._checkpoint(
                    st,
                    log,
                    CheckpointKind.AUTHORITY,
                    "发现超出权限或行文关系问题，需人工确认",
                    [f"{f.message}（{f.basis[0].source if f.basis else ''}）" for f in blocking] + genre.conflicts,
                    {"resume_stage": Stage.EVIDENCE.value},
                )
                return Stage.OUT_OF_AUTHORITY
        pack = sc.load("policy_pack", PolicyPack)
        if pack is None:
            pack = self.rt.skills.get("gongwen-policy-retrieval").run(sc, spec, genre, bundle)
            sc.save("policy_pack", pack)
            if pack.conflicts:
                self._checkpoint(st, log, CheckpointKind.CONFLICT, "依据之间存在冲突，请专业人员判断（系统不以“新文件优先”代替判断）", [c.point + "；" + c.conditions for c in pack.conflicts], {"resume_stage": Stage.EVIDENCE.value, "kind": "policy"})
                return Stage.CONFLICT
        ledger = self.load_matter_ledger(st)
        if ledger is None or st.options.get("materials_changed") or not st.artifacts.get("fact_ledger"):
            new = self.rt.skills.get("gongwen-fact-ledger").run(sc, spec, bundle)
            ledger = self.merge_matter_ledger(st, new)
            sc.save("fact_ledger", ledger)
            open_conflicts = [c for c in ledger.conflicts if c.resolution is None]
            if open_conflicts:
                self._checkpoint(st, log, CheckpointKind.CONFLICT, "材料之间存在相互矛盾的数据，请确认采信哪一项", [f"{c.conflict_id} {c.attribute}：{c.description}（涉及 {', '.join(c.fact_ids)}）" for c in open_conflicts], {"resume_stage": Stage.EVIDENCE.value, "kind": "fact"})
                return Stage.CONFLICT
        return Stage.OUTLINE_CONFIRM

    def _h_conflict(self, st: TaskState, log: SessionLog) -> Stage | None:
        return None  # 等待人工处理冲突节点

    def _h_out_of_authority(self, st: TaskState, log: SessionLog) -> Stage | None:
        return None  # 等待人工处理权限节点

    def _h_outline_confirm(self, st: TaskState, log: SessionLog) -> Stage | None:
        sc = self.sc(st, log)
        outline = sc.load("outline", OutlinePlan)
        if outline is None or st.options.pop("rebuild_outline", False):
            spec = sc.load("task_spec", TaskSpec)
            genre = sc.load("genre_decision", GenreDecision)
            pack = sc.load("policy_pack", PolicyPack)
            bundle = sc.load("source_bundle", SourceBundle)
            ledger = self.load_matter_ledger(st) or FactLedger()
            outline = self.rt.skills.get("gongwen-outline-planning").run(sc, spec, genre, ledger, pack, bundle)
            sc.save("outline", outline)
            details = [f"标题：{outline.title}"]
            for sec in outline.sections:
                details.append(f"{sec.heading or sec.role}：{'；'.join(p.core for p in sec.paragraphs if p.core) or '（材料不足）'}")
            for m in outline.measures:
                details.append(f"措施 {m.measure_id}［{m.status}·{m.origin}］主体：{m.subject or '未明确'}｜{m.action}{m.obj}｜时限：{m.deadline or '未明确'}｜强度：{m.obligation or '—'}")
            key = [f for f in ledger.facts if f.kind in ("money", "count", "percent") and f.status == FactStatus.RECORDED and "example" not in f.tags][:12]
            if key:
                details.append("以下关键数据为“材料记载”，如已核实请在确认时列入 confirm_facts：" + "、".join(f"{f.fact_id}（{f.attribute} {f.display_value()}）" for f in key))
            details += [f"待回答：{q}" for q in outline.open_questions]
            details += [f"缺少要素：{x}" for x in outline.contract_missing]
            if genre is not None:  # 文种与程序问题在起草前呈现，而不是等审校发现
                details += [f"文种与行文：{c}" for c in genre.conflicts]
                details += [f"专门程序：{pr.name}（{pr.status}）——{pr.trigger}" for pr in genre.procedures]
            self._checkpoint(st, log, CheckpointKind.OUTLINE_CONFIRM, "请确认提纲与措施表（系统可以提示缺失和候选方案，但不替您决定预算、规模、责任和承诺）", details)
            return None
        if self._resolved(st, CheckpointKind.OUTLINE_CONFIRM) is None:
            return None
        return Stage.DRAFTING

    def _h_drafting(self, st: TaskState, log: SessionLog) -> Stage | None:
        sc = self.sc(st, log)
        spec = sc.load("task_spec", TaskSpec)
        genre = sc.load("genre_decision", GenreDecision)
        pack = sc.load("policy_pack", PolicyPack)
        outline = sc.load("outline", OutlinePlan)
        bundle = sc.load("source_bundle", SourceBundle)
        ledger = self.load_matter_ledger(st) or FactLedger()
        doc_id = st.doc_ids[0] if st.doc_ids else f"D{st.task_id[1:]}"
        ir = self.rt.skills.get("gongwen-constrained-drafting").run(sc, spec, genre, ledger, pack, outline, bundle, doc_id, st.matter_id)
        version = (st.current_version or 0) + 1
        ir.version = version
        ir.meta.update({"created_at": utcnow().isoformat(), "author": "system", "summary": "初稿" if version == 1 else "重新起草"})
        self.store.save_version(st.task_id, doc_id, version, ir)
        if doc_id not in st.doc_ids:
            st.doc_ids.append(doc_id)
        st.current_version = version
        st.options["review_round"] = 0
        log.append("draft.created", {"doc_id": doc_id, "version": version, "sha256": stable_hash(ir)}, stage=st.stage.value)
        return Stage.REVIEW

    def current_ir(self, st: TaskState) -> DocumentIR | None:
        if not st.doc_ids:
            return None
        return self.store.load_version(st.task_id, st.doc_ids[0], st.current_version, DocumentIR)

    def check_context(self, st: TaskState, ir: DocumentIR, previous: DocumentIR | None = None, sc: SkillContext | None = None) -> CheckContext:
        """独立审校上下文：从存储重新加载证据，不复用起草时的内存对象。"""
        sc = sc or self.sc(st, self.log(st.task_id))
        return CheckContext(
            ir=ir,
            task=sc.load("task_spec", TaskSpec),
            genre=sc.load("genre_decision", GenreDecision),
            ledger=self.load_matter_ledger(st),
            policies=sc.load("policy_pack", PolicyPack),
            outline=sc.load("outline", OutlinePlan),
            sources=sc.load("source_bundle", SourceBundle),
            previous=previous,
            siblings=matter_siblings(sc, ir.doc_id),
            profile=self.rt.profile,
            features=self.rt.config.features,
            ids=st.ids,
            policy_library=self.rt.policies,
        )

    def _h_review(self, st: TaskState, log: SessionLog) -> Stage | None:
        sc = self.sc(st, log)
        ir = self.current_ir(st)
        prev = self.store.load_version(st.task_id, ir.doc_id, ir.based_on_version, DocumentIR) if ir.based_on_version else None
        ctx = self.check_context(st, ir, prev, sc)
        report_c, cons_issues = self.rt.skills.get("gongwen-consistency-check").run(sc, ctx)
        rnd = int(st.options.get("review_round", 0)) + 1
        st.options["review_round"] = rnd
        report = self.rt.skills.get("gongwen-independent-review").run(sc, ctx, rnd, cons_issues)
        # 人工已接受风险的问题保持状态
        for i in report.issues:
            if (i.rule.rule_id if i.rule else "", i.location.sentence_id, i.type.value) in {tuple(x) for x in st.options.get("accepted_risks", [])}:
                i.status = "accepted_risk"
        self.store.save_version(st.task_id, ir.doc_id, ir.version, ir)  # 保存自动补充的证据关联
        sc.save("consistency_report", report_c)
        sc.save(f"review_r{rnd}", report)
        sc.save("review_report", report)
        auto_fixable = [i for i in report.open_issues() if i.auto_fixable]
        rounds_left = sc.router.budget.revision_rounds_left() if sc.router.budget else 0
        if auto_fixable and rounds_left > 0 and self.rt.config.features.targeted_revision:
            return Stage.REVISION
        needs_human = [i for i in report.open_issues() if i.needs_human and i.severity.rank >= 3]
        proposals = sc.load("pending_proposals", PatchSet)
        if (needs_human or (proposals and proposals.patches)) and not st.options.get("escalated_v") == ir.version:
            st.options["escalated_v"] = ir.version
            details = [f"{i.issue_id} [{i.severity.value}] {i.type.value}：{i.suggestion}（{i.location.label or i.location.field or ''}）" for i in needs_human[:20]]
            if proposals and proposals.patches:
                details += [f"建议修改 {p.patch_id}：{p.reason}｜“{p.before[:40]}”→“{p.after[:40]}”" for p in proposals.patches if p.status == "needs_human"]
            self._checkpoint(st, log, CheckpointKind.REVIEW_ESCALATION, "以下问题需要人工处理（自动修订已达上限或涉及实质判断）", details)
            return None
        return Stage.LAYOUT

    def _h_revision(self, st: TaskState, log: SessionLog) -> Stage | None:
        sc = self.sc(st, log)
        ir = self.current_ir(st)
        report = sc.load("review_report", ReviewReport)
        ledger = self.load_matter_ledger(st)
        skill = self.rt.skills.get("gongwen-targeted-revision")
        rnd = int(st.options.get("revision_round", 0)) + 1
        st.options["revision_round"] = rnd
        ps = skill.propose(sc, ir, report, ledger, rnd)
        new = skill.apply(sc, ir, ps)
        new.meta.update({"created_at": utcnow().isoformat(), "author": "system:定向修订", "summary": f"第{rnd}轮定向修订：应用 {sum(1 for p in ps.patches if p.status == 'applied')} 处"})
        skill.recheck(sc, self.check_context(st, new, ir, sc), ps)
        sc.save(f"patchset_{rnd}", ps)
        waiting = PatchSet(doc_id=ir.doc_id, round=rnd, from_version=new.version, patches=[p for p in ps.patches if p.status == "needs_human"])
        sc.save("pending_proposals", waiting)
        if sc.router.budget:
            sc.router.budget.charge_revision_round()
        if any(p.status == "applied" for p in ps.patches):
            self.store.save_version(st.task_id, new.doc_id, new.version, new)
            st.current_version = new.version
        else:
            st.options["revision_noop"] = True
            sc.router.budget.usage.revision_rounds = sc.router.budget.config.max_revision_rounds if sc.router.budget else 0
        return Stage.REVIEW

    def _h_layout(self, st: TaskState, log: SessionLog) -> Stage | None:
        sc = self.sc(st, log)
        ir = self.current_ir(st)
        report = sc.load("review_report", ReviewReport)
        from ..skills.review_package import assess_status

        ir.status, _ = assess_status(ir, report, self.load_matter_ledger(st), sc.load("policy_pack", PolicyPack), st)
        out = self.store.out_dir(st.task_id)
        layout = self.rt.skills.get("gongwen-layout-compile").run(sc, ir, out)
        sc.save("layout_report", layout)
        if layout.render.rendered and layout.render.first_page_has_body is False and len(ir.recipients) > 3 and not st.options.get("moved_recipients"):
            # 7.3.2：主送机关过多导致首页不能显示正文时，移至版记
            ir.imprint.main_moved = list(ir.recipients)
            ir.recipients = []
            ir.version += 1
            ir.meta.update({"summary": "主送机关移至版记（首页须显示正文）", "author": "system:版式"})
            self.store.save_version(st.task_id, ir.doc_id, ir.version, ir)
            st.current_version = ir.version
            st.options["moved_recipients"] = True
            return Stage.LAYOUT
        return Stage.HUMAN_REVIEW

    def _h_human_review(self, st: TaskState, log: SessionLog) -> Stage | None:
        if any(c.kind == CheckpointKind.HUMAN_REVIEW and c.status == "pending" for c in st.checkpoints):
            return None
        pkg = self.package(st, log)
        self._checkpoint(
            st,
            log,
            CheckpointKind.HUMAN_REVIEW,
            f"当前文稿为“{pkg.status.value}”。请审阅工作台中的文稿、证据、问题、待确认项和修改差异后决定",
            pkg.status_reasons + [f"输出：{o.kind} {o.path}" for o in pkg.outputs],
        )
        return None

    def package(self, st: TaskState, log: SessionLog) -> ReviewPackage:
        return self.package_with_data(st, log)[0]

    def package_with_data(self, st: TaskState, log: SessionLog) -> tuple[ReviewPackage, dict[str, Any]]:
        """生成送审包，同时返回审阅工作台所需的数据（供本地审阅服务渲染交互页面）。"""
        sc = self.sc(st, log)
        ir = self.current_ir(st)
        patchsets = []
        for k in sorted(st.artifacts):
            if k.startswith("patchset_") or k.startswith("human_patch_"):
                ps = sc.load(k, PatchSet)
                if ps:
                    patchsets.append(ps)
        pkg, data = self.rt.skills.get("gongwen-review-package").run(
            sc,
            ir,
            sc.load("review_report", ReviewReport),
            self.load_matter_ledger(st),
            sc.load("policy_pack", PolicyPack),
            sc.load("outline", OutlinePlan),
            sc.load("genre_decision", GenreDecision),
            sc.load("layout_report", LayoutReport),
            patchsets,
            AdmissionList.load(self.store, st.task_id),
            self.store.out_dir(st.task_id),
        )
        st.doc_status = pkg.status
        self.store.save_version(st.task_id, ir.doc_id, ir.version, ir)
        sc.save("review_package", pkg)
        self.store.write_json(st.task_id, "workbench_data.json", data)
        return pkg, data

    def workbench_data(self, task_id: str) -> dict[str, Any] | None:
        """审阅工作台数据（证据映射、问题、待确认项、版本差异）；尚未审校时返回 None。

        优先复用最近一次送审打包生成的数据（与当前版本一致时），只刷新待确认事项，
        避免每次查看都重新打包、重复写审计日志。
        """
        st = self.load_state(task_id)
        ir = self.current_ir(st)
        if ir is None:
            return None
        cache = self.store.task_dir(task_id) / "workbench_data.json"
        data = None
        if cache.exists():
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("version") != ir.version:
                data = None
        if data is None:
            if not self.store.has(task_id, "review_report"):
                return None
            _, data = self.package_with_data(st, self.log(task_id))
            self.save_state(st)
        data["checkpoints"] = [
            {"cp_id": c.cp_id, "kind": c.kind.value, "question": c.question, "details": c.details, "options": [o.model_dump() for o in c.options]}
            for c in st.pending_checkpoints()
        ]
        return data

    def workbench_page(self, task_id: str, *, api: str = "", token: str = "") -> str | None:
        """当前文稿的审阅工作台页面；尚未形成审校结果时返回 None。"""
        data = self.workbench_data(task_id)
        if data is None:
            return None
        from ..workbench import build_page

        return build_page(self.current_ir(self.load_state(task_id)), data, api=api, token=token)

    # ================================================================ 人工审核节点
    def resolve_checkpoint(self, task_id: str, cp_id: str, option: str, *, by: Principal, note: str = "", data: dict[str, Any] | None = None) -> TaskState:
        st = self.load_state(task_id)
        if not by.is_human:
            raise PermissionError("人工审核节点只能由人通过人工通道处理，模型通道不可代为确认")
        self._require(by, Action.CHECKPOINT_RESOLVE, st.matter_id)
        cp = st.checkpoint(cp_id)
        if cp is None or cp.status != "pending":
            raise KeyError(f"没有待处理的审核节点：{cp_id}")
        if option not in {o.key for o in cp.options}:
            raise ValueError(f"无效选项：{option}（可选：{'、'.join(o.key for o in cp.options)}）")
        log = self.log(task_id)
        self._resolve(st, log, cp, option, by, note, data or {})
        self.save_state(st)
        return st

    def _resolve(self, st: TaskState, log: SessionLog, cp: Checkpoint, option: str, by: Principal, note: str, data: dict[str, Any]) -> None:
        cp.status = "resolved"
        cp.resolution = {"option": option, "note": note, "data": data}
        cp.resolved_by = by.id
        cp.resolved_at = utcnow()
        log.append("checkpoint.resolved", {"cp_id": cp.cp_id, "kind": cp.kind.value, "option": option, "note": note, "data_keys": sorted(data)}, actor=by.id, stage=st.stage.value)
        sc = self.sc(st, log)
        k = cp.kind
        if option == "abort":
            self._transition(st, log, Stage.FAILED, f"人工终止：{note}")
            st.exception_reason = f"人工终止：{note}"
            return
        if k == CheckpointKind.MATERIAL_CONFIRM:
            self._resolve_material(st, log, cp, option, by, data)
        elif k == CheckpointKind.TASK_CONFIRM:
            spec = sc.load("task_spec", TaskSpec)
            if option == "edit":
                self._apply_spec_edits(spec, data)
            if option == "need_material":
                st.options["need_material_details"] = [g.question for g in spec.gaps if g.ask_user]
                sc.save("task_spec", spec)
                self._transition(st, log, Stage.NEED_MATERIAL, "人工选择先补充材料")
                return
            for slot_name in ("issuer", "recipients", "relation", "suggested_genre", "subject", "region", "layer"):
                slot = getattr(spec, slot_name)
                if slot.known and slot.status in ("推断", "待确认", "材料记载", "用户提供"):
                    slot.status = "已确认"
            for g in spec.gaps:
                if g.field in data:
                    g.ask_user = False
            sc.save("task_spec", spec)
        elif k == CheckpointKind.NEED_MATERIAL:
            for name in ("task_spec", "source_bundle", "genre_decision", "policy_pack", "fact_ledger", "outline"):
                st.artifacts.pop(name, None)
                p = self.store.task_dir(st.task_id) / "artifacts" / f"{name}.json"
                p.unlink(missing_ok=True)
            self._transition(st, log, Stage.ADMISSION, "人工确认材料已补充")
        elif k == CheckpointKind.AUTHORITY:
            if option == "edit":
                spec = sc.load("task_spec", TaskSpec)
                self._apply_spec_edits(spec, data)
                sc.save("task_spec", spec)
                (self.store.task_dir(st.task_id) / "artifacts" / "genre_decision.json").unlink(missing_ok=True)
                st.artifacts.pop("genre_decision", None)
            else:
                st.options.setdefault("authority_ack", []).append({"by": by.id, "note": note})
            self._transition(st, log, Stage.EVIDENCE, "人工处理权限问题")
        elif k == CheckpointKind.CONFLICT:
            ledger = self.load_matter_ledger(st)
            if cp.payload.get("kind") == "fact" and ledger is not None:
                if option == "resolve":
                    confirm_facts(ledger, list(data.get("confirm_facts", [])), by.id)
                for c in ledger.conflicts:
                    if c.resolution is None:
                        c.resolution = f"人工裁定：暂不采信（{by.id}）"
                        for fid in c.fact_ids:
                            f = ledger.get(fid)
                            if f and f.status == FactStatus.CONFLICT:
                                f.status = FactStatus.UNKNOWN
                self.save_matter_ledger(st, ledger)
                sc.save("fact_ledger", ledger)
                self._transition(st, log, Stage.OUTLINE_CONFIRM, "冲突已人工处理")
            else:
                st.options.setdefault("policy_conflict_ack", []).append({"by": by.id, "note": note})
                self._transition(st, log, Stage.EVIDENCE, "依据冲突已人工裁定")
        elif k == CheckpointKind.OUTLINE_CONFIRM:
            ledger = self.load_matter_ledger(st) or FactLedger()
            outline = sc.load("outline", OutlinePlan)
            if option == "edit":
                changed = confirm_facts(ledger, list(data.get("confirm_facts", [])), by.id)
                ids = self._matter_ids(st)
                for item in data.get("add_facts", []):
                    add_human_fact(ledger, ids, item["statement"], item.get("value"), item.get("unit", ""), item.get("kind", "text"), by.id)
                self._save_matter_ids(st, ids)
                for mid in data.get("drop_measures", []):
                    outline.measures = [m for m in outline.measures if m.measure_id != mid]
                    for sec in outline.sections:
                        for p in sec.paragraphs:
                            p.measure_ids = [x for x in p.measure_ids if x != mid]
                for mid in data.get("confirm_measures", []):
                    m = outline.measure(mid)
                    if m:
                        m.confirmed = True
                if data.get("alternative"):
                    outline.chosen_alternative = data["alternative"]
                self.save_matter_ledger(st, ledger)
                sc.save("fact_ledger", ledger)
                sc.save("outline", outline)
                if data.get("add_facts"):
                    st.options["rebuild_outline"] = True
                    (self.store.task_dir(st.task_id) / "artifacts" / "outline.json").unlink(missing_ok=True)
                    st.artifacts.pop("outline", None)
                    # 重新规划后再次确认
                    return
                log.append("facts.confirmed", {"fact_ids": changed}, actor=by.id)
        elif k == CheckpointKind.REVIEW_ESCALATION:
            if option == "apply":
                proposals = sc.load("pending_proposals", PatchSet)
                if proposals and proposals.patches:
                    ir = self.current_ir(st)
                    skill = self.rt.skills.get("gongwen-targeted-revision")
                    for p in proposals.patches:
                        p.status = "proposed"
                        p.author = f"human-approved:{by.id}"
                    new = skill.apply(sc, ir, proposals)
                    new.meta.update({"created_at": utcnow().isoformat(), "author": by.id, "summary": "采纳人工确认的修订建议"})
                    self.store.save_version(st.task_id, new.doc_id, new.version, new)
                    st.current_version = new.version
                    n = len([a for a in st.artifacts if a.startswith("human_patch_")]) + 1
                    sc.save(f"human_patch_{n}", proposals)
                    sc.save("pending_proposals", PatchSet(doc_id=new.doc_id, round=proposals.round, from_version=new.version))
                self._transition(st, log, Stage.REVIEW, "采纳建议修改后重新审校")
            elif option == "keep":
                report = sc.load("review_report", ReviewReport)
                accepted = st.options.setdefault("accepted_risks", [])
                for i in report.open_issues() if report else []:
                    if i.needs_human:
                        accepted.append([i.rule.rule_id if i.rule else "", i.location.sentence_id, i.type.value])
                self._transition(st, log, Stage.LAYOUT, "人工保留原文，问题保留在送审包中")
            elif option == "revise":
                self._revise(st, log, by, data)
        elif k == CheckpointKind.HUMAN_REVIEW:
            if option == "submit":
                self._transition(st, log, Stage.SUBMITTED, f"人工确认形成送审材料（{by.id}）")
            elif option == "revise":
                self._revise(st, log, by, data)

    def _resolve_material(self, st: TaskState, log: SessionLog, cp: Checkpoint, option: str, by: Principal, data: dict) -> None:
        if cp.payload.get("aggregation"):
            st.options["aggregation_ack"] = by.id
            return
        items = AdmissionList.load(self.store, st.task_id)
        declared = data.get("materials", {})
        for a in items:
            if a.material_id not in cp.payload.get("materials", []):
                continue
            if option == "reject" or a.material_id not in declared:
                if option == "reject" or not declared:
                    a.decision = AdmissionDecision.FORBID
                    a.reasons.append(f"人工不予准入（{by.id}）")
                    try:
                        self.rt.materials.purge(st.matter_id, a.material_id)
                    except FileNotFoundError:
                        pass
                continue
            cl = Clearance(declared[a.material_id])
            mat = self.rt.materials.get(st.matter_id, a.material_id)
            data_bytes = self.rt.materials.read_bytes(mat)
            res = scan(ScanInput(a.material_id, a.filename, parse_bytes(a.material_id, a.filename, data_bytes), cl), self.rt.config.environment.route, self.rt.config.environment.accept_internal_materials)
            if res.decision == AdmissionDecision.FORBID:
                a.decision = AdmissionDecision.FORBID
                a.reasons = res.reasons
                self.rt.materials.purge(st.matter_id, a.material_id)
            else:
                # 人工确认：申报属性，并确认隐藏内容、个人信息可以按最小必要原则处理
                a.decision = AdmissionDecision.ALLOW
                a.declared_clearance = cl
                a.detected_clearance = res.detected_clearance if res.detected_clearance != Clearance.UNKNOWN else cl
                a.confirmed_by = by.id
                a.confirmed_at = utcnow()
                mat.admission = AdmissionDecision.ALLOW
                mat.clearance = a.detected_clearance
                mat.declared_clearance = cl
                self.rt.materials.save_meta(mat)
            log.append("material.admission_confirmed", {"material_id": a.material_id, "decision": a.decision.value, "clearance": a.detected_clearance.value}, actor=by.id)
        AdmissionList.save(self.store, st.task_id, items)

    @staticmethod
    def _apply_spec_edits(spec: TaskSpec, data: dict) -> None:
        from ..schemas.common import slot
        from ..schemas.task import Organ

        if data.get("issuer"):
            spec.issuer = slot(Organ(name=data["issuer"], type=data.get("issuer_type", "未知")).model_dump(), "已确认", "人工修改")
        if data.get("recipients"):
            rs = data["recipients"] if isinstance(data["recipients"], list) else [x for x in str(data["recipients"]).replace("，", "、").split("、") if x]
            spec.recipients = slot([Organ(name=r).model_dump() for r in rs], "已确认", "人工修改")
        if data.get("cc"):
            cs = data["cc"] if isinstance(data["cc"], list) else [x for x in str(data["cc"]).replace("，", "、").split("、") if x]
            spec.cc = slot([Organ(name=c).model_dump() for c in cs], "已确认", "人工修改")
        if data.get("relation"):
            rel = data["relation"]
            mapping = {"上行": Direction.UP.value, "下行": Direction.DOWN.value, "平行": Direction.PARALLEL.value}
            spec.relation = slot(mapping.get(rel, rel), "已确认", "人工修改")
        if data.get("genre"):
            spec.suggested_genre = slot(data["genre"], "已确认", "人工修改")
        if data.get("subject"):
            spec.subject = slot(data["subject"], "已确认", "人工修改")
        if data.get("region"):
            spec.region = slot(data["region"], "已确认", "人工修改")
        if data.get("as_of"):
            from datetime import date

            spec.policy_as_of = date.fromisoformat(str(data["as_of"]))
            spec.policy_mode = "historical"
        for g in spec.gaps:
            if g.field in data or (g.field == "relation" and data.get("relation")):
                g.ask_user = False

    # ================================================================ 修订请求（人工发起）
    def request_revision(self, task_id: str, *, by: Principal, instruction: str | None = None, edits: list[dict] | None = None, fact_changes: list[dict] | None = None) -> TaskState:
        st = self.load_state(task_id)
        if not by.is_human:
            raise PermissionError("修订请求须由人发起")
        self._require(by, Action.IR_PATCH, st.matter_id)
        log = self.log(task_id)
        for cp in st.pending_checkpoints():
            if cp.kind in (CheckpointKind.HUMAN_REVIEW, CheckpointKind.REVIEW_ESCALATION):
                cp.status = "cancelled"
        self._revise(st, log, by, {"instruction": instruction, "edits": edits or [], "fact_changes": fact_changes or []})
        self.save_state(st)
        return st

    def _revise(self, st: TaskState, log: SessionLog, by: Principal, data: dict) -> None:
        sc = self.sc(st, log)
        ir = self.current_ir(st)
        skill = self.rt.skills.get("gongwen-targeted-revision")
        rnd = len([a for a in st.artifacts if a.startswith("human_patch_")]) + 1
        was_approved = st.stage == Stage.APPROVED or any(a.doc_id == ir.doc_id for a in st.approvals)
        ledger = self.load_matter_ledger(st)
        combined = PatchSet(doc_id=ir.doc_id, round=rnd, from_version=ir.version)
        work = ir
        all_olds: dict = {}
        reasons: list[str] = []
        for fc in data.get("fact_changes") or []:
            change = fc if isinstance(fc, FactChange) else FactChange(**fc, by=by.id) if "by" not in fc else FactChange(**fc)
            olds = skill.apply_fact_change(ledger, change)
            f = ledger.get(change.fact_id)
            reason = f"关键事实变更：{f.attribute} {olds['__before__']} → {f.display_value()}（{change.reason or '人工变更'}）"
            reasons.append(reason)
            ps = skill.propagate_values(sc, work, ledger, olds, reason, rnd)
            ps.fact_changes = [change]
            for k, v in olds.items():
                all_olds.setdefault(k, v)  # 保留最早的取值，供同一事项其他文稿联动
            combined.fact_changes += ps.fact_changes
            work = skill.apply(sc, work, ps)
            work.version = ir.version  # 版本在最后统一递增
            combined.patches += ps.patches
        if ledger is not None and data.get("fact_changes"):
            self.save_matter_ledger(st, ledger)
            sc.save("fact_ledger", ledger)
            # 同一事项的其他文稿：联动更新并退回审校（设计 §6.3）
            combined.affected_docs += self._propagate_to_siblings(st, by, ledger, all_olds, "；".join(reasons))
        for e in data.get("edits") or []:
            ps = skill.human_edit(sc, work, e["sid"], e["text"], by.id, rnd)
            work = skill.apply(sc, work, ps)
            work.version = ir.version
            combined.patches += ps.patches
        if data.get("instruction"):
            ps = skill.instruction(sc, work, data["instruction"], ledger, rnd)
            work = skill.apply(sc, work, ps)
            work.version = ir.version
            combined.patches += ps.patches
            combined.escalated_issue_ids += ps.escalated_issue_ids
        new = deepcopy(work)
        new.version = ir.version + 1
        new.based_on_version = ir.version
        new.status = DocStatus.DISCUSSION
        new.meta.update({"created_at": utcnow().isoformat(), "author": by.id, "summary": "人工发起的修订" + ("（含关键事实变更）" if data.get("fact_changes") else "")})
        combined.to_version = new.version
        self.store.save_version(st.task_id, new.doc_id, new.version, new)
        st.current_version = new.version
        sc.save(f"human_patch_{rnd}", combined)
        if was_approved:
            st.approvals = [a for a in st.approvals if a.doc_id != ir.doc_id]
            st.errors.append("已审批版本发生修改：须报原签批人复审（条例第二十五条（一））")
            log.append("approval.invalidated", {"doc_id": ir.doc_id, "from_version": ir.version}, actor=by.id)
        st.options["review_round"] = 0
        st.options["revision_round"] = 0
        st.budget.revision_rounds = 0
        st.options.pop("escalated_v", None)
        log.append("revision.requested", {"patches": len(combined.patches), "fact_changes": len(combined.fact_changes), "affected_docs": combined.affected_docs, "instruction": bool(data.get("instruction"))}, actor=by.id)
        self._transition(st, log, Stage.REVIEW, "人工发起修订后重新审校")

    def _matter_tasks(self, st: TaskState) -> list[TaskState]:
        out = []
        for tid in self.store.list_tasks():
            if tid == st.task_id:
                continue
            try:
                other = self.load_state(tid)
            except (KeyError, ValueError):
                continue
            if other.matter_id == st.matter_id:
                out.append(other)
        return out

    def _propagate_to_siblings(self, st: TaskState, by: Principal, ledger: FactLedger, olds: dict, reason: str) -> list[str]:
        """一处关键事实修改后，定位同一事项下引用了该事实（或其计算结果）的文稿，联动更新并退回审校。"""
        changed = {k for k in olds if not k.startswith("__")}
        affected: list[str] = []
        skill = self.rt.skills.get("gongwen-targeted-revision")
        for other in self._matter_tasks(st):
            oir = self.current_ir(other)
            if oir is None or other.stage in (Stage.FAILED, Stage.BLOCKED):
                continue
            if not any(r.id in changed for _, s in oir.iter_sentences() for r in s.refs):
                continue
            olog = self.log(other.task_id)
            osc = self.sc(other, olog)
            rnd = len([a for a in other.artifacts if a.startswith("human_patch_")]) + 1
            ps = skill.propagate_values(osc, oir, ledger, olds, f"同一事项其他文稿（{st.task_id}）{reason}", rnd)
            new = skill.apply(osc, oir, ps) if ps.patches else deepcopy(oir)
            new.version = oir.version + 1
            new.based_on_version = oir.version
            new.status = DocStatus.DISCUSSION
            new.meta.update({"created_at": utcnow().isoformat(), "author": by.id, "summary": f"同一事项关键事实变更联动（来自 {st.task_id}）"})
            ps.to_version = new.version
            self.store.save_version(other.task_id, new.doc_id, new.version, new)
            other.current_version = new.version
            osc.save(f"human_patch_{rnd}", ps)
            osc.save("fact_ledger", ledger)
            note = f"同一事项的任务 {st.task_id} 变更了关键事实（{reason}），本稿已联动更新，须重新审校并经人工确认"
            other.errors.append(note)
            if other.approvals:
                other.approvals = []
                other.errors.append("已审批版本因关键事实变更而修改：须报原签批人复审（条例第二十五条（一））")
                olog.append("approval.invalidated", {"doc_id": oir.doc_id, "from_version": oir.version, "cause": st.task_id}, actor=by.id)
            for cp in other.pending_checkpoints():
                cp.status = "cancelled"
            olog.append("matter.fact_changed", {"from_task": st.task_id, "facts": sorted(changed), "patches": len(ps.patches)}, actor=by.id)
            if other.stage not in (Stage.ADMISSION, Stage.TASK_CONFIRM, Stage.PARSING, Stage.EVIDENCE, Stage.OUTLINE_CONFIRM, Stage.DRAFTING):
                other.options["review_round"] = 0
                other.options["revision_round"] = 0
                other.budget.revision_rounds = 0
                self._transition(other, olog, Stage.REVIEW, f"同一事项关键事实变更（来自 {st.task_id}）")
            self.save_state(other)
            affected.append(oir.doc_id)
        return affected

    # ================================================================ 修改建议（模型通道提交，人工采纳）
    def propose_revision(self, task_id: str, *, by: Principal, instruction: str, reason: str = "") -> dict[str, Any]:
        """模型通道（对话代理、MCP 客户端）只能“提交修改建议”，不直接改稿；须由人工采纳后才进入定向修订。"""
        st = self.load_state(task_id)
        self._require(by, Action.PROPOSAL_SUBMIT, st.matter_id)
        instruction = (instruction or "").strip()
        if not instruction:
            raise ValueError("修改建议不能为空")
        if self.current_ir(st) is None:
            raise ValueError("尚未形成文稿，不能提交修改建议")
        if st.stage in (Stage.FAILED, Stage.BLOCKED):
            raise ValueError(f"任务处于“{st.stage.value}”，不能提交修改建议")
        item = {
            "proposal_id": st.ids.next("PR"),
            "instruction": instruction[:2000],
            "reason": (reason or "")[:500],
            "by": by.id,
            "at": utcnow().isoformat(),
            "version": st.current_version,
            "status": "pending",
        }
        st.options.setdefault("proposals", []).append(item)
        self.log(task_id).append(
            "proposal.submitted",
            {"proposal_id": item["proposal_id"], "instruction_sha256": sha256_text(instruction), "chars": len(instruction), "version": st.current_version},
            actor=by.id,
            stage=st.stage.value,
        )
        self.save_state(st)
        return item

    def proposals(self, task_id: str, status: str | None = None) -> list[dict[str, Any]]:
        st = self.load_state(task_id)
        return [p for p in st.options.get("proposals", []) if status is None or p.get("status") == status]

    def apply_proposal(self, task_id: str, proposal_id: str, *, by: Principal) -> TaskState:
        st = self.load_state(task_id)
        if not by.is_human:
            raise PermissionError("修改建议须由人工采纳，模型通道不能自行采纳")
        item = next((p for p in st.options.get("proposals", []) if p["proposal_id"] == proposal_id), None)
        if item is None or item.get("status") != "pending":
            raise KeyError(f"没有待采纳的修改建议：{proposal_id}")
        item.update({"status": "applied", "decided_by": by.id, "decided_at": utcnow().isoformat()})
        self.log(task_id).append("proposal.applied", {"proposal_id": proposal_id}, actor=by.id, stage=st.stage.value)
        self.save_state(st)
        return self.request_revision(task_id, by=by, instruction=item["instruction"])

    def reject_proposal(self, task_id: str, proposal_id: str, *, by: Principal, note: str = "") -> TaskState:
        st = self.load_state(task_id)
        if not by.is_human:
            raise PermissionError("修改建议须由人工处理")
        item = next((p for p in st.options.get("proposals", []) if p["proposal_id"] == proposal_id), None)
        if item is None or item.get("status") != "pending":
            raise KeyError(f"没有待处理的修改建议：{proposal_id}")
        item.update({"status": "rejected", "decided_by": by.id, "decided_at": utcnow().isoformat(), "note": note[:500]})
        self.log(task_id).append("proposal.rejected", {"proposal_id": proposal_id}, actor=by.id, stage=st.stage.value)
        self.save_state(st)
        return st

    # ================================================================ 审批记录绑定
    def import_approval(self, task_id: str, *, by: Principal, approver: str, approved_at: str, scope: str, source: str, approver_title: str = "") -> TaskState:
        st = self.load_state(task_id)
        if not by.is_human:
            raise PermissionError("审批记录只能由有权人员导入")
        self._require(by, Action.APPROVAL_IMPORT, st.matter_id)
        if st.stage not in (Stage.SUBMITTED,):
            raise ValueError("只能为已形成送审材料的文稿绑定审批记录")
        log = self.log(task_id)
        ir = self.current_ir(st)
        rec = ApprovalRecord(
            approval_id=st.ids.next("AP"),
            approver=approver,
            approver_title=approver_title,
            approved_at=approved_at,
            scope=scope,
            doc_id=ir.doc_id,
            doc_version=ir.version,
            doc_hash=stable_hash(ir),
            source=source,
            imported_by=by.id,
        )
        st.approvals.append(rec)
        ir.status = DocStatus.APPROVED_FOR_ISSUE
        self.store.save_version(st.task_id, ir.doc_id, ir.version, ir)
        rec.doc_hash = stable_hash(ir)
        log.append("approval.imported", {"approval_id": rec.approval_id, "doc_id": ir.doc_id, "version": ir.version, "doc_hash": rec.doc_hash, "source": source}, actor=by.id)
        self._transition(st, log, Stage.APPROVED, "已绑定真实审批记录")
        self.save_state(st)
        self.package(st, log)
        self.save_state(st)
        return st

    # ================================================================ 事项级事实账本
    def _matter_dir(self, st: TaskState) -> Path:
        d = Path(self.rt.config.environment.data_dir) / "matters" / safe_id(st.matter_id, "事项标识")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _matter_ids(self, st: TaskState) -> IdAllocator:
        p = self._matter_dir(st) / "ids.json"
        return IdAllocator.model_validate_json(p.read_text(encoding="utf-8")) if p.is_file() else IdAllocator()

    def _save_matter_ids(self, st: TaskState, ids: IdAllocator) -> None:
        (self._matter_dir(st) / "ids.json").write_text(ids.model_dump_json(), encoding="utf-8")

    def load_matter_ledger(self, st: TaskState) -> FactLedger | None:
        p = self._matter_dir(st) / "ledger.json"
        return FactLedger.model_validate_json(p.read_text(encoding="utf-8")) if p.is_file() else None

    def save_matter_ledger(self, st: TaskState, ledger: FactLedger) -> None:
        (self._matter_dir(st) / "ledger.json").write_text(ledger.model_dump_json(indent=2), encoding="utf-8")

    def merge_matter_ledger(self, st: TaskState, new: FactLedger) -> FactLedger:
        """同一事项的多份文稿共享经确认的事实：相同来源与陈述的事实沿用原编号与核验状态。"""
        current = self.load_matter_ledger(st) or FactLedger()
        ids = self._matter_ids(st)
        shas = {m.material_id: m.sha256 for m in self.rt.materials.list(st.matter_id)}

        def key(f):  # 同一份文件（按内容哈希）中同一位置的同一陈述，视为同一事实
            src = f.sources[0] if f.sources else None
            origin = shas.get(src.material_id, src.material_id) if src else ""
            return (f.statement, origin, src.path if src else "", str(f.value), f.unit)

        existing = {key(f): f for f in current.facts}
        mapping: dict[str, str] = {}
        for f in new.facts:
            old = existing.get(key(f))
            if old is not None:
                mapping[f.fact_id] = old.fact_id
            else:
                nid = ids.next("F")
                mapping[f.fact_id] = nid
        merged = FactLedger(facts=list(current.facts), conflicts=list(current.conflicts), calc_checks=list(current.calc_checks), unknowns=list(current.unknowns))
        known = {f.fact_id for f in merged.facts}
        for f in new.facts:
            nid = mapping[f.fact_id]
            if nid in known:
                continue
            g = f.model_copy(deep=True)
            g.fact_id = nid
            g.depends_on = [mapping.get(x, x) for x in g.depends_on]
            if g.formula:
                g.formula.inputs = [mapping.get(x, x) for x in g.formula.inputs]
                g.formula.expression = " + ".join(g.formula.inputs) if "+" in f.formula.expression else g.formula.expression
            merged.facts.append(g)
            known.add(nid)
        seen_conf = {tuple(sorted(c.fact_ids)) for c in merged.conflicts}
        for c in new.conflicts:
            ids_ = sorted(mapping.get(x, x) for x in c.fact_ids)
            if tuple(ids_) not in seen_conf:
                c2 = c.model_copy(deep=True)
                c2.fact_ids = ids_
                merged.conflicts.append(c2)
        seen_k = {(c.kind, c.description) for c in merged.calc_checks}
        merged.calc_checks += [c for c in new.calc_checks if (c.kind, c.description) not in seen_k]
        merged.unknowns = sorted(set(merged.unknowns) | set(new.unknowns))
        self._save_matter_ids(st, ids)
        self.save_matter_ledger(st, merged)
        return merged

    # ================================================================ 查询
    def status(self, task_id: str) -> dict[str, Any]:
        st = self.load_state(task_id)
        sc = self.sc(st, self.log(task_id))
        report = sc.load("review_report", ReviewReport)
        out = {
            "task_id": st.task_id,
            "matter_id": st.matter_id,
            "stage": st.stage.value,
            "doc_status": st.doc_status.value,
            "version": st.current_version,
            "pending_checkpoints": [{"cp_id": c.cp_id, "kind": c.kind.value, "question": c.question, "details": c.details, "options": [f"{o.key}={o.label}" for o in c.options]} for c in st.pending_checkpoints()],
            "pending_proposals": [{k: v for k, v in p.items() if k in ("proposal_id", "instruction", "reason", "by", "version")} for p in st.options.get("proposals", []) if p.get("status") == "pending"],
            "issue_counts": report.counts() if report else {},
            "errors": st.errors,
            "budget": st.budget.model_dump(),
            "outputs": str(self.store.out_dir(task_id)),
            "models": sc.router.describe(),
        }
        return out


def default_user(user_id: str = "local-user") -> Principal:
    """本地单人使用时的默认人工主体（经办人 + 审核人）。审批导入需显式授予 approver。"""
    return human(user_id, "drafter", "reviewer")


__all__ = ["Engine", "default_user", "channel"]

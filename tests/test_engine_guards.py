"""状态机守卫回归：权限与事项范围、审核节点不可被绕过、修订阶段、材料删除、钩子、并发与标识等。"""

import http.client
import json
import threading
import time

import pytest
from helpers import make_docx, make_xlsx
from test_engine_e2e import BUDGET, SITUATION, make_engine, pending

from gongwen.harness.hooks import HookRunner
from gongwen.harness.permissions import Action, channel, human
from gongwen.harness.session import SessionLog
from gongwen.kernel.config import HooksConfig
from gongwen.kernel.context import Context
from gongwen.knowledge.stores import safe_id
from gongwen.orchestrator import Engine, default_user
from gongwen.orchestrator.engine import AdmissionList
from gongwen.runtime import build_runtime
from gongwen.schemas.common import AdmissionDecision, Clearance
from gongwen.schemas.outline import OutlinePlan
from gongwen.schemas.patch import Patch, PatchSet
from gongwen.schemas.state import CheckpointKind, Stage
from gongwen.workbench import build_page
from gongwen.workbench.server import serve

AA = {"task_confirm", "outline_confirm", "review_escalation"}


def start(eng, user, matter_id=None):
    st = eng.create_task("写一份向主管部门申请基层医疗示范点建设经费的报告", by=user, matter_id=matter_id, hints={"recipients": "示例市人民政府", "issuer_type": "政府部门"})
    eng.add_material(st.task_id, "情况说明.docx", make_docx(SITUATION), by=user, declared=Clearance.PUBLIC)
    eng.add_material(st.task_id, "经费测算表.xlsx", make_xlsx(BUDGET, sheet="经费测算"), by=user, declared=Clearance.PUBLIC)
    return eng.load_state(st.task_id)


def drafted(eng, user, **kw):
    st = start(eng, user, **kw)
    st = eng.advance(st.task_id, by=user, auto_accept=AA)
    assert st.stage == Stage.HUMAN_REVIEW
    return st


def events(eng, task_id, type_):
    return list(eng.log(task_id).replay({type_}))


def sibling_with_draft(eng, user, a):
    """同一事项下已形成文稿的第二份文稿（函）。"""
    b = eng.create_task("给示例市财政局发函，商请支持基层医疗示范点建设经费", by=user, matter_id=a.matter_id, hints={"recipients": "示例市财政局", "issuer_type": "政府部门"})
    b = eng.advance(b.task_id, by=user, auto_accept=AA)
    assert b.stage == Stage.HUMAN_REVIEW
    return b


def used_money_fact(eng, st):
    ir, ledger = eng.current_ir(st), eng.load_matter_ledger(st)
    used = {r.id for _, s in ir.iter_sentences() for r in s.refs}
    return next(f for f in ledger.facts if f.fact_id in used and f.kind == "money")


# ---------------------------------------------------------------- 1. 同一事项联动不能作废其他任务的审核节点
def test_sibling_fact_change_keeps_unconfirmed_outline(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    a = drafted(eng, user)
    b = sibling_with_draft(eng, user, a)
    eng.add_material(b.task_id, "补充.txt", "补充说明：项目已纳入年度计划。".encode(), by=user, declared=Clearance.PUBLIC)
    b = eng.advance(b.task_id, by=user)  # 重新准入后停在新提纲的确认节点
    assert b.stage == Stage.OUTLINE_CONFIRM
    cp = pending(eng, b.task_id, CheckpointKind.OUTLINE_CONFIRM)
    eng.request_revision(a.task_id, by=user, fact_changes=[{"fact_id": used_money_fact(eng, b).fact_id, "new_value": 10, "reason": "核减"}])
    b = eng.load_state(b.task_id)
    assert b.stage == Stage.OUTLINE_CONFIRM and b.checkpoint(cp.cp_id).status == "pending"
    assert events(eng, b.task_id, "matter.fact_changed")[-1]["payload"]["redraft"] is True
    b = eng.advance(b.task_id, by=user)  # 未经确认的新提纲不能被起草
    assert b.stage == Stage.OUTLINE_CONFIRM and b.current_version == 1
    # 最近一次同类节点已作废时不回退到更早的确认
    b.checkpoint(cp.cp_id).status = "cancelled"
    assert eng._resolved(b, CheckpointKind.OUTLINE_CONFIRM) is None


def test_sibling_fact_change_keeps_authority_checkpoint(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    a = drafted(eng, user)
    b = sibling_with_draft(eng, user, a)
    fact = used_money_fact(eng, b)
    # 模拟重新准入后发现权限问题：已有旧稿，等待人工处理权限节点
    log = eng.log(b.task_id)
    for c in b.pending_checkpoints():
        c.status = "cancelled"
    eng._transition(b, log, Stage.OUT_OF_AUTHORITY, "测试")
    auth = eng._checkpoint(b, log, CheckpointKind.AUTHORITY, "发现超出权限问题", [], {"resume_stage": Stage.EVIDENCE.value})
    eng.save_state(b)
    eng.request_revision(a.task_id, by=user, fact_changes=[{"fact_id": fact.fact_id, "new_value": 10, "reason": "核减"}])
    b = eng.load_state(b.task_id)
    assert b.stage == Stage.OUT_OF_AUTHORITY and b.checkpoint(auth.cp_id).status == "pending"
    b = eng.advance(b.task_id, by=user, auto_accept=AA | {"conflict"})
    assert b.stage == Stage.OUT_OF_AUTHORITY and [c.kind for c in b.pending_checkpoints()] == [CheckpointKind.AUTHORITY]


# ---------------------------------------------------------------- 2/3. 自动接受与推进须有权限与事项范围
def test_auto_accept_requires_checkpoint_permission(tmp_path):
    eng = make_engine(tmp_path)
    owner = default_user()
    st = start(eng, owner, matter_id="M2")
    eng.rt.permissions.role_grants["clerk"] = {Action.TASK_WRITE, Action.MATERIAL_READ}  # 能推进，不能处理审核节点
    clerk = human("clerk", "clerk")
    st = eng.advance(st.task_id, by=clerk, auto_accept=AA)
    assert st.stage == Stage.TASK_CONFIRM
    assert [c.kind for c in st.pending_checkpoints()] == [CheckpointKind.TASK_CONFIRM]
    assert not any(c.status == "resolved" for c in st.checkpoints)
    # 无事项权限的经办人同样不能借自动接受处理本事项的节点
    other = human("other", "drafter", matters={"M1"})
    with pytest.raises(PermissionError):
        eng.advance(st.task_id, by=other, auto_accept=AA)
    assert eng.load_state(st.task_id).pending_checkpoints()[0].status == "pending"


def test_create_task_and_advance_check_matter_scope(tmp_path):
    eng = make_engine(tmp_path)
    owner = default_user()
    st = start(eng, owner, matter_id="M2")
    outsider = human("outsider", "drafter", matters={"M1"})
    with pytest.raises(PermissionError):
        eng.create_task("写一份申请经费的请示", by=outsider, matter_id="M2")
    with pytest.raises(PermissionError):
        eng.advance(st.task_id, by=outsider)
    with pytest.raises(PermissionError):
        eng.advance(st.task_id, by=human("rev", "reviewer"))  # 推进任务须有 task.write
    assert eng.load_state(st.task_id).stage == Stage.ADMISSION
    # 模型通道照常推进（停在人工节点）
    assert eng.advance(st.task_id, by=channel("mcp")).stage == Stage.TASK_CONFIRM


# ---------------------------------------------------------------- 4. 汇聚风险“不准入”不是确认
def test_aggregation_reject_blocks_task(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    st = start(eng, user)
    log = eng.log(st.task_id)
    st.options["aggregation_ack"] = "pending"
    cp = eng._checkpoint(st, log, CheckpointKind.MATERIAL_CONFIRM, "多份材料汇聚后可能形成更高敏感度的信息集合", [], {"aggregation": True, "materials": []})
    eng.save_state(st)
    st = eng.resolve_checkpoint(st.task_id, cp.cp_id, "reject", by=user, note="汇聚风险过高")
    assert st.stage == Stage.BLOCKED and "aggregation_ack" not in st.options
    assert "汇聚" in st.exception_reason
    assert eng.advance(st.task_id, by=user).stage == Stage.BLOCKED


# ---------------------------------------------------------------- 5/16/17. 材料准入确认：权限、访问级别、申报数据
def _need_confirm(eng, user):
    st = eng.create_task("写一份情况报告", by=user, hints={"recipients": "示例市人民政府"})
    eng.add_material(st.task_id, "情况说明.docx", make_docx(SITUATION), by=user, declared=None)
    st = eng.advance(st.task_id, by=user)
    return st, pending(eng, st.task_id, CheckpointKind.MATERIAL_CONFIRM)


def _admission(eng, task_id):
    return AdmissionList.load(eng.store, task_id)[0]


def test_material_confirm_requires_admission_grant_and_clearance(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    st, cp = _need_confirm(eng, user)
    with pytest.raises(PermissionError):  # 审核人未被授予 admission.confirm
        eng.resolve_checkpoint(st.task_id, cp.cp_id, "confirm", by=human("rev", "reviewer"), data={"materials": {"MAT-001": "公开"}})
    low = human("low", "drafter", max_clearance=Clearance.PUBLIC)
    with pytest.raises(PermissionError):  # 申报属性高于本人的访问级别
        eng.resolve_checkpoint(st.task_id, cp.cp_id, "confirm", by=low, data={"materials": {"MAT-001": "内部"}})
    with pytest.raises(PermissionError):
        eng.add_material(st.task_id, "内部.txt", "内部安排".encode(), by=low, declared=Clearance.INTERNAL)
    with pytest.raises(ValueError):  # 无效属性：先校验，不留下“已处理”的审计记录
        eng.resolve_checkpoint(st.task_id, cp.cp_id, "confirm", by=user, data={"materials": {"MAT-001": "绝密级"}})
    assert eng.load_state(st.task_id).checkpoint(cp.cp_id).status == "pending"
    assert not events(eng, st.task_id, "checkpoint.resolved")
    assert _admission(eng, st.task_id).decision == AdmissionDecision.NEED_CONFIRM
    st = eng.resolve_checkpoint(st.task_id, cp.cp_id, "confirm", by=user, data={"materials": {"MAT-001": "公开"}})
    assert _admission(eng, st.task_id).decision == AdmissionDecision.ALLOW


def test_material_confirm_without_declarations_is_rejected(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    st, cp = _need_confirm(eng, user)
    with pytest.raises(ValueError):
        eng.resolve_checkpoint(st.task_id, cp.cp_id, "confirm", by=user)
    assert eng.load_state(st.task_id).checkpoint(cp.cp_id).status == "pending"
    assert not events(eng, st.task_id, "checkpoint.resolved")
    assert [m.material_id for m in eng.rt.materials.list(st.matter_id)] == ["MAT-001"]  # 未被删除


# ---------------------------------------------------------------- 6. 修订只能在形成文稿后的审校至送审阶段发起
def test_revision_only_from_review_stages(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    st = drafted(eng, user)
    eng.add_material(st.task_id, "补充说明.txt", "补充：2026年拟新建示范点调整为20个。".encode(), by=user, declared=Clearance.PUBLIC)
    assert eng.load_state(st.task_id).stage == Stage.ADMISSION
    with pytest.raises(ValueError):  # 追加材料后须重新准入、解析，不能直接跳到审校
        eng.request_revision(st.task_id, by=user, instruction="语言再精炼一些")
    assert eng.load_state(st.task_id).stage == Stage.ADMISSION

    st2 = drafted(eng, user)
    st2 = eng.resolve_checkpoint(st2.task_id, pending(eng, st2.task_id, CheckpointKind.HUMAN_REVIEW).cp_id, "abort", by=user, note="不再办理")
    with pytest.raises(ValueError):  # 已终止的任务不能被修订“复活”
        eng.request_revision(st2.task_id, by=user, instruction="精炼")
    assert eng.load_state(st2.task_id).stage == Stage.FAILED

    st3 = eng.create_task("写一份通知", by=user, hints={"recipients": "各科室"})
    with pytest.raises(ValueError):  # 尚无文稿：明确报错而不是 AttributeError
        eng.request_revision(st3.task_id, by=user, instruction="x")


# ---------------------------------------------------------------- 7. 删除不予准入的材料不影响共享同一原始文件的材料
def test_purge_keeps_blob_shared_with_admitted_material(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    st = eng.create_task("请示", by=user, hints={"recipients": "示例市人民政府", "issuer_type": "政府部门"})
    data = make_docx(SITUATION)
    a = eng.add_material(st.task_id, "情况说明.docx", data, by=user, declared=Clearance.PUBLIC)
    b = eng.add_material(st.task_id, "情况说明-副本.docx", data, by=user, declared=Clearance.INTERNAL)
    assert a.decision == AdmissionDecision.ALLOW and b.decision == AdmissionDecision.FORBID
    mats = eng.rt.materials.list(st.matter_id)
    assert [m.material_id for m in mats] == ["MAT-001"] and mats[0].admission == AdmissionDecision.ALLOW
    assert eng.rt.materials.read_bytes(mats[0]) == data
    assert eng.advance(st.task_id, by=user).stage == Stage.TASK_CONFIRM


# ---------------------------------------------------------------- 8. 重建提纲的标记只生效一次
def test_outline_rebuild_flag_cleared_and_edits_kept(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    st = start(eng, user)
    st = eng.advance(st.task_id, by=user, auto_accept={"task_confirm"})
    cp1 = pending(eng, st.task_id, CheckpointKind.OUTLINE_CONFIRM)
    eng.resolve_checkpoint(st.task_id, cp1.cp_id, "edit", by=user, data={"add_facts": [{"statement": "资金来源为市级财政专项", "kind": "text"}]})
    st = eng.advance(st.task_id, by=user)
    assert "rebuild_outline" not in st.options
    cp2 = pending(eng, st.task_id, CheckpointKind.OUTLINE_CONFIRM)
    outline = eng.store.load_model(st.task_id, "outline", OutlinePlan)
    assert outline.measures
    drop = outline.measures[0].measure_id
    eng.resolve_checkpoint(st.task_id, cp2.cp_id, "edit", by=user, data={"drop_measures": [drop], "alternative": "B"})
    st = eng.advance(st.task_id, by=user)
    assert not any(c.kind == CheckpointKind.OUTLINE_CONFIRM for c in st.pending_checkpoints())
    final = eng.store.load_model(st.task_id, "outline", OutlinePlan)
    assert drop not in [m.measure_id for m in final.measures] and final.chosen_alternative == "B"


# ---------------------------------------------------------------- 9. 过时的修订建议
def _stale_proposal(eng, st, sid, before):
    ir = eng.current_ir(st)
    patch = Patch(patch_id="PA-900", doc_id=ir.doc_id, target=sid, op="replace", before=before, after="模型建议：设备问题已全部解决。", reason="模型建议", status="needs_human")
    eng.sc(st, eng.log(st.task_id)).save("pending_proposals", PatchSet(doc_id=ir.doc_id, round=1, from_version=ir.version, patches=[patch]))


def test_human_revision_clears_proposals_and_stale_patch_is_not_applied(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    st = drafted(eng, user)
    sid, old = next((s.sid, s.text) for _, s in eng.current_ir(st).iter_sentences() if "设备老化" in s.text)
    _stale_proposal(eng, st, sid, old)
    eng.save_state(st)
    human_text = "人工改写：部分设备购置于2010年前，亟待更新。"
    cp = pending(eng, st.task_id, CheckpointKind.HUMAN_REVIEW)
    st = eng.resolve_checkpoint(st.task_id, cp.cp_id, "revise", by=user, data={"edits": [{"sid": sid, "text": human_text}]})
    assert not eng.store.load_model(st.task_id, "pending_proposals", PatchSet).patches  # 人工修订后旧建议作废
    st = eng.advance(st.task_id, by=user)
    assert eng.current_ir(st).find_sentence(sid)[1].text == human_text
    # 即使旧建议仍在：采纳时逐句核对原文，不覆盖人工文字
    st = eng.load_state(st.task_id)
    _stale_proposal(eng, st, sid, old)
    esc = eng._checkpoint(st, eng.log(st.task_id), CheckpointKind.REVIEW_ESCALATION, "以下问题需要人工处理", [])
    eng.save_state(st)
    st = eng.resolve_checkpoint(st.task_id, esc.cp_id, "apply", by=user)
    assert eng.current_ir(st).find_sentence(sid)[1].text == human_text
    assert any("PA-900" in e and "过时" in e for e in st.errors)


# ---------------------------------------------------------------- 10. 钩子
def test_stage_enter_hook_blocks_stage(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    eng.rt.ctx.on("hook/StageEnter", lambda subject, payload: "未经科室负责人同意不得进入起草" if subject == Stage.DRAFTING.value else None)
    st = start(eng, user)
    st = eng.advance(st.task_id, by=user, auto_accept=AA)
    assert st.stage == Stage.FAILED and st.current_version == 0
    assert "未经科室负责人同意" in st.exception_reason


def test_session_start_hook_blocks_task_creation(tmp_path):
    eng = make_engine(tmp_path)
    eng.rt.ctx.on("hook/SessionStart", lambda subject, payload: "本单位暂停受理")
    with pytest.raises(PermissionError):
        eng.create_task("写一份通知", by=default_user())
    assert eng.store.list_tasks() == []


def test_hook_allow_value_does_not_hide_later_block():
    ctx = Context()
    ctx.on("hook/PreToolUse", lambda s, p: False, priority=10)
    ctx.on("hook/PreToolUse", lambda s, p: "阻断导出", priority=0)
    res = HookRunner(HooksConfig(), ctx).run("PreToolUse", "export_docx", {})
    assert res.blocked and res.reason == "阻断导出"
    assert ctx.bail("hook/PreToolUse", "x", {}) is False  # 通用 bail 语义不变


# ---------------------------------------------------------------- 11. 任务确认阶段追加材料须重新准入
def test_material_added_at_task_confirm_is_readmitted(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    st = start(eng, user)
    st = eng.advance(st.task_id, by=user)
    old_cp = pending(eng, st.task_id, CheckpointKind.TASK_CONFIRM)
    r = eng.add_material(st.task_id, "补充经费说明.docx", make_docx(["补充说明：资金来源为市级财政专项资金。"]), by=channel("agent"), declared=None)
    assert r.decision == AdmissionDecision.NEED_CONFIRM
    st = eng.load_state(st.task_id)
    assert st.stage == Stage.ADMISSION and st.checkpoint(old_cp.cp_id).status == "cancelled"
    st = eng.advance(st.task_id, by=user, auto_accept=AA)
    cp = pending(eng, st.task_id, CheckpointKind.MATERIAL_CONFIRM)
    assert r.material_id in cp.payload["materials"]
    eng.resolve_checkpoint(st.task_id, cp.cp_id, "confirm", by=user, data={"materials": {r.material_id: "公开"}})
    st = eng.advance(st.task_id, by=user)
    assert st.stage == Stage.TASK_CONFIRM  # 任务契约按新材料重新生成并重新确认
    assert pending(eng, st.task_id, CheckpointKind.TASK_CONFIRM).cp_id != old_cp.cp_id


# ---------------------------------------------------------------- 12. 标识不冲突
def test_task_and_matter_ids_unique_within_same_millisecond(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    states = [eng.create_task(f"任务{i}", by=user) for i in range(60)]
    assert len({s.task_id for s in states}) == 60 and len({s.matter_id for s in states}) == 60
    assert len(eng.store.list_tasks()) == 60
    for st in states:
        for value in (st.task_id, st.matter_id, f"D{st.task_id[1:]}"):
            assert safe_id(value) == value


# ---------------------------------------------------------------- 13. 日志链与并发处理
def test_session_log_instances_share_one_chain(tmp_path):
    path = tmp_path / "events.jsonl"
    a, b = SessionLog(path), SessionLog(path)
    a.append("x", {"n": 1})
    b.append("y", {"n": 2})
    a.append("z", {"n": 3})

    def worker(i):
        log = SessionLog(path)
        for j in range(10):
            log.append("t", {"i": i, "j": j})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert SessionLog(path).verify_chain() == (True, None)
    assert [r["seq"] for r in SessionLog(path).replay()] == list(range(1, 44))


def test_concurrent_resolution_happens_once(tmp_path, monkeypatch):
    eng = make_engine(tmp_path)
    user = default_user()
    st = start(eng, user)
    st = eng.advance(st.task_id, by=user)
    cp = pending(eng, st.task_id, CheckpointKind.TASK_CONFIRM)
    original = Engine._resolve

    def slow(self, *args, **kwargs):
        time.sleep(0.3)  # 扩大读-改-写窗口
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Engine, "_resolve", slow)
    results = []

    def resolve():
        try:
            eng.resolve_checkpoint(st.task_id, cp.cp_id, "accept", by=user)
            results.append("ok")
        except KeyError:
            results.append("gone")

    threads = [threading.Thread(target=resolve) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["gone", "ok"]
    assert len([r for r in events(eng, st.task_id, "checkpoint.resolved") if r["payload"]["cp_id"] == cp.cp_id]) == 1


# ---------------------------------------------------------------- 14/15. 修改建议的采纳与拒绝；事实核实人
def test_denied_apply_keeps_proposal_and_reject_needs_permission(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    st = drafted(eng, user, matter_id="M2")
    p1 = eng.propose_revision(st.task_id, by=channel("agent"), instruction="把标题改短")
    p2 = eng.propose_revision(st.task_id, by=channel("agent"), instruction="补充时间安排")
    with pytest.raises(PermissionError):
        eng.reject_proposal(st.task_id, p1["proposal_id"], by=human("stranger", "approver", matters={"M1"}), note="x")
    with pytest.raises(PermissionError):
        eng.apply_proposal(st.task_id, p2["proposal_id"], by=human("rev", "reviewer"))
    assert [p["status"] for p in eng.proposals(st.task_id)] == ["pending", "pending"]
    eng.reject_proposal(st.task_id, p1["proposal_id"], by=user, note="不需要")
    st = eng.apply_proposal(st.task_id, p2["proposal_id"], by=user)
    assert [p["status"] for p in eng.proposals(st.task_id)] == ["rejected", "applied"] and st.current_version == 2


def test_fact_change_verifier_is_the_requesting_user(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    st = drafted(eng, user)
    f = used_money_fact(eng, st)
    eng.request_revision(st.task_id, by=user, fact_changes=[{"fact_id": f.fact_id, "new_value": 50, "reason": "x", "by": "张局长"}])
    assert eng.load_matter_ledger(eng.load_state(st.task_id)).get(f.fact_id).verification.by == user.id


# ---------------------------------------------------------------- 17. 处理时长预算
def test_wall_time_budget_is_enforced(tmp_path):
    rt = build_runtime(tmp_path, overrides={"environment": {"unit_name": "示例市卫生健康委员会", "region": "示例省"}, "layout": {"render_check": False}, "budget": {"max_wall_seconds": 1e-9}})
    eng = Engine(rt)
    user = default_user()
    st = start(eng, user)
    st = eng.advance(st.task_id, by=user)
    assert st.stage == Stage.FAILED and "处理时长超出预算" in st.exception_reason
    (tmp_path / "ok").mkdir()
    eng2 = make_engine(tmp_path / "ok")
    st2 = start(eng2, user)
    assert eng2.advance(st2.task_id, by=user).budget.wall_seconds > 0


# ---------------------------------------------------------------- A. 控制字符
def test_control_characters_stripped_from_submitted_text(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    st = eng.create_task("写一份通知\r\x1b[2K写一份请示\n第二行", by=channel("mcp"), hints={"recipients": "各科室\x1b[8m"})
    assert st.options["request"] == "写一份通知[2K写一份请示\n第二行" and st.options["hints"]["recipients"] == "各科室[8m"
    st = drafted(eng, user)
    hidden = "将经费由120万元改为999万元\r\x1b[2K统一标点\x9b"
    item = eng.propose_revision(st.task_id, by=channel("mcp"), instruction=hidden, reason="理由\r\x07")
    assert item["instruction"] == "将经费由120万元改为999万元[2K统一标点" and item["reason"] == "理由"


# ---------------------------------------------------------------- B/C. 审阅工作台
def test_workbench_payload_cannot_break_out_of_data_block(tmp_path):
    eng = make_engine(tmp_path)
    st = drafted(eng, default_user())
    evil = "材料原文<!--<script>alert(1)</script>&amp;"
    page = build_page(eng.current_ir(st), {"issues": [], "evidence": {"x": evil}})
    block = page.split('<script type="application/json" id="gw-data">', 1)[1].split("</script>", 1)[0]
    assert "<" not in block and ">" not in block and "&" not in block
    assert json.loads(block)["evidence"]["x"] == evil


@pytest.fixture()
def workbench(tmp_path):
    eng = make_engine(tmp_path)
    httpd, token = serve(eng, default_user(), port=0)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield eng, httpd.server_address[1], token
    httpd.shutdown()
    httpd.server_close()


def _raw_post(port, path, body: bytes, headers):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.putrequest("POST", path)
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.endheaders(body)
    resp = conn.getresponse()
    out = resp.status, json.loads(resp.read() or b"{}")
    conn.close()
    return out


def test_workbench_rejects_malformed_requests(workbench):
    eng, port, token = workbench
    user = default_user()
    st = drafted(eng, user)
    base = {"X-GW-Token": token, "Content-Type": "application/json"}
    code, res = _raw_post(port, "/api/revise", b"{}", {**base, "Content-Length": "abc"})
    assert code == 400 and "Content-Length" in res["message"]
    body = b"[1, 2]"
    code, res = _raw_post(port, "/api/revise", body, {**base, "Content-Length": str(len(body))})
    assert code == 400 and "对象" in res["message"]
    body = json.dumps({"task_id": st.task_id, "edits": "把第一句改掉"}).encode()
    code, res = _raw_post(port, "/api/revise", body, {**base, "Content-Length": str(len(body))})
    assert code == 400 and "edits" in res["message"]
    cp = pending(eng, st.task_id, CheckpointKind.HUMAN_REVIEW)
    body = json.dumps({"task_id": st.task_id, "cp_id": cp.cp_id, "option": "submit", "data": [1]}).encode()
    code, res = _raw_post(port, "/api/checkpoint", body, {**base, "Content-Length": str(len(body))})
    assert code == 400
    st = eng.load_state(st.task_id)
    assert st.stage == Stage.HUMAN_REVIEW and st.current_version == 1


def test_all_forbidden_materials_pause_for_more_material(tmp_path):
    """已添加的材料全部禁止进入：停在“待补材料”，不悄悄改为无材料起草；由人决定后才继续。"""
    eng = make_engine(tmp_path)
    user = default_user()
    st = eng.create_task("写一份向主管部门申请基层医疗示范点建设经费的报告", by=user, hints={"recipients": "示例市人民政府", "issuer_type": "政府部门"})
    eng.add_material(st.task_id, "情况说明.md", "绝密★1年\n内部情况说明。".encode(), by=user, declared=Clearance.PUBLIC)
    st = eng.advance(st.task_id, by=user, auto_accept=AA)
    assert st.stage == Stage.NEED_MATERIAL
    cp = next(c for c in st.pending_checkpoints() if c.kind == CheckpointKind.NEED_MATERIAL)
    assert any("禁止进入" in d for d in cp.details)
    eng.resolve_checkpoint(st.task_id, cp.cp_id, cp.options[0].key, by=user)
    st = eng.advance(st.task_id, by=user, auto_accept=AA)
    assert st.stage not in (Stage.ADMISSION, Stage.NEED_MATERIAL)

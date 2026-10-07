"""端到端：从需求到送审稿的完整流程（离线确定性路径 + 脚本化模型路径）。"""

import shutil

import pytest
from helpers import make_docx, make_xlsx

from gongwen.harness.permissions import channel, human
from gongwen.llm import ScriptedProvider
from gongwen.orchestrator import Engine, default_user
from gongwen.runtime import build_runtime
from gongwen.schemas.common import AdmissionDecision, Clearance, DocStatus
from gongwen.schemas.facts import FactStatus
from gongwen.schemas.review import IssueType, ReviewReport
from gongwen.schemas.state import CheckpointKind, Stage

SITUATION = [
    "关于基层医疗示范点建设的情况说明",
    "为提升基层医疗卫生服务能力，我委于2025年启动基层医疗示范点建设。",
    "截至2025年12月31日，我委已建成基层医疗示范点8个，覆盖5个区。",
    "目前存在设备老化、服务能力不足等问题。",
    "2026年拟新建示范点12个，计划于2026年12月底前完成建设。",
]
BUDGET = [["项目", "金额（万元）"], ["设备购置", 60], ["场地改造", 40], ["人员培训", 20], ["合计", 120]]


def make_engine(tmp_path, render=False, providers=None, **env):
    overrides = {
        "environment": {"unit_name": "示例市卫生健康委员会", "region": "示例省", **env},
        "layout": {"render_check": render},
    }
    rt = build_runtime(tmp_path, overrides=overrides, model_providers=providers)
    return Engine(rt)


def start(eng, user, request="写一份向主管部门申请基层医疗示范点建设经费的报告", recipients="示例市人民政府", **hints):
    st = eng.create_task(request, by=user, hints={"recipients": recipients, "issuer_type": "政府部门", **hints})
    eng.add_material(st.task_id, "情况说明.docx", make_docx(SITUATION), by=user, declared=Clearance.PUBLIC)
    eng.add_material(st.task_id, "经费测算表.xlsx", make_xlsx(BUDGET, sheet="经费测算"), by=user, declared=Clearance.PUBLIC)
    return st


def pending(eng, task_id, kind):
    st = eng.load_state(task_id)
    return next(c for c in st.pending_checkpoints() if c.kind == kind)


def run_to_review(eng, user, task_id):
    st = eng.advance(task_id, by=user)
    assert st.stage == Stage.TASK_CONFIRM
    cp = pending(eng, task_id, CheckpointKind.TASK_CONFIRM)
    assert any("更适合“请示”" in d for d in cp.details)  # 用户说“报告”，实质是请求批准
    eng.resolve_checkpoint(task_id, cp.cp_id, "accept", by=user)
    st = eng.advance(task_id, by=user)
    assert st.stage == Stage.OUTLINE_CONFIRM
    ledger = eng.load_matter_ledger(st)
    money = [f.fact_id for f in ledger.facts if f.kind == "money" and f.status == FactStatus.RECORDED]
    cp = pending(eng, task_id, CheckpointKind.OUTLINE_CONFIRM)
    eng.resolve_checkpoint(task_id, cp.cp_id, "edit", by=user, data={"confirm_facts": money})
    return eng.advance(task_id, by=user)


def test_full_flow_report_becomes_qingshi(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    st = start(eng, user)
    st = run_to_review(eng, user, st.task_id)
    assert st.stage == Stage.HUMAN_REVIEW
    ir = eng.current_ir(st)
    assert ir.genre == "请示" and ir.title.endswith("的请示")
    text = ir.full_text()
    assert "已建成基层医疗示范点8个" in text  # 已核实/记载事实逐字取自材料
    assert "拟新建示范点12个" in text  # 拟议事项保留拟议语义
    assert "经费合计120万元" in text
    assert "妥否，请批示。" in text
    assert "整治形式主义" not in text  # 程序性规范不被误当作实体依据
    for field in ("发文字号", "成文日期"):
        assert any(field in p.reason for p in ir.placeholders)  # 不由系统填写
    # 每个数字都能追溯到证据
    nums = [s for _, s in ir.iter_sentences() if any(c.isdigit() for c in s.text)]
    assert all(s.refs for s in nums)
    out = eng.store.out_dir(st.task_id)
    assert (out / "workbench.html").is_file() and (out / "review_package.json").is_file()
    assert any(out.glob("*.docx"))
    pkg_status = eng.load_state(st.task_id).doc_status
    assert pkg_status == DocStatus.DISCUSSION  # 存在待补资金来源与未核实数据
    cp = pending(eng, st.task_id, CheckpointKind.HUMAN_REVIEW)
    assert cp.auto_acceptable is False
    st = eng.resolve_checkpoint(st.task_id, cp.cp_id, "submit", by=user)
    assert st.stage == Stage.SUBMITTED


def test_model_channel_cannot_resolve_checkpoints(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    st = start(eng, user)
    eng.advance(st.task_id, by=user)
    cp = pending(eng, st.task_id, CheckpointKind.TASK_CONFIRM)
    with pytest.raises(PermissionError):
        eng.resolve_checkpoint(st.task_id, cp.cp_id, "accept", by=channel("agent"))


def test_approval_binding_and_invalidation_on_change(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    st = start(eng, user)
    st = run_to_review(eng, user, st.task_id)
    cp = pending(eng, st.task_id, CheckpointKind.HUMAN_REVIEW)
    eng.resolve_checkpoint(st.task_id, cp.cp_id, "submit", by=user)
    with pytest.raises(PermissionError):
        eng.import_approval(st.task_id, by=user, approver="张某", approved_at="2026年10月8日", scope="同意", source="OA-1")
    approver = human("leader", "approver")
    st = eng.import_approval(st.task_id, by=approver, approver="张某", approved_at="2026年10月8日", scope="同意按请示上报", source="OA-2026-001")
    assert st.stage == Stage.APPROVED
    assert eng.current_ir(st).status == DocStatus.APPROVED_FOR_ISSUE
    # 审批后发生实质性修改：审批失效，回到审校
    ledger = eng.load_matter_ledger(st)
    equip = next(f for f in ledger.facts if f.attribute.startswith("设备购置"))
    st = eng.request_revision(st.task_id, by=user, fact_changes=[{"fact_id": equip.fact_id, "new_value": 40, "reason": "核减"}])
    assert st.stage == Stage.REVIEW
    assert not st.approvals
    assert any("复审" in e for e in st.errors)


def test_fact_change_propagates_to_totals_and_attachment(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    st = start(eng, user)
    st = run_to_review(eng, user, st.task_id)
    ledger = eng.load_matter_ledger(st)
    equip = next(f for f in ledger.facts if f.attribute.startswith("设备购置"))
    st = eng.request_revision(st.task_id, by=user, fact_changes=[{"fact_id": equip.fact_id, "new_value": 40, "reason": "按审核意见核减"}])
    ir = eng.current_ir(st)
    text = ir.full_text()
    assert "合计100万元" in text and "120万元" not in text
    table = ir.attachments[0].blocks[0].table
    assert ["设备购置", "40"] in table and any(r[0] == "合计" and r[1] == "100" for r in table)
    ledger = eng.load_matter_ledger(st)
    total = next(f for f in ledger.facts if f.status == FactStatus.COMPUTED and f.kind == "money")
    assert total.value == 100
    st = eng.advance(st.task_id, by=user)
    assert st.stage == Stage.HUMAN_REVIEW


def test_human_edit_status_upgrade_is_flagged(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    st = start(eng, user)
    st = run_to_review(eng, user, st.task_id)
    ir = eng.current_ir(st)
    s = next(s for _, s in ir.iter_sentences() if "拟新建" in s.text)
    st = eng.request_revision(st.task_id, by=user, edits=[{"sid": s.sid, "text": "2026年已建成示范点12个。"}])
    st = eng.advance(st.task_id, by=user)
    report = eng.store.load_model(st.task_id, "review_report", ReviewReport)
    up = [i for i in report.issues if i.type == IssueType.STATUS_UPGRADE and i.location.sentence_id == s.sid]
    assert up, "拟议事项被写成已完成，必须被拦下"


def test_admission_paths(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    st = eng.create_task("起草一份开展安全检查的通知", by=user, hints={"recipients": "各科室"})
    r1 = eng.add_material(st.task_id, "机密材料.txt", "机密★1年\n某事项".encode(), by=user, declared=Clearance.PUBLIC)
    assert r1.decision == AdmissionDecision.FORBID
    assert not eng.rt.materials.list(st.matter_id)  # 禁止进入：原文件已删除
    r2 = eng.add_material(st.task_id, "说明.txt", "本次检查范围为全院各科室。".encode(), by=user)
    assert r2.decision == AdmissionDecision.NEED_CONFIRM  # 未申报属性先暂停
    st = eng.advance(st.task_id, by=user)
    cp = pending(eng, st.task_id, CheckpointKind.MATERIAL_CONFIRM)
    eng.resolve_checkpoint(st.task_id, cp.cp_id, "confirm", by=user, data={"materials": {r2.material_id: "公开"}})
    st = eng.advance(st.task_id, by=user)
    assert st.stage == Stage.TASK_CONFIRM
    mats = eng.materials(eng.load_state(st.task_id))
    assert [m.material_id for m in mats] == [r2.material_id]


def test_peer_department_and_internal_unit_authority(tmp_path):
    eng = make_engine(tmp_path, unit_name="")
    user = default_user()
    st = eng.create_task("向主管部门申请专项经费", by=user, hints={"issuer": "示例市卫生健康委员会", "issuer_type": "政府部门", "recipients": "示例市财政局"})
    st = eng.advance(st.task_id, by=user)
    eng.resolve_checkpoint(st.task_id, pending(eng, st.task_id, CheckpointKind.TASK_CONFIRM).cp_id, "accept", by=user)
    st = eng.advance(st.task_id, by=user)
    from gongwen.schemas.genre import GenreDecision

    g = eng.store.load_model(st.task_id, "genre_decision", GenreDecision)
    assert any(f.code == "PEER_DEPARTMENT" for f in g.authority_findings) and "函" in g.alternatives
    # 部门内设机构对外正式行文：超出权限
    st2 = eng.create_task("向示例市财政局申请专项经费", by=user, hints={"issuer": "示例市卫生健康委员会规划发展处", "recipients": "示例市财政局", "direction": "上行文"})
    st2 = eng.advance(st2.task_id, by=user)
    eng.resolve_checkpoint(st2.task_id, pending(eng, st2.task_id, CheckpointKind.TASK_CONFIRM).cp_id, "accept", by=user)
    st2 = eng.advance(st2.task_id, by=user)
    assert st2.stage == Stage.OUT_OF_AUTHORITY
    cp = pending(eng, st2.task_id, CheckpointKind.AUTHORITY)
    assert any("内设机构" in d for d in cp.details)


def test_headless_auto_accept_never_skips_human_review(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    st = start(eng, user)
    st = eng.advance(st.task_id, by=user, auto_accept={"task_confirm", "outline_confirm", "review_escalation"})
    assert st.stage == Stage.HUMAN_REVIEW
    cp = pending(eng, st.task_id, CheckpointKind.HUMAN_REVIEW)
    st = eng.advance(st.task_id, by=user, auto_accept={"task_confirm", "outline_confirm", "review_escalation", "human_review"})
    assert st.stage == Stage.HUMAN_REVIEW and cp.status == "pending"
    log = [r for r in eng.log(st.task_id).replay({"checkpoint.resolved"})]
    assert all("自动接受" in r["payload"]["note"] for r in log)


def test_scripted_model_fabrication_is_rejected(tmp_path):
    """模型把拟议写成已完成、虚构金额：逐句校验拒绝，回退到确定性表达。"""

    def responder(messages, system, tools, schema):
        if schema and "paragraphs" in schema.get("properties", {}):
            import json

            task = json.loads(messages[0].content.split("待改写段落：\n", 1)[1])
            out = []
            for p in task:
                text = p["draft"]
                if "拟新建" in text:
                    text = "2026年已建成示范点12个，投入资金300万元。"
                out.append({"para_id": p["para_id"], "sentences": [{"text": text, "refs": p["refs"]}]})
            return {"paragraphs": out}
        return {"purposes": [], "issuer": None, "recipients": [], "subject": None, "facts": [], "issues": []}

    prov = ScriptedProvider(responder=responder, endpoint="http://localhost/scripted")
    eng = make_engine(tmp_path, providers={"*": prov})
    user = default_user()
    st = start(eng, user)
    st = run_to_review(eng, user, st.task_id)
    ir = eng.current_ir(st)
    text = ir.full_text()
    assert "已建成示范点12个" not in text and "300万元" not in text
    assert "拟新建示范点12个" in text
    events = [r["type"] for r in eng.log(st.task_id).replay()]
    assert "model.request" in events and "egress.decision" in events
    # 审计日志只记录哈希与对象编号，不记录材料原文
    raw = (eng.store.task_dir(st.task_id) / "events.jsonl").read_text(encoding="utf-8")
    assert "设备老化" not in raw


def test_internal_material_never_reaches_public_model(tmp_path):
    prov = ScriptedProvider(responses=[], endpoint="https://api.example-model.com/v1")
    eng = make_engine(tmp_path, providers={"*": prov}, route="unit_approved", accept_internal_materials=True)
    user = default_user()
    st = eng.create_task("起草一份开展安全检查的通知", by=user, hints={"recipients": "各科室"})
    eng.add_material(st.task_id, "内部安排.txt", "内部资料 注意保存\n全院开展安全检查。".encode(), by=user, declared=Clearance.INTERNAL)
    st = eng.advance(st.task_id, by=user, auto_accept={"task_confirm", "outline_confirm", "review_escalation"})
    assert prov.calls == []  # 模型只获准处理公开材料：一次也没有被调用
    denied = [r for r in eng.log(st.task_id).replay({"egress.decision"}) if not r["payload"]["allowed"]]
    assert denied == [] or all("公开" in r["payload"]["reason"] or "白名单" in r["payload"]["reason"] for r in denied)


@pytest.mark.skipif(shutil.which("soffice") is None, reason="需要 LibreOffice")
def test_render_check_with_libreoffice(tmp_path):
    eng = make_engine(tmp_path, render=True)
    user = default_user()
    st = start(eng, user)
    st = run_to_review(eng, user, st.task_id)
    from gongwen.schemas.layout import LayoutReport

    lr = eng.store.load_model(st.task_id, "layout_report", LayoutReport)
    assert lr.render.rendered and lr.render.first_page_has_body
    byid = {c.rule_id: c for c in lr.checks}
    assert byid["LAY-FIRSTPAGE"].status == "pass"
    assert byid["LAY-PAGENO"].status == "pass"
    assert byid["DOCX-MARGIN-TOP"].status == "pass"


def test_matter_siblings_follow_fact_change_and_keep_own_materials(tmp_path):
    """同一事项的请示与函共享事项账本：一处经费变更，另一份文稿联动更新并退回审校（设计 §6.3）。"""
    eng = make_engine(tmp_path)
    user = default_user()
    st1 = start(eng, user)
    st1 = run_to_review(eng, user, st1.task_id)
    st2 = eng.create_task("给示例市财政局发函，商请支持基层医疗示范点建设经费", by=user, matter_id=st1.matter_id, hints={"recipients": "示例市财政局", "issuer_type": "政府部门"})
    eng.add_material(st2.task_id, "经费测算表.xlsx", make_xlsx(BUDGET, sheet="经费测算"), by=user, declared=Clearance.PUBLIC)
    mats = eng.rt.materials.list(st1.matter_id)
    assert len({m.material_id for m in mats}) == 3  # 事项内编号不重复，第二个任务没有覆盖第一个任务的材料
    st2 = eng.advance(st2.task_id, by=user, auto_accept={"task_confirm", "outline_confirm", "review_escalation"})
    assert st2.stage == Stage.HUMAN_REVIEW
    ir2 = eng.current_ir(st2)
    assert "120万元" in ir2.full_text()
    ledger = eng.load_matter_ledger(st1)
    equip = next(f for f in ledger.facts if f.attribute.startswith("设备购置"))
    eng.request_revision(st1.task_id, by=user, fact_changes=[{"fact_id": equip.fact_id, "new_value": 40, "reason": "核减"}])
    st2 = eng.load_state(st2.task_id)
    assert st2.stage == Stage.REVIEW and any("同一事项" in e for e in st2.errors)
    ir2 = eng.current_ir(st2)
    assert "100万元" in ir2.full_text() and "120万元" not in ir2.full_text()
    assert not st2.pending_checkpoints()  # 原人工送审节点作废，须重新审校后再送审
    st2 = eng.advance(st2.task_id, by=user, auto_accept={"review_escalation"})
    assert st2.stage == Stage.HUMAN_REVIEW

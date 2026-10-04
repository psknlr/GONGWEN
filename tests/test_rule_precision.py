"""规则精度回归：数字抽取、小句级状态、会议决策、义务强度、表格合计与常见误报。"""

from datetime import date

from gongwen.knowledge.policy_library import PolicyLibrary
from gongwen.parsing.base import classify_line, table_from_rows
from gongwen.rules import CheckContext, run_checks
from gongwen.rules.base import CheckContext as Ctx
from gongwen.rules.consistency import check_tables
from gongwen.rules.genre_rules import detect_request_matters
from gongwen.rules.semantics import VERIFY_CLAIM, meeting_decision, progress_at, semantic_diff, check_numbers_sourced
from gongwen.rules.textutil import extract_numbers, is_total_row, label_column
from gongwen.schemas.common import IdAllocator
from gongwen.schemas.facts import FactLedger, Progress
from gongwen.schemas.ir import Block, DocumentIR
from gongwen.schemas.task import TaskSpec
from gongwen.importer import ir_from_text


def nums(t):
    return [(m.value, m.unit) for m in extract_numbers(t)]


def test_extract_numbers_common_forms():
    assert nums("3.5亿元资金已全部下达。") == [(3.5, "亿元")]
    assert nums("惠及群众3万人。") == [(30000.0, "人")]
    assert nums("培训2000余人次。") == [(2000.0, "人次")]
    assert nums("覆盖300多个村。") == [(300.0, "个")]
    assert nums("新建10～20个示范点。") == [(10.0, "个"), (20.0, "个")]
    assert nums("受益人口达1.2亿人次。") == [(120000000.0, "人次")]
    assert nums("1.已培训医务人员300人。") == [(300.0, "人")]  # 序号不是数字


def test_number_source_is_quantity_not_substring():
    ir = ir_from_text("示例市卫生健康委员会关于申请经费的请示\n示例市人民政府：\n拟申请安排经费200万元，用于新建示范点2个。\n妥否，请批示。\n")
    for _, s in ir.iter_sentences():
        s.origin = "system"
    task = TaskSpec(task_id="T", request_text="申请经费1200万元，新建示范点12个", policy_as_of=date(2026, 1, 1))
    issues = check_numbers_sourced(Ctx(ir=ir, task=task, ledger=FactLedger(), ids=IdAllocator()))
    flagged = {i.original for i in issues}
    assert any("200万元" in t for t in flagged)


def test_progress_is_judged_per_clause():
    t = "截至2025年底已建成示范点8个，2026年拟新建示范点12个。"
    assert progress_at(t, t.index("8个")) == Progress.COMPLETED
    assert progress_at(t, t.index("12个")) == Progress.PLANNED
    t = "2026年新建的12个示范点全部建成投用，将进一步提升基层服务能力。"
    assert progress_at(t, t.index("12个")) == Progress.COMPLETED  # “将进一步”不掩盖“全部建成”
    t = "我委已完成全部选址工作，目前正在建设示范点12个。"
    assert progress_at(t, t.index("12个")) == Progress.ONGOING
    t = "示范点12个，拟于2026年底前建成。"
    assert progress_at(t, t.index("12个")) == Progress.PLANNED


def test_meeting_decision_checks_negation_and_suggestions_first():
    assert meeting_decision("会议决定，2026年新建示范点12个。") == "decided"
    assert meeting_decision("规划发展处张某建议进一步明确各区职责分工。") == "discussion"
    assert meeting_decision("财务处认为经费来源尚未确定。") == "discussion"
    assert meeting_decision("张某建议增加2个示范点，会议未作决定。") == "discussion"
    assert meeting_decision("经讨论，会议同意该方案。") == "decided"


def test_obligation_and_scope_changes_are_not_masked():
    dims = lambda a, b: {(c.dimension, c.direction) for c in semantic_diff(a, b)}  # noqa: E731
    assert ("义务强度", "增强") in dims("各单位可以组织应急演练。", "各单位应当组织应急演练。")
    assert ("义务强度", "增强") in dims("必须于6月30日前完成自查，确有困难的可以适当延期。", "必须于6月30日前完成自查，确有困难的也必须按期完成。")
    assert ("实施范围", "扩大") in dims("在全市选取3个区开展试点。", "在全市全面推开。")
    assert not dims("各单位应当组织应急演练。", "各单位应当组织好应急演练。")


def test_verify_claim_variants():
    for t in ("经核实，已建成8个。", "经我委核实，已建成8个。", "经过核实，已建成8个。", "经认真核查，已建成8个。", "经查实，已建成8个。", "经逐一核对，已建成8个。"):
        assert VERIFY_CLAIM.search(t), t


def test_total_row_and_label_column_detection():
    assert is_total_row(["", "合  计", "120"])
    assert is_total_row(["合计（万元）", "120"])
    assert not is_total_row(["设备购置", "60"])
    assert label_column(["序号", "项目", "金额（万元）"], [["1", "设备购置", "60"]]) == 1
    t = table_from_rows("M", "M.t1", [["经费测算表", ""], ["项目", "金额（万元）"], ["设备购置", "60"]], "x")
    assert t.header == ["项目", "金额（万元）"] and t.title == "经费测算表"


def test_table_total_check_skips_rate_columns():
    ir = DocumentIR(doc_id="D", matter_id="M", genre="报告")
    ir.blocks.append(Block(bid="B1", kind="table", table=[["项目", "金额（万元）", "完成率（%）"], ["甲", "60", "80"], ["乙", "40", "90"], ["合计", "100", "85"]]))
    assert not check_tables(CheckContext(ir=ir))


def test_request_matters_ignore_uses_of_funds():
    found = detect_request_matters(["现申请安排经费120万元，用于设备购置、场地改造和人员培训。"])
    assert list(found) == ["经费"]
    found = detect_request_matters(["申请增加编制5名和经费100万元。"])
    assert {"经费", "编制人员"} <= set(found)


def test_common_false_positives_on_clean_request():
    text = (
        "示例市卫生健康委员会关于申请基层医疗示范点建设经费的请示\n示例市人民政府：\n"
        "2026年拟新建示范点12个，计划于2026年6月30日前完成选址，确需延期的，须经市卫生健康委员会批准。"
        "经测算，所需经费合计120万元，现申请安排经费120万元，用于设备购置、场地改造和人员培训。\n妥否，请批示。\n"
    )
    ir = ir_from_text(text)
    ids = {i.rule.rule_id for i in run_checks(CheckContext(ir=ir)) if i.rule}
    assert not ids & {"GW-GENRE-002", "GW-SEM-006", "GW-STYLE-003"}


def test_conditional_rules_are_capped_even_with_explicit_severity():
    from gongwen.schemas.common import Severity
    from gongwen.schemas.review import IssueType

    ctx = CheckContext(ir=DocumentIR(doc_id="D", matter_id="M"))
    issue = ctx.issue("GW-ROUTE-005", IssueType.AUTHORITY, "越级", severity=Severity.MAJOR)
    assert issue.severity == Severity.MINOR


def test_numbered_statement_lines_are_paragraphs():
    assert classify_line("一、总体要求") == "heading"
    assert classify_line("1.已培训基层医务人员300人。") == "paragraph"


def test_policy_region_must_contain_task_region():
    from gongwen.knowledge.policy_library import PolicyDocument

    lib = PolicyLibrary(include_seed=False, allow_synthetic=True)
    doc = PolicyDocument.model_validate({
        "policy_id": "LOCAL-1", "title": "杭州市基层医疗示范点建设管理办法", "issuers": ["杭州市人民政府"],
        "regions": ["浙江省杭州市"], "subjects": ["通用"], "status": "现行有效",
        "publish_date": "2025-01-01", "effective_date": "2025-01-01", "synthetic": True,
    })
    lib.add(doc)
    assert lib.applicability(doc, date(2026, 6, 1), region="浙江省杭州市余杭区").region_ok is True
    # 文件地域比任务地域小（市级文件用于全省事项）：不适用
    assert lib.applicability(doc, date(2026, 6, 1), region="浙江省").region_ok is False

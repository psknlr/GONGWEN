from datetime import date

from gongwen.knowledge.policy_library import PolicyLibrary
from gongwen.rules import CheckContext, run_checks
from gongwen.rules.semantics import progress_of, semantic_diff
from gongwen.rules.textutil import cn_to_number, extract_numbers, split_sentences
from gongwen.schemas import (
    Block,
    DocumentIR,
    EvidenceRef,
    Fact,
    FactLedger,
    FactStatus,
    Locator,
    Progress,
    Sentence,
    TaskSpec,
)
from gongwen.schemas.review import IssueType


def mk_ir(genre, paragraphs, direction="上行文", title=None, recipients=None, refs=None):
    blocks = []
    for i, p in enumerate(paragraphs):
        if isinstance(p, tuple):  # (level, label, heading)
            lvl, label, heading = p
            blocks.append(Block(bid=f"b{i}", kind="heading", level=lvl, label=label, heading=heading))
        else:
            r = (refs or {}).get(i, [])
            blocks.append(Block(bid=f"b{i}", kind="paragraph", sentences=[Sentence(sid=f"s{i}", text=p, refs=r)]))
    return DocumentIR(
        doc_id="D1",
        matter_id="M1",
        genre=genre,
        direction=direction,
        title=title or f"示例市卫生健康委员会关于某事项的{genre}",
        recipients=recipients or ["示例市财政局"],
        blocks=blocks,
    )


def types(issues):
    return {i.type for i in issues}


def test_text_utils():
    assert cn_to_number("二十") == 20
    assert cn_to_number("一百二十") == 120
    assert cn_to_number("三千五百万") == 35_000_000
    nums = extract_numbers("拟建设20个示范点，经费100万元，占比15%，依据国办发〔2018〕115号，见附件1，2026年8月1日")
    raws = [n.raw.strip() for n in nums]
    assert "20个" in raws and "100万元" in raws and "15%" in raws
    assert not any("115" in r or "2018" in r or "2026" in r for r in raws)
    assert split_sentences("一是加强管理。二是“完善制度。”三是落实责任；") == ["一是加强管理。", "二是“完善制度。”三是落实责任；"]


def test_progress_and_semantic_diff():
    assert progress_of("拟建设20个示范点") == Progress.PLANNED
    assert progress_of("已建成20个示范点") == Progress.COMPLETED
    # 目标与要求不是完成陈述：按拟议处理，事后改成“已完成”会被识别为状态升级
    assert progress_of("确保按时完成建设任务") == Progress.PLANNED
    assert progress_of("主要应用场景已建成") == Progress.COMPLETED  # “主要”“应用”不是要求性语境
    assert progress_of("做好来访接待工作") == Progress.NONE  # “接待”不是拟议标记
    changes = semantic_diff("原则上可以开展试点，确有需要的经批准后实施", "必须全面实施")
    dims = {c.dimension for c in changes}
    assert {"义务强度", "实施范围", "条件与例外"} <= dims
    assert any(c.dimension == "决策状态" for c in semantic_diff("会议讨论了该方案", "会议决定实施该方案"))


def test_report_with_mixed_request_and_closing():
    ir = mk_ir("报告", ["现将有关情况报告如下。", "拟申请经费100万元，妥否，请批示。"])
    issues = run_checks(CheckContext(ir=ir), ["genre"])
    assert IssueType.MIXED_REQUEST in types(issues)
    mixed = next(i for i in issues if i.type == IssueType.MIXED_REQUEST)
    assert mixed.severity.value == "阻断送审"
    assert "第十五条" in mixed.rule.source


def test_qingshi_single_matter_and_closing():
    ir = mk_ir("请示", ["为推进工作，现就有关事项请示如下。", "申请安排专项经费80万元。", "同时申请增加编制3名。"])
    issues = run_checks(CheckContext(ir=ir), ["genre"])
    t = types(issues)
    assert IssueType.MULTI_MATTER in t
    assert IssueType.CLOSING_MISMATCH in t  # 缺少请求性结束语


def test_address_and_function_letter_tone():
    ir = mk_ir("函", ["你局要务必于月底前完成，请遵照执行。"], direction="平行文")
    t = types(run_checks(CheckContext(ir=ir), ["genre"]))
    assert IssueType.ADDRESS_MISMATCH in t


def test_status_upgrade_against_ledger():
    f = Fact(
        fact_id="F-001",
        statement="拟建设20个示范点",
        attribute="示范点数量",
        value=20,
        unit="个",
        kind="count",
        status=FactStatus.PROPOSED,
        progress=Progress.PLANNED,
        sources=[Locator(material_id="M-001", kind="paragraph", path="p3", excerpt="拟建设20个示范点")],
    )
    ledger = FactLedger(facts=[f])
    ir = mk_ir("报告", ["今年已建成20个示范点。"], refs={0: [EvidenceRef(kind="fact", id="F-001")]})
    issues = run_checks(CheckContext(ir=ir, ledger=ledger), ["semantics", "language"])
    up = [i for i in issues if i.type == IssueType.STATUS_UPGRADE]
    assert up and up[0].severity.value == "阻断送审"
    assert up[0].evidence_text == "拟建设20个示范点"
    assert IssueType.RELATIVE_TIME in types(issues)


def test_unsourced_money_is_blocking():
    ledger = FactLedger(facts=[Fact(fact_id="F-001", statement="经费100万元", value=100, unit="万元", kind="money", status=FactStatus.RECORDED)])
    task = TaskSpec(task_id="T1", request_text="写一份请示")
    ir = mk_ir("请示", ["经测算需经费120万元。", "其中设备购置100万元。"])
    issues = run_checks(CheckContext(ir=ir, ledger=ledger, task=task), ["semantics"])
    uns = [i for i in issues if i.type == IssueType.UNSOURCED_NUMBER]
    assert len(uns) == 1 and "120万元" in uns[0].suggestion and uns[0].severity.value == "阻断送审"
    # 有来源的数字被自动关联证据，便于审阅侧栏追溯
    assert any(r.id == "F-001" for r in ir.blocks[1].sentences[0].refs)


def test_approval_claim_without_record():
    ir = mk_ir("通知", ["经市政府同意，现将方案印发给你们。"], direction="下行文")
    assert IssueType.STATUS_UPGRADE in types(run_checks(CheckContext(ir=ir), ["semantics"]))


def test_language_punctuation_numbers_structure():
    ir = mk_ir(
        "通知",
        [
            (1, "一、", "总体要求"),
            "各单位要按照要求,于26年3月前完成，比例为15～30%。",
            (2, "（二）", "工作安排"),
            "规划期为2026-2030年，其它事项另行通知。",
        ],
        direction="下行文",
    )
    issues = run_checks(CheckContext(ir=ir), ["language"])
    t = types(issues)
    assert {IssueType.PUNCTUATION, IssueType.NUMBER_USAGE, IssueType.NUMBERING, IssueType.WORDING} <= t
    numbering = [i for i in issues if i.type == IssueType.NUMBERING]
    assert numbering[0].fix_hint["set_label"] == "（一）"


def test_citation_temporal_applicability():
    lib = PolicyLibrary()
    task = TaskSpec(task_id="T1", request_text="x", policy_as_of=date(2011, 1, 1))
    ir = mk_ir("通知", ["根据《党政机关公文处理工作条例》（中办发〔2012〕14号），现就有关事项通知如下。"], direction="下行文")
    issues = run_checks(CheckContext(ir=ir, task=task, policy_library=lib), ["basis"])
    na = [i for i in issues if i.type == IssueType.CITATION_NOT_APPLICABLE]
    assert na and "早于施行日期" in na[0].suggestion
    ir2 = mk_ir("通知", ["根据《某某不存在的管理办法》，现通知如下。"], direction="下行文")
    assert IssueType.CITATION_MISSING in types(run_checks(CheckContext(ir=ir2, task=task, policy_library=lib), ["basis"]))


def test_burden_and_empty_phrases():
    task = TaskSpec(task_id="T1", request_text="写一份开展安全检查的通知")
    ir = mk_ir("通知", ["各单位要高度重视，切实加强。", "请各单位每周报送工作台账和照片。"], direction="下行文")
    issues = run_checks(CheckContext(ir=ir, task=task), ["burden"])
    t = types(issues)
    assert IssueType.BURDEN in t and IssueType.EMPTY_PHRASE in t


def test_attachment_consistency_and_table_totals():
    from gongwen.schemas import Attachment, AttachmentNote

    ir = mk_ir("请示", ["经费测算明细见附件2。"])
    ir.attachment_notes = [AttachmentNote(seq=1, name="经费测算表。")]
    ir.attachments = [
        Attachment(seq=1, title="经费明细表", blocks=[Block(bid="t1", kind="table", table=[["项目", "金额"], ["设备", "60"], ["改造", "40"], ["合计", "90"]])])
    ]
    issues = run_checks(CheckContext(ir=ir), ["consistency"])
    t = types(issues)
    assert {IssueType.ATTACHMENT_MISMATCH, IssueType.CROSS_REF, IssueType.CALC_ERROR} <= t
    assert IssueType.FORMAT in t  # 附件名称后有句号

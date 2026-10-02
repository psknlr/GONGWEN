from datetime import date

from gongwen.knowledge.policy_library import PolicyLibrary
from gongwen.knowledge.retrieval import find_doc_numbers, normalize_doc_number, tokenize
from gongwen.knowledge.stores import CaseLibrary, mask_specifics
from gongwen.schemas.policy import PolicyDocument


def test_tokenize_and_docnumbers():
    assert "请示" in tokenize("请示应当一文一事")
    assert normalize_doc_number("中办发[2012]14号") == "中办发〔2012〕14号"
    assert find_doc_numbers("依据国办发〔2018〕115号和国务院令第783号") == ["国办发〔2018〕115号", "国务院令第783号"]


def test_exact_and_bm25_retrieval():
    lib = PolicyLibrary()
    hits = lib.exact("根据《党政机关公文处理工作条例》（中办发〔2012〕14号）")
    assert hits and hits[0].policy.policy_id == "gongwen-tiaoli-2012"
    top = lib.search("请示应当一文一事 不得在报告中夹带请示事项", k=3)
    pairs = [(h.policy.policy_id, h.article.article_no) for h in top]
    assert ("gongwen-tiaoli-2012", "第十五条") in pairs
    assert all("一文一事" in h.article.text for h in top[:2])
    assert all(h.article.verbatim for h in top[:2])


def test_applicability_time_region_subject():
    lib = PolicyLibrary()
    p = lib.get("gongwen-tiaoli-2012")
    early = lib.applicability(p, date(2011, 1, 1))
    assert early.applicable is False and early.temporal_ok is False
    ok = lib.applicability(p, date(2026, 10, 1), subject_type="党政机关")
    assert ok.applicable is True
    hospital = lib.applicability(p, date(2026, 10, 1), subject_type="医院")
    assert hospital.applicable is True
    assert any("参照执行" in r for r in hospital.reasons)
    local = PolicyDocument(
        policy_id="demo-local",
        title="示例省某专项资金管理办法",
        issuers=["示例省财政厅"],
        regions=["示例省"],
        subjects=["通用"],
        effective_date=date(2024, 1, 1),
        synthetic=True,
    )
    lib.docs[local.policy_id] = local
    a = lib.applicability(local, date(2026, 1, 1), region="另一省")
    assert a.applicable is False  # 合成数据且地域不符
    lib.allow_synthetic = True
    b = lib.applicability(local, date(2026, 1, 1), region="示例省")
    assert b.applicable is True and any("示例数据" in r for r in b.reasons)


def test_constraints_retrieval_returns_limiting_articles():
    lib = PolicyLibrary()
    hits = lib.constraints("向上级机关请示 报告 行文")
    assert hits
    assert all(any(m in h.article.text for m in ("不得", "除", "原则上", "应当经", "禁止")) for h in hits)


def test_case_library_masks_specifics():
    masked = mask_specifics("2025年3月，某市卫生健康委员会投入资金120万元，建成示范点20个。")
    assert "120" not in masked and "2025" not in masked and "卫生健康委员会" not in masked
    refs = CaseLibrary().style_refs("请示", "申请经费")
    assert refs and refs[0]["usage"].startswith("仅供结构与表达参考")

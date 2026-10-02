"""评测框架：用例结构有效、运行与汇总正确、消融能定位模块贡献。"""

from gongwen.eval import FLAGS, GROUPS, load_cases, run_case, summarize, variants

KINDS = {"pipeline", "check", "admission", "revision", "model"}


def test_cases_are_well_formed():
    cases = load_cases()
    assert len(cases) >= 50
    assert {c["group"] for c in cases} == set(GROUPS)
    for c in cases:
        assert c["kind"] in KINDS and c.get("title") and c.get("expect"), c["id"]
        if c["kind"] == "check":
            assert c.get("text")
        else:
            assert c.get("request")
    assert sum(1 for c in cases if c.get("control")) >= 3  # 干净对照用于度量误报


def test_run_and_ablation_attribution():
    cases = {c["id"]: c for c in load_cases()}
    picked = [cases[i] for i in ("FB-12", "SR-11", "FL-01", "GR-13")]
    var = variants(["consistency_check", "burden_check"])
    results = [run_case(c, v, flags) for v, flags in var.items() for c in picked]
    full = {r.case_id: r for r in results if r.variant == "full"}
    assert all(r.passed for r in full.values()), [(r.case_id, r.error, [c for c in r.checks if not c.ok]) for r in full.values()]
    summary = summarize(results)
    assert summary["full"]["planted_issue_recall"] == 1.0
    assert summary["full"]["control_false_alarms_major_plus"] == 0
    assert summary["no_consistency_check"]["lost_vs_full"] == ["FB-12"]  # 表格合计由一致性检查发现
    assert summary["no_burden_check"]["lost_vs_full"] == ["SR-11"]  # 新增报送要求由减负检查发现
    assert set(FLAGS) >= {"fact_ledger", "temporal_check"}

"""固定句标定与按提示词改写：规则标定、校验器、人工优先、采纳与过期、工作台接口与页面。"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from gongwen.eval.runner import _engine, _human_loop, _materials
from gongwen.harness.permissions import channel
from gongwen.orchestrator import default_user
from gongwen.schemas.ir import Block, DocumentIR, Sentence
from gongwen.schemas.common import EvidenceRef
from gongwen.schemas.state import Stage
from gongwen.skills.rewrite import auto_locks, validate_edit

CASE = {
    "id": "RW-T",
    "request": "起草一份部署2026年基层医疗示范点建设工作的通知",
    "hints": {"recipients": "各区卫生健康局", "issuer_type": "政府部门"},
    "materials": [
        {
            "name": "工作安排.md",
            "text": "各区卫生健康局要在2026年6月底前完成选址。\n\n各区卫生健康局应当于2026年12月底前完成示范点建设并组织验收。\n\n市卫生健康委负责统筹协调和技术指导。\n",
        }
    ],
    "accept": ["task_confirm", "outline_confirm"],
}


def _task(tmp_path, model="rewrite_mix"):
    case = {**CASE, **({"model": model} if model else {})}
    eng = _engine(case, tmp_path, {})
    user = default_user("tester")
    st = eng.create_task(case["request"], by=user, hints=case["hints"])
    _materials(eng, st.task_id, case, user)
    st = _human_loop(eng, st.task_id, case, user)
    assert st.stage == Stage.HUMAN_REVIEW
    return eng, user, st.task_id


def _sid(eng, task_id, needle):
    return next(lk["sid"] for lk in eng.sentence_locks(task_id) if needle in lk["text"])


# ------------------------------------------------------------------ 规则标定与校验器
def test_auto_locks_cover_facts_citations_closings_and_placeholders():
    def s(sid, text, **kw):
        return Sentence(sid=sid, text=text, **kw)

    ir = DocumentIR(
        doc_id="D",
        matter_id="M",
        title="t",
        blocks=[
            Block(
                bid="b1",
                kind="paragraph",
                sentences=[
                    s("s1", "为提升服务能力，现就有关事项通知如下。"),
                    s("s2", "截至2025年底已建成示范点8个。"),
                    s("s3", "根据《示例省基层医疗卫生条例》有关规定，结合实际。"),
                    s("s4", "资金来源：【待补】。"),
                    s("s5", "经市人民政府同意，现予印发。"),
                    s("s6", "特此通知。", function="结语"),
                    s("s7", "贵局来函收悉。", refs=[EvidenceRef(kind="material", id="MAT-1", note="来文")]),
                ],
            )
        ],
    )
    locks = auto_locks(ir)
    assert "s1" not in locks
    assert "事实数据" in locks["s2"] and "文件标题" in locks["s3"] and "待补" in locks["s4"]
    assert "审批" in locks["s5"] and "结束语" in locks["s6"] and "来文" in locks["s7"]


@pytest.mark.parametrize(
    "before,after,status,why",
    [
        ("各区要抓好落实。", "各区要认真抓好落实。", "proposed", ""),
        ("各区要抓好落实。", "经市政府同意，各区要抓好落实。", "rejected", "审批"),
        ("各区要抓好落实。", "各区要投入300万元抓好落实。", "rejected", "无来源数字"),
        ("各区要抓好落实。", "各区要按照《示例办法》抓好落实。", "rejected", "文件标题"),
        ("资金来源：【待补】。", "资金来源：市级财政。", "rejected", "待补"),
        ("各区要抓好落实。", "各区可以抓好落实。", "needs_human", "义务强度"),
        ("各区要于6月底前完成。", "各区要于9月底前完成。", "rejected", "时限"),
        ("各区要于6月底前完成选址。", "各区要抓紧完成选址。", "needs_human", "删去了时限"),
        ("各区要在5个工作日内报送。", "各区要在10个工作日内报送。", "rejected", "时限"),
        ("各区要抓好落实。", "", "needs_human", "删除整句"),
        ("各区要抓好落实。", "经核实，各区要抓好落实。", "rejected", "核实"),
    ],
)
def test_validate_edit(before, after, status, why):
    st, reasons, _ = validate_edit(before, after, None, set())
    assert st == status
    assert why in "；".join(reasons)


# ------------------------------------------------------------------ 引擎流程
def test_rewrite_flow_apply_safe_only_and_rerun_review(tmp_path):
    eng, user, tid = _task(tmp_path)
    v0 = eng.load_state(tid).current_version
    r = eng.rewrite(tid, by=user, prompt="语言更简洁有力")
    assert r["counts"] == {"proposed": 1, "needs_human": 1, "rejected": 3}
    assert eng.current_ir(eng.load_state(tid)).version == v0  # 只形成建议，不改稿
    rejected = next(p["patch_id"] for p in r["patches"] if p["status"] == "rejected")
    with pytest.raises(ValueError):
        eng.apply_rewrite(tid, r["rewrite_id"], by=user, patch_ids=[rejected])
    res = eng.apply_rewrite(tid, r["rewrite_id"], by=user)
    assert len(res["applied"]) == 1
    st = eng.advance(tid, by=user, auto_accept={"review_escalation"})
    ir = eng.current_ir(st)
    assert ir.version == v0 + 1 and st.stage == Stage.HUMAN_REVIEW
    text = ir.full_text()
    assert "并做好技术指导工作" in text and "经市政府同意" not in text and "500万元" not in text
    s = next(s for _, s in ir.iter_sentences() if "技术指导工作" in s.text)
    assert s.origin == "patch"  # 采纳的模型改写可追溯，不记为人工原文
    with pytest.raises(KeyError):
        eng.apply_rewrite(tid, r["rewrite_id"], by=user)  # 已采纳的不能重复采纳


def test_human_lock_wins_and_model_calibration_only_adds(tmp_path):
    eng, user, tid = _task(tmp_path)
    duty = _sid(eng, tid, "负责统筹协调")
    closing = _sid(eng, tid, "特此通知")
    eng.set_sentence_locks(tid, by=user, unlock=[closing])
    r = eng.calibrate_locks(tid, by=user, prompt="突出责任落实")
    assert duty in r["added"]
    locks = {lk["sid"]: lk for lk in eng.sentence_locks(tid)}
    assert locks[duty]["locked"] and locks[duty]["source"] == "model"
    assert not locks[closing]["locked"] and locks[closing]["source"] == "human"  # 模型标定不推翻人工解锁
    eng.set_sentence_locks(tid, by=user, unlock=[duty])
    assert not {lk["sid"]: lk for lk in eng.sentence_locks(tid)}[duty]["locked"]
    with pytest.raises(KeyError):
        eng.set_sentence_locks(tid, by=user, lock=["s-999"])
    with pytest.raises(ValueError):
        eng.set_sentence_locks(tid, by=user, lock=[duty], unlock=[duty])


def test_model_channel_cannot_lock_or_rewrite(tmp_path):
    eng, user, tid = _task(tmp_path)
    bot = channel("agent")
    with pytest.raises(PermissionError):
        eng.rewrite(tid, by=bot, prompt="改简洁")
    with pytest.raises(PermissionError):
        eng.set_sentence_locks(tid, by=bot, lock=[_sid(eng, tid, "负责")])
    with pytest.raises(ValueError):
        eng.rewrite(tid, by=user, prompt="   ")


def test_stale_suggestion_is_skipped_after_manual_edit(tmp_path):
    eng, user, tid = _task(tmp_path)
    r = eng.rewrite(tid, by=user, prompt="语言更简洁有力")
    target = next(p for p in r["patches"] if p["status"] == "proposed")["target"]
    eng.request_revision(tid, by=user, edits=[{"sid": target, "text": "市卫生健康委负责统筹协调和全程技术指导。"}])
    eng.advance(tid, by=user, auto_accept={"review_escalation"})
    with pytest.raises(ValueError, match="过时"):
        eng.apply_rewrite(tid, r["rewrite_id"], by=user)


def test_offline_rewrite_says_model_needed(tmp_path):
    eng, user, tid = _task(tmp_path, model=None)
    r = eng.rewrite(tid, by=user, prompt="语言更简洁")
    assert r["status"] == "empty" and not r["patches"]
    assert any("未配置可用模型" in n for n in r["notes"])
    r2 = eng.calibrate_locks(tid, by=user, prompt="语言更简洁")
    assert r2["added"] == {} and "未配置可用模型" in r2["notes"][0]


# ------------------------------------------------------------------ 工作台
@pytest.fixture()
def live(tmp_path):
    from gongwen.workbench.server import serve

    eng, user, tid = _task(tmp_path)
    httpd, token = serve(eng, user, port=0)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield eng, tid, f"http://127.0.0.1:{httpd.server_address[1]}", token
    httpd.shutdown()
    httpd.server_close()


def _post(url, body, token=None):
    headers = {"Content-Type": "application/json", **({"X-GW-Token": token} if token else {})}
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_workbench_rewrite_routes(live):
    eng, tid, base, token = live
    assert _post(base + "/api/rewrite", {"task_id": tid, "prompt": "简洁"})[0] == 403
    code, j = _post(base + "/api/locks", {"task_id": tid, "lock": "s-001"}, token)
    assert code == 400  # 须为列表
    code, j = _post(base + "/api/rewrite", {"task_id": tid, "prompt": "语言更简洁有力"}, token)
    assert code == 200 and "可采纳 1 条" in j["message"]
    rid = j["rewrite_id"]
    page = urllib.request.urlopen(f"{base}/task/{tid}", timeout=60).read().decode("utf-8")
    assert 'id="p-rewrite"' in page and rid in page
    pid = next(p["patch_id"] for p in eng.rewrite_result(tid, rid)["patches"] if p["status"] == "proposed")
    code, j = _post(base + "/api/rewrite/apply", {"task_id": tid, "rewrite_id": rid, "patch_ids": [pid]}, token)
    assert code == 200 and "已采纳 1 条" in j["message"]
    assert "技术指导工作" in eng.current_ir(eng.load_state(tid)).full_text()


def test_workbench_rewrite_tab_renders_and_locks_in_browser(live):
    playwright = pytest.importorskip("playwright.sync_api")
    eng, tid, base, token = live
    errors: list[str] = []
    with playwright.sync_playwright() as p:
        browser = None
        # 预装的 Chromium 版本可能与 playwright 包期望的不同：依次尝试默认位置与预装可执行文件
        for kw in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
            try:
                browser = p.chromium.launch(**kw)
                break
            except Exception as exc:  # 浏览器不可用时跳过，而不是误报失败
                reason = str(exc).splitlines()[0]
        if browser is None:
            pytest.skip(f"无法启动浏览器：{reason}")
        page = browser.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto(f"{base}/task/{tid}")
        page.click("nav.tabs button[data-t=rewrite]")
        rows = page.locator("#p-rewrite .lockrow")
        assert rows.count() == len(eng.sentence_locks(tid))
        duty = _sid(eng, tid, "负责统筹协调")
        with page.expect_navigation():
            page.click(f"button[data-lock='{duty}']")
        assert {lk["sid"]: lk for lk in eng.sentence_locks(tid)}[duty]["source"] == "human"
        assert page.locator(f".gw-doc span.s.locked[data-sid='{duty}']").count() == 1
        browser.close()
    assert not errors, errors

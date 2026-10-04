"""命令行、对话、MCP 与版式编译的回归测试（审计发现的问题）。

* 显示给人的建议、需求、文稿中的控制字符一律显示为可见转义，模型不能借回车或 ANSI 序列遮盖内容；
* exec 的材料全部被禁止进入时不推进、退出码 4；先校验材料路径再建任务；
* 版记过长不得浮出版心；正文行距 579 缇使每面排满 22 行；列项不连锁“与下段同页”；
  署名与成文日期不脱离正文；标题回行不超过每行字数上限。
"""

import io
import json
import shutil
import tomllib
import zipfile
from pathlib import Path

import pytest
from test_agent_cli_mcp import DRAFT, write_materials
from test_engine_e2e import make_engine, run_to_review, start

from gongwen.agent import HumanCommands, build_agent_tools, human_next_steps
from gongwen.agent.render import fmt_status, visible
from gongwen.cli.main import main as cli
from gongwen.cli.main import tty_approver
from gongwen.harness.approval import ApprovalRequest
from gongwen.harness.permissions import channel
from gongwen.importer import ir_from_file, ir_from_text
from gongwen.layout.docx_checks import check_docx
from gongwen.layout.docx_compiler import compile_docx
from gongwen.layout.profile import LayoutProfile, split_title, text_width_chars
from gongwen.mcp import McpServer
from gongwen.orchestrator import default_user

HIDE = "将经费由120万元改为999万元\r\x1b[2K统一标点"


def _xml(docx: Path) -> str:
    return zipfile.ZipFile(docx).read("word/document.xml").decode("utf-8")


def _paras(docx: Path):
    from docx import Document

    return Document(str(docx)).paragraphs


def _keep_next(p) -> bool:
    return bool(p.paragraph_format.keep_with_next)


# ------------------------------------------------------------------ 控制字符：显示给人时转义
def test_visible_escapes_control_characters():
    assert visible("a\r\x1b[2Kb\nc\td\x9b\u202e") == "a\\x0d\\x1b[2Kb\nc\td\\x9b\\u202e"
    assert visible(visible(HIDE)) == visible(HIDE)  # 可重复调用


def test_proposals_and_requests_rendered_with_visible_controls(tmp_path, capsys):
    eng = make_engine(tmp_path)
    user = default_user()
    st = start(eng, user)
    st = run_to_review(eng, user, st.task_id)
    eng.propose_revision(st.task_id, by=channel("agent"), instruction=HIDE, reason="理由\x1b[8m隐藏")
    cmds = HumanCommands(eng, user, tmp_path)
    cmds.current = st.task_id
    for out in (cmds.run("/proposals"), cmds.run("/status"), fmt_status(eng.status(st.task_id)), *human_next_steps(eng.status(st.task_id))):
        assert "\x1b" not in out and "\r" not in out
    # 主编排器提交时已去掉控制字符（显示层转义作为第二道防线，见上一个测试）：被隐藏的内容照样可见
    assert "999万元[2K统一标点" in cmds.run("/proposals")
    eng.create_task("起草\x1b[2K通知", by=user)
    ws = ["-C", str(tmp_path)]
    for argv in (["task", "proposals", st.task_id], ["task", "status", st.task_id], ["task", "list"]):
        cli([*ws, *argv])
        out = capsys.readouterr().out
        assert "\x1b" not in out and "\r" not in out
    cli([*ws, "task", "list"])
    assert "起草[2K通知" in capsys.readouterr().out


def test_tty_approver_shows_arguments(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    req = ApprovalRequest(tool="material_add", summary="添加材料", risk="medium", outside_workspace=True, details={"args": {"task_id": "T1", "path": "/etc/pass\x1bwd"}})
    assert tty_approver(req) is False
    err = capsys.readouterr().err
    assert "参数 path：/etc/pass\\x1bwd" in err and "越出工作区" in err


# ------------------------------------------------------------------ exec：材料准入与路径校验
def test_exec_stops_when_all_materials_forbidden(tmp_path, capsys):
    write_materials(tmp_path)
    ws = ["-C", str(tmp_path), "-c", "layout.render_check=false"]
    mats = ["--material", str(tmp_path / "情况说明.md"), "--material", str(tmp_path / "经费测算表.csv")]
    # 公开材料研发版不接入内部材料：两份材料都被禁止进入（已清除），任务不得推进到人工送审
    code = cli([*ws, "exec", "写一份申请经费的报告", *mats, "--clearance", "内部", "--accept", "task_confirm,outline_confirm,conflict,review_escalation", "--json"])
    res = [json.loads(x) for x in capsys.readouterr().out.splitlines()][-1]
    assert code == 4 and res["exit_code"] == 4
    assert res["stage"] == "材料准入" and res["version"] == 0
    assert [f["filename"] for f in res["forbidden_materials"]] == ["情况说明.md", "经费测算表.csv"]
    # 部分材料被禁止：照常推进，但在结果中报告
    (tmp_path / "密件.md").write_text("机密★1年\n示范点建设情况。", encoding="utf-8")
    code = cli([*ws, "exec", "写一份申请经费的报告", "--material", str(tmp_path / "情况说明.md"), "--material", str(tmp_path / "密件.md"), "--clearance", "公开", "--json"])
    res = [json.loads(x) for x in capsys.readouterr().out.splitlines()][-1]
    assert code in (0, 3) and [f["filename"] for f in res["forbidden_materials"]] == ["密件.md"]


def test_exec_and_task_new_validate_material_paths_first(tmp_path, capsys):
    ws = ["-C", str(tmp_path)]
    assert cli([*ws, "exec", "写一份报告", "--material", str(tmp_path / "不存在.md")]) == 2
    assert cli([*ws, "task", "new", "写一份报告", "--material", str(tmp_path)]) == 2  # 目录不是文件
    assert cli([*ws, "task", "new", "写一份报告", "--clearance", "绝密"]) == 2
    assert "材料不存在或不是文件" in capsys.readouterr().err
    assert make_engine(tmp_path).store.list_tasks() == []  # 不留下空任务


# ------------------------------------------------------------------ 命令行其他
def test_init_escapes_toml_strings(tmp_path, capsys):
    name = 'A"B\\C\x01'
    assert cli(["-C", str(tmp_path), "init", "--unit-name", name, "--region", "示例\"省"]) == 0
    cfg = tomllib.loads((tmp_path / ".gongwen" / "config.toml").read_text(encoding="utf-8"))
    assert cfg["environment"]["unit_name"] == name and cfg["environment"]["region"] == "示例\"省"


def test_format_never_overwrites_input(tmp_path, capsys):
    src = tmp_path / "原稿.md"
    src.write_text(DRAFT, encoding="utf-8")
    before = src.read_bytes()
    assert cli(["-C", str(tmp_path), "format", str(src), "-o", str(tmp_path), "--no-render"]) == 0
    assert src.read_bytes() == before
    assert (tmp_path / "原稿.排版.docx").is_file() and (tmp_path / "原稿.排版.md").is_file()


def test_corrupt_or_empty_files_are_reported(tmp_path, capsys):
    with pytest.raises(ValueError, match="无法从 bad.docx 读取文稿内容"):
        ir_from_file("bad.docx", b"notazip")
    with pytest.raises(ValueError, match="文件为空"):
        ir_from_file("空.txt", b"  \n")
    (tmp_path / "bad.docx").write_bytes(b"notazip")
    for cmd in ("check", "format"):
        assert cli(["-C", str(tmp_path), cmd, str(tmp_path / "bad.docx")]) == 2
        assert "无法从 bad.docx 读取文稿内容" in capsys.readouterr().err
    assert cli(["-C", str(tmp_path), "check", str(tmp_path)]) == 2  # 目录：中文提示而不是堆栈
    assert "错误：" in capsys.readouterr().err


def test_attachment_tables_become_table_blocks():
    text = DRAFT.replace("抄送：", "附件1\n示范点名单\n第一区示范点。\n附件2\n经费测算表\n| 项目 | 金额 |\n|---|---|\n| 设备 | 60 |\n抄送：")
    ir = ir_from_text(text)
    att = ir.attachments[1]
    assert att.title == "经费测算表" and [b.kind for b in att.blocks] == ["table"]
    assert att.blocks[0].table == [["项目", "金额"], ["设备", "60"]]


def test_chat_commands_survive_file_errors(tmp_path):
    eng = make_engine(tmp_path)
    cmds = HumanCommands(eng, default_user(), tmp_path)
    assert cmds.run("/check .").startswith("未执行：文件不存在或不是文件")
    (tmp_path / "bad.docx").write_bytes(b"notazip")
    assert cmds.run("/check bad.docx").startswith("未执行：无法从 bad.docx 读取文稿内容")
    cmds.run("/new 写一份报告")
    (tmp_path / "目录").mkdir()
    assert cmds.run("/add 目录").startswith("未执行：文件不存在")


def test_material_add_denies_data_directory(tmp_path):
    for env, sub in (({}, ".gongwen"), ({"data_dir": str(tmp_path / "数据")}, "数据")):
        eng = make_engine(tmp_path, **env)
        tid = eng.create_task("写一份报告", by=default_user()).task_id
        leaked = tmp_path / sub / "matters" / "其他事项.md"
        leaked.parent.mkdir(parents=True, exist_ok=True)
        leaked.write_text("其他任务的文稿", encoding="utf-8")
        agent = channel("agent")
        reg = build_agent_tools(eng, principal=agent, workspace=tmp_path)
        res = reg.call("material_add", {"task_id": tid, "path": f"{sub}/matters/其他事项.md"}, agent)
        assert not res.ok and "数据目录" in res.error


# ------------------------------------------------------------------ MCP：JSON-RPC 边界
def test_mcp_jsonrpc_edge_cases(tmp_path):
    srv = McpServer(make_engine(tmp_path), tmp_path)
    lines = [
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": [1, 2]},
        "[]",
        {"jsonrpc": "2.0", "id": 11},
        {"jsonrpc": "1.0", "id": 13, "method": "ping"},
        {"jsonrpc": "2.0", "id": {"x": 1}, "method": "ping"},
        {"jsonrpc": "2.0", "id": 14, "result": {}},  # 客户端的响应：忽略
        {"jsonrpc": "2.0", "id": 15, "method": "ping", "params": None},
    ]
    inp = io.StringIO("".join((x if isinstance(x, str) else json.dumps(x)) + "\n" for x in lines))
    out = io.StringIO()
    srv.serve(inp, out)
    got = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [(r["id"], r.get("error", {}).get("code")) for r in got] == [(3, -32602), (None, -32600), (11, -32600), (13, -32600), (None, -32600), (15, None)]
    assert got[-1]["result"] == {}


# ------------------------------------------------------------------ 版式编译
def _body_ir(n: int = 30, cc: int = 2):
    para = "各区要高度重视基层医疗示范点建设工作，确保今年底前完成建设任务，按月报送进展情况，及时协调解决存在的问题。"
    body = "\n".join(para for _ in range(n))
    ccs = "，".join(f"示例市第{i}区卫生健康局" for i in range(1, cc + 1))
    return ir_from_text(f"示例市卫生健康委员会文件\n示卫发〔2026〕5号\n关于加强基层医疗示范点建设的通知\n各区卫生健康局：\n{body}\n示例市卫生健康委员会\n2026年8月1日\n抄送：{ccs}。\n示例市卫生健康委员会办公室 2026年8月1日印发\n")


def test_body_line_spacing_is_579_twips(tmp_path):
    path, _ = compile_docx(_body_ir(3), tmp_path / "a.docx")
    xml = _xml(path)
    assert 'w:line="579"' in xml and 'w:line="580"' not in xml and 'w:linePitch="579"' in xml
    assert 22 * 579 <= round(225 / 25.4 * 1440)  # 22 行不超出版心


def test_keep_with_next_only_for_standalone_headings_and_signature(tmp_path):
    items = "\n".join(f"（{c}）各区要高度重视，确保今年底前完成建设任务，按月报送进展。" for c in "一二三四五六七八九十")
    ir = ir_from_text(f"关于加强基层医疗示范点建设的通知\n各区卫生健康局：\n一、总体要求\n{items}\n附件：示范点名单\n示例市卫生健康委员会\n2026年8月1日\n")
    path, _ = compile_docx(ir, tmp_path / "a.docx")
    paras = _paras(path)
    assert _keep_next(next(p for p in paras if p.text == "一、总体要求"))  # 单独成段的标题与下段同页
    assert not any(_keep_next(p) for p in paras if p.text.startswith("（"))  # 带正文的列项不连锁
    i = max(k for k, p in enumerate(paras) if p.text == "示例市卫生健康委员会")
    date = paras[i + 1]
    assert date.text == "2026年8月1日" and not _keep_next(date)
    prev = max(k for k in range(i) if paras[k].text)
    assert paras[prev].text.startswith("附件")
    assert all(_keep_next(p) for p in paras[prev : i + 1])  # 署名之前的一段、空行与署名行


def test_oversized_imprint_is_not_floating(tmp_path):
    small, _ = compile_docx(_body_ir(3, cc=3), tmp_path / "small.docx")
    huge, _ = compile_docx(_body_ir(3, cc=60), tmp_path / "huge.docx")
    assert "w:tblpPr" in _xml(small)  # 一般情况：锚定在版心底部
    assert "w:tblpPr" not in _xml(huge)  # 高于一面版心：紧接正文的普通表格


def test_long_titles_split_within_line_limit():
    titles = [
        ("示例市卫生健康委员会关于进一步加强和规范基层医疗卫生机构示范点建设管理与运行维护工作若干问题的通知", "示例市卫生健康委员会"),
        ("示例市卫生健康委员会关于转发《基层医疗卫生机构示范点建设管理办法》的通知", ""),
        ("关于进一步加强基层医疗卫生机构示范点建设管理工作的通知", ""),
        ("关于印发《示例市基层医疗卫生机构示范点建设与运行维护管理暂行办法实施细则》的通知", ""),
    ]
    for title, issuer in titles:
        lines = split_title(title, 20, issuer)
        assert "".join(lines) == title and all(text_width_chars(x) <= 20 for x in lines), lines
    long = split_title(titles[0][0], 20, titles[0][1])
    assert len(long) == 3 and max(map(len, long)) - min(map(len, long)) <= 6
    assert any("《基层医疗卫生机构示范点建设管理办法》" in x for x in split_title(titles[1][0], 20))  # 书名号内不拆行
    assert not any(x.endswith("机") for x in split_title(titles[2][0], 20))  # 不拆开“机构”


def test_title_check_measures_title_not_red_mark(tmp_path):
    ir = ir_from_text("示例市卫生健康委员会关于商请支持示范点建设的函\n示例市财政局：\n为推进示范点建设，现商请贵局给予支持。\n示例市卫生健康委员会\n2026年8月1日")
    ir.header.organ_mark = "示例市卫生健康委员会"  # 函的发文机关标志是标题的开头
    profile = LayoutProfile.load()
    path, _ = compile_docx(ir, tmp_path / "letter.docx", profile)
    check = next(c for c in check_docx(path, profile, ir.title) if c.rule_id == "DOCX-TITLE")
    assert check.actual.startswith("22磅") and check.status == "pass"


@pytest.mark.skipif(not (shutil.which("soffice") and shutil.which("pdftotext")), reason="需要 LibreOffice 与 poppler 做实际渲染")
def test_rendered_pages_hold_22_lines_and_imprint_stays_on_page(tmp_path):
    from gongwen.layout.render_check import _full_page_rows, check_rendering, pdf_pages

    ir = _body_ir(30, cc=60)
    profile = LayoutProfile.load()
    path, fonts = compile_docx(ir, tmp_path / "a.docx", profile)
    info, checks = check_rendering(path, ir, profile, fonts, tmp_path)
    by = {c.rule_id: c for c in checks}
    pages = pdf_pages(Path(info.pdf_path))
    assert any(k == 22 for _, k in _full_page_rows(pages, 37, 262, profile.line_pt * 25.4 / 72))
    assert by["LAY-LINES"].status == "pass", by["LAY-LINES"].actual
    assert all(l.y1 * 25.4 / 72 < 297 and l.y0 >= 0 for _, _, ls in pages for l in ls)  # 版记没有浮出页面
    assert by["LAY-IMPRINT"].status == "fail"  # 版记高于一面版心，无法排到最后一面底部：如实报告
    # 行距 580 缇（29 磅）时排满的页面只有 21 行：提示
    wide = LayoutProfile.load(overrides={"grid": {"line_pt": 29.0}})
    ir2 = _body_ir(30)
    path, fonts = compile_docx(ir2, tmp_path / "wide" / "b.docx", wide)
    _, checks = check_rendering(path, ir2, wide, fonts, tmp_path / "wide")
    lines = next(c for c in checks if c.rule_id == "LAY-LINES")
    assert lines.status == "warn" and "21行" in lines.actual


@pytest.mark.skipif(not (shutil.which("soffice") and shutil.which("pdftotext")), reason="需要 LibreOffice 与 poppler 做实际渲染")
def test_signature_is_not_pushed_alone_to_next_page(tmp_path):
    """正文末段之后的附件说明与署名恰好放不下时，按 7.3.5.5 缩小空行行距，署名不单独成页。"""
    from gongwen.layout.pipeline import layout_document
    from gongwen.schemas.ir import AttachmentNote

    ir = ir_from_text(
        "示例市卫生健康委员会关于印发《基层医疗示范点建设工作方案》的通知\n各区卫生健康局：\n"
        "现将《基层医疗示范点建设工作方案》印发给你们，请结合实际认真组织实施。\n示例市卫生健康委员会\n2026年3月1日\n"
    )
    ir.header.organ_mark = "示例市卫生健康委员会文件"
    ir.header.doc_number = "示卫发〔2026〕1号"
    ir.signature.seal_mode = "seal"
    ir.attachment_notes = [AttachmentNote(seq=1, name="基层医疗示范点建设工作方案")]
    rep = layout_document(ir, tmp_path)
    sig = next(c for c in rep.checks if c.rule_id == "LAY-SIGNATURE")
    assert sig.status == "pass", (sig.actual, rep.render.notes)
    assert any("7.3.5.5" in n for n in rep.render.notes)  # 首次排版署名被挤到下一面，已调整空行行距


@pytest.mark.skipif(not shutil.which("soffice"), reason="需要 LibreOffice 生成 PDF")
def test_pdf_draft_is_reflowed_into_paragraphs(tmp_path):
    """PDF 文稿：去掉页码与页眉标注、合并跨行的段落与分行的标题、去掉汉字与数字间的空格。"""
    import subprocess

    txt = (
        "示例市卫生健康委员会关于做好2026年基层医疗示范点建设工作的通知\n各区卫生健康局：\n"
        "为提升基层医疗卫生服务能力，根据市政府工作部署，现就做好2026年基层医疗示范点建设工作有关事项通知如下。\n"
        "一、工作目标\n2026年全市新建基层医疗示范点12个，各区卫生健康局要在2026年6月30日前完成选址，并于2026年12月31日前完成建设和验收工作。\n"
        "示例市卫生健康委员会\n2026年3月1日\n"
    )
    ir = ir_from_text(txt)
    path, _ = compile_docx(ir, tmp_path / "a.docx", LayoutProfile.load())
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(tmp_path), str(path)], capture_output=True, timeout=180)
    ir2 = ir_from_file("a.pdf", (tmp_path / "a.pdf").read_bytes())
    assert ir2.title == "示例市卫生健康委员会关于做好2026年基层医疗示范点建设工作的通知"
    assert ir2.recipients == ["各区卫生健康局"] and ir2.genre == "通知"
    texts = [b.text() for b in ir2.blocks]
    assert any(t.startswith("2026年全市新建") and t.endswith("完成建设和验收工作。") for t in texts)
    assert not any("—" in t or "【" in t for t in texts)
    assert ir2.signature.date == "2026年3月1日"


def test_check_fix_applies_only_mechanical_changes(tmp_path, capsys):
    """check --fix 只改标点、数字与日期写法、附件序号等机械性问题，输出修订稿与逐处清单，原稿不动；
    文种、结束语、称谓等须人工处理的问题保留在复检结果中。"""
    src = tmp_path / "稿.txt"
    text = (
        "示例市卫生健康委员会关于开展检查的通知。\n各区卫生健康局：\n"
        "检查时间为2026年4月1号至4月30号,共检查１２３家机构。\n请贵局认真组织实施。\n"
        "附件：1、检查表。\n示例市卫生健康委员会\n二〇二六年三月一日\n"
    )
    src.write_text(text, encoding="utf-8")
    rc = cli(["-C", str(tmp_path), "check", str(src), "--fix", "-o", str(tmp_path / "out"), "--json"])
    d = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert src.read_text(encoding="utf-8") == text
    fixed = (tmp_path / "out" / "稿.修订.md").read_text(encoding="utf-8")
    assert "2026年4月1日至4月30日，共检查123家机构。" in fixed
    assert "2026年3月1日" in fixed and "附件：检查表" in fixed and "关于开展检查的通知\n" in fixed  # 只有一个附件不编序号
    assert "贵局" in fixed  # 称谓问题须人工判断，不自动改
    rules_left = {i["rule"].split()[0] for i in d["issues"] if i.get("rule")}
    assert "GW-GENRE-006" in rules_left and not rules_left & {"GW-NUM-006", "GW-NUM-007", "GW-PUNC-001", "GW-FMT-002", "GW-FMT-009"}
    assert {c["rule"] for c in d["changes"]} >= {"GW-NUM-006", "GW-NUM-007", "GW-PUNC-001", "GW-FMT-002", "GW-FMT-003", "GW-FMT-009"}
    assert rc in (0, 1)


def test_exec_accept_can_be_repeated_or_comma_separated(tmp_path, capsys):
    """--accept 重复给出时全部生效（此前只保留最后一个，任务停在任务契约确认）。"""
    from gongwen.cli.main import _accept_set

    assert _accept_set(["task_confirm", "outline_confirm"]) == {"task_confirm", "outline_confirm"}
    assert _accept_set("task_confirm,outline_confirm") == {"task_confirm", "outline_confirm"}
    with pytest.raises(ValueError):
        _accept_set(["task_confirm", "human_review"])

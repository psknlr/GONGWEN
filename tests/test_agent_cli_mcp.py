"""命令行、对话代理、MCP 服务与外部文稿检查。

重点验证治理边界：模型通道（对话代理、MCP 客户端）只能推进、查询、检索、检查和提交建议；
审核节点、材料属性申报、采纳建议只能由人完成；MCP 不返回高于允许级别的任务内容。
"""

import io
import json
from pathlib import Path

import pytest
from test_engine_e2e import BUDGET, SITUATION, make_engine, run_to_review, start

from gongwen.agent import AgentSession, HumanCommands
from gongwen.cli.main import main as cli
from gongwen.harness.permissions import channel
from gongwen.importer import check_external, ir_from_text
from gongwen.llm import ScriptedProvider
from gongwen.llm.base import ModelResponse, ToolCall
from gongwen.mcp import McpServer
from gongwen.orchestrator import default_user
from gongwen.schemas.state import CheckpointKind, Stage

DRAFT = """示例市卫生健康委员会文件
示卫发[2026]第05号
关于加强基层医疗示范点建设的
通知
各区卫生健康局：
为提升基层医疗卫生服务能力，根据《国家行政机关公文处理办法》，现就有关事项通知如下。
一、总体要求
各区要高度重视，确保今年底前完成建设任务。
1.加强统筹。各区卫生健康局要每月报送进展情况表。
| 项目 | 金额（万元） |
|---|---|
| 设备购置 | 60 |
| 场地改造 | 40 |
| 合计 | 110 |
附件：1.示范点名单
2.经费测算表。
示例市卫生健康委员会
2026年08月01日
抄送：市财政局，市医保局
示例市卫生健康委员会办公室  2026年8月1日印发
"""


def write_materials(d: Path) -> None:
    (d / "情况说明.md").write_text("\n\n".join(SITUATION), encoding="utf-8")
    (d / "经费测算表.csv").write_text("\n".join(",".join(str(c) for c in r) for r in BUDGET), encoding="utf-8")


# ------------------------------------------------------------------ 外部文稿导入与检查
def test_import_structure_and_external_checks():
    ir = ir_from_text(DRAFT)
    assert ir.title == "关于加强基层医疗示范点建设的通知" and ir.genre == "通知" and ir.direction == "下行文"
    assert ir.header.doc_number == "示卫发[2026]第05号" and ir.header.organ_mark.endswith("文件")
    assert ir.recipients == ["各区卫生健康局"]
    assert ir.signature.organs == ["示例市卫生健康委员会"] and ir.signature.date == "2026年08月01日"
    assert [n.name for n in ir.attachment_notes] == ["示范点名单", "经费测算表。"]
    assert ir.imprint.cc == ["市财政局", "市医保局"] and ir.imprint.printer.startswith("示例市")
    assert any(b.kind == "table" and len(b.table) == 4 for b in ir.blocks)
    res = check_external(ir)
    rules = {i.rule.rule_id for i in res.issues if i.rule}
    assert "GW-BASIS-002" in rules  # 引用已停止执行的 2000 年《国家行政机关公文处理办法》
    assert "GW-FACT-003" in rules  # 表格合计 60+40≠110
    assert {"GW-FMT-001", "GW-FMT-002", "GW-FMT-003"} <= rules  # 发文字号、成文日期虚位、附件名称标点
    assert "GW-STYLE-003" in rules and "GW-BURDEN-001" in rules
    assert "GW-FACT-002" not in rules  # 外部文稿无证据链：不判“数字无来源”，改为人工核验提示
    assert res.unverifiable


def test_procedural_norm_is_not_substantive_basis():
    ir = ir_from_text("关于做好示范点建设工作的通知\n各区卫生健康局：\n根据《党政机关公文处理工作条例》，现将有关事项通知如下。\n示例局\n2026年8月1日")
    assert any(i.rule and i.rule.rule_id == "GW-BASIS-005" for i in check_external(ir).issues)
    ir2 = ir_from_text("关于进一步规范公文处理工作的通知\n各处室：\n根据《党政机关公文处理工作条例》，现将有关事项通知如下。\n示例局\n2026年8月1日")
    assert not any(i.rule and i.rule.rule_id == "GW-BASIS-005" for i in check_external(ir2).issues)


# ------------------------------------------------------------------ 命令行
def test_cli_exec_confirm_and_session(tmp_path, capsys):
    write_materials(tmp_path)
    ws = ["-C", str(tmp_path), "-c", "layout.render_check=false", "--user", "tester"]
    assert cli([*ws, "init", "--unit-name", "示例市卫生健康委员会", "--region", "示例省"]) == 0
    capsys.readouterr()
    # 未申报材料属性：停在“材料准入确认”，--accept 也不能跳过
    code = cli([*ws, "exec", "写一份向主管部门申请基层医疗示范点建设经费的报告", "--material", str(tmp_path / "情况说明.md"), "--to", "示例市人民政府", "--issuer", "政府部门", "--accept", "task_confirm,outline_confirm", "--json"])
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert code == 3 and lines[-1]["type"] == "result" and lines[-1]["pending_checkpoints"][0]["kind"] == CheckpointKind.MATERIAL_CONFIRM.value
    assert any(x.get("event") == "material.added" for x in lines)
    # 不允许自动接受人工送审
    assert cli([*ws, "exec", "x", "--accept", "human_review"]) == 2
    capsys.readouterr()
    # 申报为公开并自动接受任务与提纲确认：到达人工送审（仍须人处理）
    code = cli([*ws, "exec", "写一份向主管部门申请基层医疗示范点建设经费的报告", "--material", str(tmp_path / "情况说明.md"), "--material", str(tmp_path / "经费测算表.csv"), "--clearance", "公开", "--to", "示例市人民政府", "--issuer", "政府部门", "--accept", "task_confirm,outline_confirm", "--json"])
    res = [json.loads(x) for x in capsys.readouterr().out.splitlines()][-1]
    assert code == 0 and res["stage"] == Stage.HUMAN_REVIEW.value
    tid = res["task_id"]
    cp = next(c for c in res["pending_checkpoints"] if c["kind"] == CheckpointKind.HUMAN_REVIEW.value)
    assert cli([*ws, "task", "confirm", tid, cp["cp_id"], "submit", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["stage"] == Stage.SUBMITTED.value
    # 审批导入须明确确认且以有权人员身份执行
    assert cli([*ws, "task", "approve", tid, "--approver", "张某", "--date", "2026年10月8日", "--scope", "同意", "--source", "OA-1", "--yes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["stage"] == Stage.APPROVED.value
    assert cli([*ws, "session", "verify", tid]) == 0
    assert "哈希链完整" in capsys.readouterr().out


def test_cli_check_and_format(tmp_path, capsys):
    f = tmp_path / "稿.txt"
    f.write_text(DRAFT, encoding="utf-8")
    assert cli(["-C", str(tmp_path), "check", str(f), "--json"]) == 1  # 引用已废止文件：阻断送审
    d = json.loads(capsys.readouterr().out)
    assert d["genre"] == "通知" and d["counts"].get("阻断送审") == 1
    assert cli(["-C", str(tmp_path), "format", str(f), "-o", str(tmp_path / "out"), "--no-render"]) == 0
    assert (tmp_path / "out" / "稿.docx").is_file()
    from docx import Document

    doc = Document(str(tmp_path / "out" / "稿.docx"))
    assert "排版稿" in doc.sections[0].header.paragraphs[0].text
    assert "未经本系统核验" in doc.core_properties.comments


# ------------------------------------------------------------------ MCP 服务
def _mcp(server, *msgs):
    inp = io.StringIO("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in msgs))
    out = io.StringIO()
    server.serve(inp, out)
    return [json.loads(x) for x in out.getvalue().splitlines()]


def test_mcp_protocol_and_boundaries(tmp_path):
    write_materials(tmp_path)
    eng = make_engine(tmp_path)
    srv = McpServer(eng, tmp_path)
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2099-01-01", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    out = _mcp(srv, init, {"jsonrpc": "2.0", "method": "notifications/initialized"}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert out[0]["result"]["protocolVersion"] == "2025-11-25"  # 不支持的版本：回应本服务支持的最新版本
    names = {t["name"] for t in out[1]["result"]["tools"]}
    assert {"task_advance", "text_check", "revision_propose"} <= names
    assert not names & {"checkpoint_resolve", "approval_import", "proposal_apply", "admission_confirm"}
    call = lambda i, name, args: {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}}
    out = _mcp(srv, call(3, "task_create", {"request": "写一份向主管部门申请基层医疗示范点建设经费的报告", "recipients": "示例市人民政府", "issuer_type": "政府部门"}))
    tid = out[0]["result"]["structuredContent"]["task_id"]
    out = _mcp(srv, call(4, "material_add", {"task_id": tid, "path": "情况说明.md"}), call(5, "material_add", {"task_id": tid, "path": "../../etc/hosts"}), call(6, "task_advance", {"task_id": tid}))
    assert out[0]["result"]["structuredContent"]["decision"] == "需人工确认"  # 模型通道不能申报材料属性
    assert out[1]["result"]["isError"]  # 工作区外路径：无人工审批通道，拒绝
    adv = out[2]["result"]["structuredContent"]
    assert adv["stage"] == Stage.ADMISSION.value and "gongwen task confirm" in adv["human_actions"][0]
    out = _mcp(srv, call(7, "checkpoint_resolve", {"task_id": tid}), {"jsonrpc": "2.0", "id": 8, "method": "nope"}, call(9, "task_create", {"request": 5}))
    assert out[0]["error"]["code"] == -32602 and out[1]["error"]["code"] == -32601
    assert out[2]["result"]["isError"] and "参数无效" in out[2]["result"]["content"][0]["text"]
    # 审计：MCP 会话日志记录调用但不保存材料全文
    log = (Path(eng.rt.config.environment.data_dir) / "mcp").glob("*.jsonl")
    text = "".join(p.read_text(encoding="utf-8") for p in log)
    assert "tool.call" in text and SITUATION[2] not in text


def test_mcp_withholds_internal_task_content(tmp_path):
    eng = make_engine(tmp_path, route="unit_approved", accept_internal_materials=True)
    user = default_user()
    st = eng.create_task("写一份向主管部门申请基层医疗示范点建设经费的报告", by=user, hints={"recipients": "示例市人民政府", "issuer_type": "政府部门"})
    from helpers import make_docx

    from gongwen.schemas.common import Clearance

    eng.add_material(st.task_id, "情况说明.docx", make_docx(SITUATION), by=user, declared=Clearance.INTERNAL)
    srv = McpServer(eng, tmp_path)
    out = _mcp(srv, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "task_status", "arguments": {"task_id": st.task_id}}})
    r = out[0]["result"]
    assert r["isError"] and "高于 MCP 允许返回的级别" in r["content"][0]["text"]


# ------------------------------------------------------------------ 对话代理
def test_agent_session_tools_and_human_commands(tmp_path):
    write_materials(tmp_path)
    state: dict = {}

    def responder(messages, system, tools, schema):
        last = messages[-1]
        names = {t.name for t in tools or []}
        assert "checkpoint_resolve" not in names
        if last.role == "user":
            return ModelResponse(tool_calls=[ToolCall("c1", "task_create", {"request": "写一份向主管部门申请基层医疗示范点建设经费的报告", "recipients": "示例市人民政府", "issuer_type": "政府部门"})], stop_reason="tool_use")
        if last.name == "task_create":
            state["tid"] = json.loads(last.content.split("\n", 1)[1].rsplit("\n", 1)[0])["task_id"]
            return ModelResponse(tool_calls=[ToolCall("c2", "material_add", {"task_id": state["tid"], "path": "情况说明.md"}), ToolCall("c3", "checkpoint_resolve", {"task_id": state["tid"]})], stop_reason="tool_use")
        if last.name == "checkpoint_resolve":
            assert last.content.startswith("错误：未知工具")
            return ModelResponse(tool_calls=[ToolCall("c4", "task_advance", {"task_id": state["tid"]})], stop_reason="tool_use")
        return ModelResponse(text="材料需要您确认属性：请输入 /confirm。", stop_reason="end_turn")

    scripted = ScriptedProvider(responder=responder)
    eng = make_engine(tmp_path, providers={"*": scripted})
    user = default_user()
    session = AgentSession(eng, human=user, workspace=tmp_path)
    assert session.model_status()[0]
    reply = session.send("帮我写个申请经费的报告")
    assert "/confirm" in reply.text
    assert [e.name for e in reply.events] == ["task_create", "material_add", "checkpoint_resolve", "task_advance"]
    tid = state["tid"]
    st = eng.load_state(tid)
    cp = next(c for c in st.pending_checkpoints() if c.kind == CheckpointKind.MATERIAL_CONFIRM)
    # 人工通道：斜杠命令处理审核节点
    cmds = HumanCommands(eng, user, tmp_path, session=session)
    cmds.current = tid
    out = cmds.run(f"/confirm {cp.cp_id} confirm --data '{{\"materials\": {{\"MAT-001\": \"公开\"}}}}'")
    assert "任务确认" in out
    # 对话日志只记哈希与摘要；工具结果作为不可信数据回传
    tool_msgs = [m for m in session.messages if m.role == "tool" and not m.content.startswith("错误")]
    assert all(m.content.startswith("<untrusted") for m in tool_msgs)


def test_proposals_require_human_adoption(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    st = start(eng, user)
    st = run_to_review(eng, user, st.task_id)
    agent = channel("agent")
    item = eng.propose_revision(st.task_id, by=agent, instruction="把第一段改得更简洁", reason="冗余")
    assert item["status"] == "pending" and eng.load_state(st.task_id).current_version == 1
    with pytest.raises(PermissionError):
        eng.apply_proposal(st.task_id, item["proposal_id"], by=agent)
    with pytest.raises(PermissionError):
        eng.request_revision(st.task_id, by=agent, instruction="x")
    st2 = eng.apply_proposal(st.task_id, item["proposal_id"], by=user)
    assert st2.stage == Stage.REVIEW and st2.current_version == 2
    assert eng.proposals(st.task_id, "applied")[0]["decided_by"] == user.id

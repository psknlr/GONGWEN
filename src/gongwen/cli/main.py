"""命令行入口 gongwen。

形态借鉴 Codex（exec 无头模式、-c 覆盖配置、配置档 profile、MCP 服务模式、会话回放）与
grok-cli（交互式对话、无头 NDJSON 输出、钩子），但办文的关键节点始终由程序化状态机把关：
无头模式也只能按显式 --accept 自动接受“任务确认、提纲确认、冲突暂不采信、审校问题保留”四类节点，
“材料准入确认”“权限确认”“人工送审”永远需要人来处理。

退出码：0 已到人工送审或已完成；3 停在待人工处理的审核节点；4 禁止进入/超出权限；1 处理失败；2 用法错误。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
import threading
import tomllib
from pathlib import Path
from typing import Any

from .. import PROTOCOL_VERSION, __version__

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_WAIT, EXIT_BLOCKED = 0, 1, 2, 3, 4

GONGWEN_MD_TEMPLATE = """# 本单位办文说明（GONGWEN.md）

本文件会被公文智能体读取，作为本工作区的办文补充说明（与 AGENTS.md 一样按目录层级合并，
子目录可用 GONGWEN.override.md 覆盖）。只写本单位制度和习惯，不写具体事实数据。

## 单位信息
- 单位名称：
- 公文处理制度：（如《××委公文处理实施细则》，写明版本与施行日期）

## 本单位惯例（实务，非国标规定）
- 联系人附注写法：
- 版记印发机关：
- 需要会签的部门：

## 禁止事项
- 不得在文稿中写入未经审批的经费、编制、考核要求。
"""


# ---------------------------------------------------------------- 公共工具
def _parse_override(item: str) -> dict[str, Any]:
    key, sep, raw = item.partition("=")
    if not sep:
        raise ValueError(f"-c 参数应为 key=value：{item}")
    try:
        value = tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        value = raw
    out: dict[str, Any] = {}
    cur = out
    parts = key.strip().split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value
    return out


def _merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def tty_approver(req) -> bool:
    """工具动作审批（如写出工作区以外）：只在交互终端询问。"""
    where = "，越出工作区" if req.outside_workspace else ""
    print(f"\n需要审批：{req.summary}（工具 {req.tool}，风险 {req.risk}{where}）", file=sys.stderr)
    try:
        return input("是否批准？[y/N] ").strip().lower() in ("y", "yes", "是")
    except EOFError:
        return False


def workspace_of(args) -> Path:
    return Path(args.workspace or os.getcwd()).resolve()


def make_engine(args, *, render: bool | None = None, interactive: bool = True):
    from ..orchestrator import Engine
    from ..runtime import build_runtime

    overrides: dict[str, Any] = {}
    for item in args.config or []:
        overrides = _merge(overrides, _parse_override(item))
    if getattr(args, "offline", False):
        overrides = _merge(overrides, {"model": {"provider": "offline"}, "models": {}, "routing": {"light": None, "heavy": None, "reviewer": None}})
    if render is not None:
        overrides = _merge(overrides, {"layout": {"render_check": render}})
    # 只有交互终端才有人工审批通道；MCP 等管道模式下标准输入是协议流，绝不能用来询问
    approver = tty_approver if interactive and sys.stdin.isatty() else None
    rt = build_runtime(workspace_of(args), profile=args.profile, overrides=overrides or None, approver=approver)
    return Engine(rt)


def human_user(args, *roles: str):
    from ..harness.permissions import human
    from ..orchestrator import default_user

    uid = args.user or os.environ.get("GONGWEN_USER") or getpass.getuser() or "local-user"
    return human(uid, *roles) if roles else default_user(uid)


def emit_json(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def exit_code_for(stage: str) -> int:
    from ..schemas.state import Stage

    if stage in (Stage.HUMAN_REVIEW.value, Stage.SUBMITTED.value, Stage.APPROVED.value):
        return EXIT_OK
    if stage == Stage.FAILED.value:
        return EXIT_FAIL
    if stage in (Stage.BLOCKED.value, Stage.OUT_OF_AUTHORITY.value):
        return EXIT_BLOCKED
    return EXIT_WAIT


def _status_out(args, eng, task_id: str) -> int:
    from ..agent.render import fmt_status

    s = eng.status(task_id)
    if getattr(args, "json", False):
        emit_json(s)
    else:
        print(fmt_status(s))
    return exit_code_for(s["stage"])


def _clearance(value: str | None):
    from ..schemas.common import Clearance

    if not value:
        return None
    c = next((c for c in Clearance if c.value == value), None)
    if c is None or c in (Clearance.CLASSIFIED, Clearance.UNKNOWN):
        raise ValueError("材料属性应为：公开、内部、敏感、工作秘密（涉密材料不得进入本系统）")
    return c


# ---------------------------------------------------------------- init / doctor / config
def cmd_init(args) -> int:
    from ..kernel.config import DEFAULT_CONFIG_TOML
    from ..knowledge import kb

    ws = workspace_of(args)
    cfg_dir = ws / ".gongwen"
    cfg_path = cfg_dir / "config.toml"
    if cfg_path.exists() and not args.force:
        print(f"已存在 {cfg_path}（如需覆盖请加 --force）")
    else:
        if args.unit_profile not in kb.list_profiles():
            raise ValueError(f"未知单位配置档：{args.unit_profile}（可选：{'、'.join(kb.list_profiles())}）")
        text = DEFAULT_CONFIG_TOML.replace('unit_profile = "party_gov"', f'unit_profile = "{args.unit_profile}"')
        text = text.replace('unit_name = ""', f'unit_name = "{args.unit_name or ""}"').replace('region = ""', f'region = "{args.region or ""}"', 1)
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(text, encoding="utf-8")
        (cfg_dir / ".gitignore").write_text("# 任务数据、材料与审计日志不入版本库\n*\n!config.toml\n!.gitignore\n", encoding="utf-8")
        print(f"已写入 {cfg_path}")
    md = ws / "GONGWEN.md"
    if not md.exists():
        md.write_text(GONGWEN_MD_TEMPLATE, encoding="utf-8")
        print(f"已写入 {md}（本单位办文说明，可按需填写）")
    print("下一步：gongwen doctor 检查环境；gongwen chat 进入对话；或 gongwen exec \"办文需求\" --material 文件 --clearance 公开")
    return EXIT_OK


def cmd_doctor(args) -> int:
    from ..layout.render_check import installed_font_families, tools_available
    from ..skills.base import SKILLS_DIR

    eng = make_engine(args)
    rt = eng.rt
    cfg = rt.config
    ok = True
    print(f"公文智能体 {__version__}（Protocol v{PROTOCOL_VERSION}）")
    print(f"工作区：{rt.workspace}\n数据目录：{cfg.environment.data_dir}")
    print(f"环境路线：{cfg.environment.route.value}　单位配置档：{cfg.environment.unit_profile}　审批策略：{cfg.approval.policy}")
    for mod in ("docx", "openpyxl", "pypdf", "httpx", "yaml", "pydantic"):
        try:
            __import__(mod)
            print(f"  [ok] Python 依赖 {mod}")
        except ImportError:
            ok = False
            print(f"  [缺失] Python 依赖 {mod}")
    try:
        __import__("anthropic")
        print("  [ok] 可选依赖 anthropic（Claude 适配）")
    except ImportError:
        print("  [--] 可选依赖 anthropic 未安装（仅使用 Claude 时需要：pip install \"gongwen[anthropic]\"）")
    tools = tools_available()
    for k, v in tools.items():
        print(f"  [{'ok' if v else '--'}] {k}{'：' + v if v else '（未安装：无法做实际渲染核验，排版结果将标注“未核验”）'}")
    fams = installed_font_families()
    for f in ("仿宋_GB2312", "楷体_GB2312", "黑体", "方正小标宋简体", "宋体"):
        hit = any(f in x for x in fams)
        print(f"  [{'ok' if hit else '--'}] 字体 {f}{'' if hit else '（未安装：渲染时会被替代，报告中会标明）'}")
    router = rt.router()
    for role, desc in router.describe().items():
        print(f"  模型 {role}：{desc}")
    print(f"  出网白名单：{cfg.egress.allowed_hosts or '（空：除本机外一律拒绝）'}　MCP 返回上限：{cfg.mcp.max_clearance.value}")
    lib = rt.policies
    print(f"  依据库：{len(lib.docs)} 份文件（含已废止旧规，用于识别过时引用）")
    print(f"  技能说明目录：{SKILLS_DIR}（{len(list(SKILLS_DIR.glob('*/SKILL.md')))} 项）")
    inst = rt.instructions()
    print(f"  办文说明（GONGWEN.md/AGENTS.md）：{'已加载 ' + str(len(inst)) + ' 字' if inst else '未找到'}")
    return EXIT_OK if ok else EXIT_FAIL


def cmd_config_show(args) -> int:
    eng = make_engine(args)
    data = json.loads(eng.rt.config.model_dump_json())
    for m in [data.get("model", {})] + list((data.get("models") or {}).values()):
        if m.get("extra_headers"):
            m["extra_headers"] = {k: "***" for k in m["extra_headers"]}
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return EXIT_OK


# ---------------------------------------------------------------- task
def cmd_task_new(args) -> int:
    eng = make_engine(args)
    user = human_user(args)
    hints = {k: v for k, v in {"recipients": args.to, "issuer_type": args.issuer, "genre": args.genre}.items() if v}
    st = eng.create_task(args.request, by=user, matter_id=args.matter, hints=hints)
    declared = _clearance(args.clearance)
    for m in args.material or []:
        p = Path(m)
        res = eng.add_material(st.task_id, p.name, p.read_bytes(), by=user, declared=declared)
        print(f"{res.material_id} {p.name}：{res.decision.value}（{res.detected_clearance.value}）", file=sys.stderr)
    if args.json:
        emit_json({"task_id": st.task_id, "matter_id": st.matter_id})
    else:
        print(st.task_id)
    return EXIT_OK


def cmd_task_add(args) -> int:
    eng = make_engine(args)
    p = Path(args.file)
    res = eng.add_material(args.task_id, p.name, p.read_bytes(), by=human_user(args), declared=_clearance(args.clearance), role=args.role, authoritative=args.authoritative)
    out = {"material_id": res.material_id, "decision": res.decision.value, "clearance": res.detected_clearance.value, "reasons": res.reasons}
    emit_json(out) if args.json else print(f"{res.material_id} {p.name}：{res.decision.value}（{res.detected_clearance.value}）" + "".join(f"\n  · {r}" for r in res.reasons))
    return EXIT_BLOCKED if res.decision.value == "禁止进入当前环境" else EXIT_OK


def _accept_set(value: str | None) -> set[str]:
    from ..orchestrator.checkpoints import AUTO_ACCEPT_KEYS

    keys = {x.strip() for x in (value or "").split(",") if x.strip()}
    bad = keys - set(AUTO_ACCEPT_KEYS)
    if bad:
        raise ValueError(f"--accept 只能是：{'、'.join(AUTO_ACCEPT_KEYS)}（材料准入、权限确认、人工送审永远需要人工处理）；无效：{'、'.join(sorted(bad))}")
    return keys


def cmd_task_advance(args) -> int:
    eng = make_engine(args)
    eng.advance(args.task_id, by=human_user(args), auto_accept=_accept_set(args.accept))
    return _status_out(args, eng, args.task_id)


def cmd_task_status(args) -> int:
    return _status_out(args, make_engine(args), args.task_id)


def cmd_task_list(args) -> int:
    eng = make_engine(args)
    rows = []
    for tid in reversed(eng.store.list_tasks()[-args.limit :]):
        st = eng.load_state(tid)
        rows.append({"task_id": tid, "stage": st.stage.value, "doc_status": st.doc_status.value, "pending": len(st.pending_checkpoints()), "request": str(st.options.get("request", ""))[:40]})
    if args.json:
        emit_json(rows)
    else:
        for r in rows:
            print(f"{r['task_id']}　{r['stage']}　{r['doc_status']}　待确认 {r['pending']}　{r['request']}")
    return EXIT_OK


def cmd_task_confirm(args) -> int:
    eng = make_engine(args)
    user = human_user(args)
    data = json.loads(args.data) if args.data else {}
    eng.resolve_checkpoint(args.task_id, args.cp_id, args.option, by=user, note=args.note or "", data=data)
    eng.advance(args.task_id, by=user)
    return _status_out(args, eng, args.task_id)


def _commands(args, eng=None):
    from ..agent.commands import HumanCommands

    eng = eng or make_engine(args)
    hc = HumanCommands(eng, human_user(args), workspace_of(args))
    hc.current = args.task_id
    return hc


def cmd_task_draft(args) -> int:
    print(_commands(args).run("/draft"))
    return EXIT_OK


def cmd_task_issues(args) -> int:
    print(_commands(args).run(f"/issues {args.min}"))
    return EXIT_OK


def cmd_task_evidence(args) -> int:
    print(_commands(args).run(f"/evidence {args.sid}"))
    return EXIT_OK


def cmd_task_revise(args) -> int:
    eng = make_engine(args)
    user = human_user(args)
    edits = []
    for e in args.edit or []:
        sid, sep, text = e.partition("=")
        if not sep:
            raise ValueError("--edit 应为 句号=新句子")
        edits.append({"sid": sid, "text": text})
    facts = []
    for f in args.fact or []:
        fid, sep, val = f.partition("=")
        if not sep:
            raise ValueError("--fact 应为 事实编号=新值")
        v: Any = val
        try:
            v = float(val) if "." in val else int(val)
        except ValueError:
            pass
        facts.append({"fact_id": fid, "new_value": v, "reason": args.reason or "人工更正"})
    if not (args.instruction or edits or facts):
        raise ValueError("请至少提供 --instruction、--edit 或 --fact 之一")
    eng.request_revision(args.task_id, by=user, instruction=args.instruction, edits=edits, fact_changes=facts)
    eng.advance(args.task_id, by=user)
    return _status_out(args, eng, args.task_id)


def cmd_task_proposals(args) -> int:
    print(_commands(args).run("/proposals"))
    return EXIT_OK


def cmd_task_apply(args) -> int:
    eng = make_engine(args)
    user = human_user(args)
    eng.apply_proposal(args.task_id, args.proposal_id, by=user)
    eng.advance(args.task_id, by=user)
    return _status_out(args, eng, args.task_id)


def cmd_task_reject(args) -> int:
    eng = make_engine(args)
    eng.reject_proposal(args.task_id, args.proposal_id, by=human_user(args), note=args.note or "")
    print(f"已拒绝修改建议 {args.proposal_id}")
    return EXIT_OK


def cmd_task_approve(args) -> int:
    """把真实审批记录绑定到当前文稿（须以有权人员身份执行）。系统本身不形成审批结论。"""
    eng = make_engine(args)
    user = human_user(args, "approver")
    st = eng.load_state(args.task_id)
    print(f"将把以下审批记录绑定到任务 {args.task_id} 第 {st.current_version} 版文稿：\n  签批人：{args.approver}{('（' + args.title + '）') if args.title else ''}\n  时间：{args.date}\n  范围：{args.scope}\n  来源：{args.source}", file=sys.stderr)
    if not args.yes:
        if not sys.stdin.isatty():
            raise PermissionError("非交互环境须加 --yes 明确确认；审批记录必须来自真实办理流程")
        if input("确认这是真实审批记录？[y/N] ").strip().lower() not in ("y", "yes", "是"):
            print("已取消")
            return EXIT_USAGE
    eng.import_approval(args.task_id, by=user, approver=args.approver, approved_at=args.date, scope=args.scope, source=args.source, approver_title=args.title or "")
    return _status_out(args, eng, args.task_id)


# ---------------------------------------------------------------- exec（无头）
def cmd_exec(args) -> int:
    eng = make_engine(args)
    user = human_user(args)
    accept = _accept_set(args.accept)
    if args.json:
        eng.rt.listeners.append(lambda tid, rec: emit_json({"type": "event", "task_id": tid, "event": rec.get("type"), **{k: rec.get(k) for k in ("seq", "ts", "actor", "stage", "payload")}}))
    hints = {k: v for k, v in {"recipients": args.to, "issuer_type": args.issuer, "genre": args.genre}.items() if v}
    st = eng.create_task(args.request, by=user, matter_id=args.matter, hints=hints)
    declared = _clearance(args.clearance)
    for m in args.material or []:
        p = Path(m)
        if not p.is_file():
            raise FileNotFoundError(f"材料不存在：{m}")
        eng.add_material(st.task_id, p.name, p.read_bytes(), by=user, declared=declared)
    eng.advance(st.task_id, by=user, auto_accept=accept)
    s = eng.status(st.task_id)
    code = exit_code_for(s["stage"])
    if args.json:
        emit_json({"type": "result", "exit_code": code, **s})
    else:
        from ..agent.render import fmt_status

        print(fmt_status(s))
        if code == EXIT_WAIT:
            print("\n任务停在需要人工处理的审核节点：用 gongwen task confirm 处理后再 gongwen task advance，或运行 gongwen serve 在浏览器中处理。")
    return code


# ---------------------------------------------------------------- chat
def cmd_chat(args) -> int:
    from ..agent import AgentSession, HumanCommands

    eng = make_engine(args)
    user = human_user(args)
    ws = workspace_of(args)

    def on_tool(ev) -> None:
        mark = "✓" if ev.ok else "✗"
        print(f"  {mark} {ev.name}：{ev.summary}", file=sys.stderr)

    session = AgentSession(eng, human=user, workspace=ws, on_tool=on_tool)
    server_box: dict[str, Any] = {}

    def serve_cb() -> str:
        if "url" in server_box:
            return f"工作台已在运行：{server_box['url']}"
        from ..workbench.server import serve

        httpd, _token = serve(eng, user, port=args.port)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        server_box["url"] = f"http://127.0.0.1:{httpd.server_address[1]}/"
        return f"本地审阅工作台：{server_box['url']}（仅本机可访问；关闭对话即停止）"

    cmds = HumanCommands(eng, user, ws, session=session, serve_cb=serve_cb)
    if args.task:
        print(cmds.run(f"/use {args.task}"))
    ok, desc = session.model_status()
    print(f"公文智能体 {__version__} · 对话模式（{desc}）\n输入办文需求与助手对话；斜杠命令由你本人执行（/help 查看）。Ctrl-D 退出。")
    while True:
        try:
            line = input("\n公文> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return EXIT_OK
        if not line:
            continue
        if line in ("/quit", "/exit"):
            return EXIT_OK
        if line.startswith("/"):
            print(cmds.run(line))
            continue
        ok, desc = session.model_status()
        if not ok:
            print(desc)
            continue
        reply = session.send(line)
        print(reply.text)


# ---------------------------------------------------------------- serve / mcp
def cmd_serve(args) -> int:
    from ..workbench.server import serve

    eng = make_engine(args)
    httpd, _token = serve(eng, human_user(args), host=args.host, port=args.port)
    url = f"http://{args.host}:{httpd.server_address[1]}/"
    print(f"本地审阅工作台：{url}\n仅本机可访问；写操作需本次启动生成的会话令牌（页面内置）。Ctrl-C 停止。", file=sys.stderr)
    if args.open:
        import webbrowser

        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return EXIT_OK


def cmd_mcp(args) -> int:
    from ..mcp.server import McpServer

    eng = make_engine(args, interactive=False)
    McpServer(eng, workspace_of(args)).serve()
    return EXIT_OK


# ---------------------------------------------------------------- check / format
def cmd_check(args) -> int:
    from datetime import date

    from ..agent.render import fmt_check
    from ..importer import check_external, ir_from_file
    from ..runtime import build_runtime

    rt = build_runtime(workspace_of(args), profile=args.profile)
    p = Path(args.file)
    ir = ir_from_file(p.name, p.read_bytes(), genre=args.genre)
    res = check_external(ir, rt, as_of=date.fromisoformat(args.as_of) if args.as_of else None, region=args.region)
    d = res.to_dict()
    emit_json(d) if args.json else print(fmt_check(d))
    return EXIT_FAIL if any(i.severity.value == "阻断送审" for i in res.issues) else EXIT_OK


def cmd_format(args) -> int:
    from ..importer import ir_from_file
    from ..layout.pipeline import layout_document
    from ..runtime import build_runtime

    rt = build_runtime(workspace_of(args), profile=args.profile)
    p = Path(args.file)
    ir = ir_from_file(p.name, p.read_bytes(), genre=args.genre)
    out = Path(args.out or (p.parent / "gongwen-format"))
    report = layout_document(ir, out, profile_id=rt.config.layout.profile, margin_mode=args.margin or rt.config.layout.margin_mode, render_check=not args.no_render, stem=p.stem)
    if args.json:
        emit_json(json.loads(report.model_dump_json()))
    else:
        for o in report.outputs:
            print(f"输出 {o.kind}：{o.path}")
        for c in report.checks:
            if c.status != "pass":
                print(f"  [{c.status}] {c.item}：要求 {c.expected}；实际 {c.actual}（{c.clause}{'，条件性' if c.conditional else ''}）")
        print("说明：本命令只排版，不核验正文内容；页眉标注“排版稿”。字体替代与未核验项见上。")
    return EXIT_OK


# ---------------------------------------------------------------- policy
def cmd_policy_search(args) -> int:
    from ..agent.commands import HumanCommands

    eng = make_engine(args)
    print(HumanCommands(eng, human_user(args), workspace_of(args)).run(f"/policy {args.query}"))
    return EXIT_OK


def cmd_policy_list(args) -> int:
    from ..runtime import build_runtime

    lib = build_runtime(workspace_of(args), profile=args.profile).policies
    for p in sorted(lib.docs.values(), key=lambda d: (d.status != "现行有效", d.policy_id)):
        print(f"{p.policy_id}　《{p.title}》{('（' + p.doc_number + '）') if p.doc_number else ''}　{p.level.value}｜{p.status}｜条款 {len(p.articles)}｜{p.verification}")
    return EXIT_OK


def cmd_policy_show(args) -> int:
    from ..runtime import build_runtime

    lib = build_runtime(workspace_of(args), profile=args.profile).policies
    p = lib.get(args.policy_id)
    if p is None:
        raise KeyError(f"依据库中没有：{args.policy_id}")
    d = json.loads(p.model_dump_json())
    if not args.articles:
        d["articles"] = f"{len(p.articles)} 条（加 --articles 显示）"
    print(json.dumps(d, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_policy_add(args) -> int:
    """把人工整理的依据文件登记到本工作区依据库（数据区一）。须补全效力与适用元数据。"""
    import yaml

    from ..schemas.policy import PolicyDocument

    eng = make_engine(args)
    user = human_user(args, "admin")
    from ..harness.permissions import Action

    eng.rt.permissions.require(user, Action.POLICY_PROMOTE)
    src = Path(args.file)
    items = yaml.safe_load(src.read_text(encoding="utf-8"))
    items = items if isinstance(items, list) else [items]
    missing = []
    for it in items:
        doc = PolicyDocument.model_validate({k: v for k, v in it.items() if k != "articles_file"})
        for f in ("issuers", "publish_date", "status", "regions", "subjects", "source_url"):
            if not getattr(doc, f):
                missing.append(f"{doc.policy_id}.{f}")
    if missing and not args.allow_incomplete:
        raise ValueError("依据元数据不完整（须写明发布机关、发布日期、效力状态、适用地域与主体、来源）：" + "、".join(missing))
    dest_dir = Path(eng.rt.config.environment.data_dir) / "policies"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    shutil.copyfile(src, dest)
    print(f"已登记到 {dest}（{len(items)} 份）。登记人：{user.id}；请在“verification”中写明人工核验情况。")
    return EXIT_OK


# ---------------------------------------------------------------- skills
def cmd_skills_list(args) -> int:
    from ..skills import build_registry

    for s in build_registry():
        d = s.describe()
        print(f"{d['number']:>2}. {d['name']}　{d['title']}　阶段：{d['stage']}　通道：{d['channel']}")
        if d["description"]:
            print(f"    {d['description'][:110]}")
    return EXIT_OK


def cmd_skills_show(args) -> int:
    from ..skills.base import SKILLS_DIR

    p = SKILLS_DIR / args.name / "SKILL.md"
    if not p.is_file():
        raise KeyError(f"没有技能：{args.name}")
    print(p.read_text(encoding="utf-8"))
    return EXIT_OK


def cmd_skills_install(args) -> int:
    """把 12 项技能说明（Agent Skills 格式）复制到其他智能体可发现的目录。"""
    from ..skills.base import SKILLS_DIR

    dest = Path(args.dest)
    dest = dest if dest.is_absolute() else workspace_of(args) / dest
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for d in sorted(SKILLS_DIR.iterdir()):
        if not (d / "SKILL.md").is_file():
            continue
        target = dest / d.name
        if target.exists():
            if not args.force:
                print(f"跳过（已存在）：{target}")
                continue
            shutil.rmtree(target)
        shutil.copytree(d, target)
        n += 1
    print(f"已安装 {n} 项技能到 {dest}")
    print("这些技能通过 gongwen 的 MCP 工具或命令行执行；请同时配置 MCP 服务（见 integrations/）。")
    return EXIT_OK


def cmd_skills_schemas(args) -> int:
    from ..schemas import SKILL_OUTPUT_SCHEMAS

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, model in SKILL_OUTPUT_SCHEMAS.items():
        (out / f"{name}.schema.json").write_text(json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已导出 {len(SKILL_OUTPUT_SCHEMAS)} 个技能输出 Schema 到 {out}")
    return EXIT_OK


# ---------------------------------------------------------------- session
def _session_log(args, eng):
    p = eng.store.task_dir(args.task_id) / "events.jsonl"
    if not p.exists():
        raise KeyError(f"任务没有会话日志：{args.task_id}")
    return eng.rt.session_log(args.task_id)


def cmd_session_verify(args) -> int:
    eng = make_engine(args)
    log = _session_log(args, eng)
    ok, bad = log.verify_chain()
    print(f"哈希链完整：{log.seq} 条记录" if ok else f"哈希链在第 {bad} 条处断裂：日志可能被篡改或损坏")
    return EXIT_OK if ok else EXIT_FAIL


def cmd_session_replay(args) -> int:
    eng = make_engine(args)
    log = _session_log(args, eng)
    types = set(args.type.split(",")) if args.type else None
    for rec in log.replay(types):
        if args.json:
            emit_json(rec)
        else:
            payload = json.dumps(rec.get("payload", {}), ensure_ascii=False)
            print(f"#{rec['seq']:>4} {rec['ts'][:19]} {rec['type']:<22} {rec.get('actor', ''):<16} {payload[:150]}")
    return EXIT_OK


def cmd_session_fork(args) -> int:
    eng = make_engine(args)
    log = _session_log(args, eng)
    dest = Path(args.out) if args.out else eng.store.task_dir(args.task_id) / f"events.fork-{args.upto or 'end'}.jsonl"
    log.fork(dest, args.upto)
    print(f"已分叉到 {dest}")
    return EXIT_OK


# ---------------------------------------------------------------- eval
def cmd_eval(args) -> int:
    from ..eval.runner import main as eval_main

    return eval_main(args)


# ---------------------------------------------------------------- 解析器
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gongwen", description="公文智能体：证据约束、规范校验与人机协同的公文拟制系统")
    ap.add_argument("--version", action="version", version=f"gongwen {__version__}（Protocol v{PROTOCOL_VERSION}）")
    ap.add_argument("-C", "--workspace", help="工作区目录（默认当前目录）")
    ap.add_argument("-p", "--profile", help="配置档（config.toml 中的 [profiles.<名称>]）")
    ap.add_argument("-c", "--config", action="append", metavar="KEY=VALUE", help="覆盖配置，如 -c model.provider=\"deepseek\"（可重复）")
    ap.add_argument("--user", help="操作人标识（默认取 GONGWEN_USER 或系统用户名）")
    ap.add_argument("--offline", action="store_true", help="本次不调用任何模型（确定性路径）")
    sub = ap.add_subparsers(dest="cmd", metavar="命令")

    p = sub.add_parser("init", help="初始化工作区配置与办文说明")
    p.add_argument("--unit-profile", default="party_gov", help="party_gov / hospital / university / research_institute")
    p.add_argument("--unit-name")
    p.add_argument("--region")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    sub.add_parser("doctor", help="检查依赖、渲染工具、字体、模型与出网配置").set_defaults(func=cmd_doctor)
    sub.add_parser("config", help="显示生效配置").set_defaults(func=cmd_config_show)

    t = sub.add_parser("task", help="办文任务（人工通道）").add_subparsers(dest="task_cmd", metavar="子命令")
    p = t.add_parser("new", help="创建任务")
    p.add_argument("request")
    p.add_argument("--to", help="主送机关（多个用顿号分隔）")
    p.add_argument("--issuer", help="发文机关类型，如 政府部门")
    p.add_argument("--genre", help="指定文种（系统仍会判断是否适当）")
    p.add_argument("--matter", help="事项编号（同一事项多份文稿共享事实账本）")
    p.add_argument("--material", action="append", help="材料文件（可重复）")
    p.add_argument("--clearance", help="申报材料属性：公开/内部/敏感/工作秘密")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_task_new)
    p = t.add_parser("add", help="添加材料")
    p.add_argument("task_id")
    p.add_argument("file")
    p.add_argument("--clearance")
    p.add_argument("--role", default="material")
    p.add_argument("--authoritative", action="store_true", help="标记为权威来源（仅人工）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_task_add)
    p = t.add_parser("advance", help="推进到下一个人工审核节点")
    p.add_argument("task_id")
    p.add_argument("--accept", help="自动接受的节点：task_confirm,outline_confirm,conflict,review_escalation")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_task_advance)
    p = t.add_parser("status", help="任务状态")
    p.add_argument("task_id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_task_status)
    p = t.add_parser("list", help="最近任务")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_task_list)
    p = t.add_parser("confirm", help="处理审核节点")
    p.add_argument("task_id")
    p.add_argument("cp_id")
    p.add_argument("option")
    p.add_argument("--note")
    p.add_argument("--data", help="JSON，如 '{\"confirm_facts\": [\"F-003\"]}'")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_task_confirm)
    for name, fn, extra in (("draft", cmd_task_draft, None), ("issues", cmd_task_issues, "min"), ("evidence", cmd_task_evidence, "sid")):
        p = t.add_parser(name, help={"draft": "查看文稿", "issues": "查看审校问题", "evidence": "查看某句证据"}[name])
        p.add_argument("task_id")
        if extra == "min":
            p.add_argument("--min", default="一般", help="最低严重度：阻断送审/重要/一般/提示")
        elif extra == "sid":
            p.add_argument("sid")
        p.set_defaults(func=fn)
    p = t.add_parser("revise", help="人工发起修订")
    p.add_argument("task_id")
    p.add_argument("--instruction")
    p.add_argument("--edit", action="append", help="句号=新句子（可重复）")
    p.add_argument("--fact", action="append", help="事实编号=新值（可重复）")
    p.add_argument("--reason")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_task_revise)
    p = t.add_parser("proposals", help="待采纳的修改建议")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_proposals)
    p = t.add_parser("apply", help="采纳修改建议")
    p.add_argument("task_id")
    p.add_argument("proposal_id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_task_apply)
    p = t.add_parser("reject", help="拒绝修改建议")
    p.add_argument("task_id")
    p.add_argument("proposal_id")
    p.add_argument("--note")
    p.set_defaults(func=cmd_task_reject)
    p = t.add_parser("approve", help="绑定真实审批记录（须有权人员）")
    p.add_argument("task_id")
    p.add_argument("--approver", required=True)
    p.add_argument("--title", help="签批人职务")
    p.add_argument("--date", required=True, help="签批时间，如 2026年10月8日")
    p.add_argument("--scope", required=True, help="审批范围/意见")
    p.add_argument("--source", required=True, help="审批记录来源，如 OA 流水号")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_task_approve)

    p = sub.add_parser("exec", help="无头执行：创建任务、添加材料并推进（可输出 NDJSON）")
    p.add_argument("request")
    p.add_argument("--material", action="append")
    p.add_argument("--clearance", help="申报材料属性（启动者对全部材料的申报）")
    p.add_argument("--to")
    p.add_argument("--issuer")
    p.add_argument("--genre")
    p.add_argument("--matter")
    p.add_argument("--accept", help="自动接受：task_confirm,outline_confirm,conflict,review_escalation")
    p.add_argument("--json", action="store_true", help="以 NDJSON 输出事件流与最终结果")
    p.set_defaults(func=cmd_exec)

    p = sub.add_parser("chat", help="对话模式（模型助手 + 人工斜杠命令）")
    p.add_argument("--task")
    p.add_argument("--port", type=int, default=0, help="/serve 使用的端口（默认随机）")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("serve", help="本地审阅工作台（仅本机）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--open", action="store_true")
    p.set_defaults(func=cmd_serve)

    sub.add_parser("mcp", help="以 MCP 服务方式运行（stdio）").set_defaults(func=cmd_mcp)

    p = sub.add_parser("check", help="检查一份已有文稿（txt/md/docx/pdf）")
    p.add_argument("file")
    p.add_argument("--genre")
    p.add_argument("--as-of", help="依据适用时点 YYYY-MM-DD")
    p.add_argument("--region")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("format", help="按 GB/T 9704—2012 对已有文稿排版并核验")
    p.add_argument("file")
    p.add_argument("-o", "--out", help="输出目录")
    p.add_argument("--genre")
    p.add_argument("--margin", choices=["standard", "compensated"])
    p.add_argument("--no-render", action="store_true", help="不做实际渲染核验")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_format)

    pol = sub.add_parser("policy", help="权威依据库").add_subparsers(dest="policy_cmd", metavar="子命令")
    p = pol.add_parser("search")
    p.add_argument("query")
    p.set_defaults(func=cmd_policy_search)
    pol.add_parser("list").set_defaults(func=cmd_policy_list)
    p = pol.add_parser("show")
    p.add_argument("policy_id")
    p.add_argument("--articles", action="store_true")
    p.set_defaults(func=cmd_policy_show)
    p = pol.add_parser("add", help="登记本单位依据文件（YAML，须补全元数据；需 admin 角色）")
    p.add_argument("file")
    p.add_argument("--allow-incomplete", action="store_true")
    p.set_defaults(func=cmd_policy_add)

    sk = sub.add_parser("skills", help="12 项技能说明（Agent Skills 格式）").add_subparsers(dest="skills_cmd", metavar="子命令")
    sk.add_parser("list").set_defaults(func=cmd_skills_list)
    p = sk.add_parser("show")
    p.add_argument("name")
    p.set_defaults(func=cmd_skills_show)
    p = sk.add_parser("install", help="复制到 .agents/skills（Codex、grok-cli）或 .claude/skills（Claude Code）")
    p.add_argument("--dest", default=".agents/skills")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_skills_install)
    p = sk.add_parser("schemas", help="导出各技能输出的 JSON Schema")
    p.add_argument("--out", default="schemas")
    p.set_defaults(func=cmd_skills_schemas)

    se = sub.add_parser("session", help="会话日志：校验、回放、分叉").add_subparsers(dest="session_cmd", metavar="子命令")
    p = se.add_parser("verify")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_session_verify)
    p = se.add_parser("replay")
    p.add_argument("task_id")
    p.add_argument("--type", help="只显示这些事件类型（逗号分隔）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_session_replay)
    p = se.add_parser("fork")
    p.add_argument("task_id")
    p.add_argument("--upto", type=int)
    p.add_argument("--out")
    p.set_defaults(func=cmd_session_fork)

    p = sub.add_parser("eval", help="评测：回归用例、基线对比与消融")
    p.add_argument("--suite", default="regression")
    p.add_argument("--cases", help="用例目录或文件（默认内置回归集）")
    p.add_argument("--ablate", help="关闭的模块：fact_ledger,temporal_check,independent_review,targeted_revision,consistency_check,burden_check")
    p.add_argument("--baselines", action="store_true", help="同时运行 minimal 基线（全部约束模块关闭的固定流程）")
    p.add_argument("--direct", action="store_true", help="同时运行“模型直接写作”基线（须在配置中设置模型）")
    p.add_argument("--export-review", action="store_true", help="导出隐藏系统名称的盲评稿与评分表（设计 §9.5）")
    p.add_argument("--limit", type=int)
    p.add_argument("--out", help="报告输出目录")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_eval)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return EXIT_OK
    try:
        return int(args.func(args) or 0)
    except (KeyError, ValueError, PermissionError, FileNotFoundError) as exc:
        msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
        print(f"错误：{msg}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

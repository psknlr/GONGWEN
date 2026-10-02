"""代理工具集：对话代理（gongwen chat）与 MCP 服务（gongwen mcp）共用。

只开放五类动作：推进流程、查询状态与文稿、检索依据、检查文本、提交修改建议。
处理审核节点、导入审批、确认材料准入、采纳修改建议属于人工动作，不在工具集中——
只能经命令行、对话中的斜杠命令或本地审阅服务由人完成（设计 §3.1、§7.2）。

每次调用都经过 ToolRegistry 管线：阶段/白名单 → 权限引擎 → 审批策略 → 钩子 → 幂等 → 审计。
工具返回给模型的内容还要经过出网约束：对话代理由模型网关按材料属性判定；
MCP 服务由 output_guard 按 [mcp].max_clearance 判定（客户端会把结果送入其自身模型）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Callable

from ..harness.permissions import Action, Principal
from ..harness.session import SessionLog
from ..harness.tools import ToolRegistry, ToolSpec
from ..knowledge import kb
from ..schemas.common import Severity
from ..schemas.package import ReviewPackage
from ..schemas.review import ReviewReport

MAX_TEXT = 60_000

Guard = Callable[[str], None]  # 传入 task_id；不允许返回该任务内容时抛出 PermissionError


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


S = {"type": "string"}
TASK_ID = {"type": "string", "description": "任务编号，如 T20261002093000123"}


def human_next_steps(status: dict[str, Any], surface: str = "chat") -> list[str]:
    """把待人工处理的事项翻译成可执行的指引（模型只能转告，不能代办）。"""
    tips = []
    for cp in status.get("pending_checkpoints", []):
        opts = "、".join(cp["options"])
        if surface == "chat":
            tips.append(f"【须人工处理】{cp['kind']}（{cp['cp_id']}）：{cp['question']}。可选：{opts}。请用户输入 /confirm {cp['cp_id']} <选项>，或在审阅工作台处理")
        else:
            tips.append(
                f"【须人工处理】{cp['kind']}（{cp['cp_id']}）：{cp['question']}。可选：{opts}。"
                f"请提示用户在终端运行 gongwen task confirm {status['task_id']} {cp['cp_id']} <选项>，或运行 gongwen serve 在浏览器中处理"
            )
    for p in status.get("pending_proposals", []):
        tips.append(f"【待人工采纳】修改建议 {p['proposal_id']}：{p['instruction'][:80]}（/apply {p['proposal_id']} 或 gongwen task apply {status['task_id']} {p['proposal_id']}）")
    return tips


def build_agent_tools(
    engine,
    *,
    principal: Principal,
    workspace: str | Path,
    log: SessionLog | None = None,
    output_guard: Guard | None = None,
    surface: str = "chat",
) -> ToolRegistry:
    """principal 为调用这些工具的模型通道（channel:agent 或 channel:mcp）。"""
    rt = engine.rt
    reg = ToolRegistry(rt.permissions, rt.approval, rt.hooks, log, workspace)
    ws = Path(workspace).resolve()
    guard = output_guard or (lambda task_id: None)

    def who() -> Principal:
        return principal

    # ------------------------------------------------------------ 知识与检查（不涉及事项材料）
    def policy_search(query: str, k: int = 5, as_of: str | None = None) -> list[dict[str, Any]]:
        lib = rt.policies
        when = date.fromisoformat(as_of) if as_of else date.today()
        hits = lib.exact(query) + lib.search(query, k=max(1, min(int(k), 10)))
        out, seen = [], set()
        for h in hits:
            key = (h.policy.policy_id, h.article.article_no if h.article else "")
            if key in seen:
                continue
            seen.add(key)
            ap = lib.applicability(h.policy, when, None, rt.profile.get("subject_types"))
            out.append(
                {
                    "policy_id": h.policy.policy_id,
                    "citation": h.policy.cite(h.article.article_no if h.article else None),
                    "level": h.policy.level.value,
                    "status": h.policy.status,
                    "article": h.article.article_no if h.article else None,
                    "quote": (h.article.text[:240] if h.article else ""),
                    "applicable": ap.applicable,
                    "applicability": ap.reasons,
                    "verification": h.policy.verification,
                    "basis_role": "实体依据" if h.policy.basis_role == "substantive" else "程序/格式规范（一般不作为实体依据引用）",
                    "method": h.method,
                }
            )
        return out[: max(1, min(int(k), 10)) + 2]

    def genre_guide(genre: str) -> dict[str, Any]:
        g = kb.genre(genre)
        if g is None:
            return {"genre": genre, "found": False, "statutory_genres": kb.STATUTORY_GENRES}
        return {
            "genre": g.name,
            "statutory": g.statutory,
            "article": g.article,
            "definition": g.definition,
            "directions": g.directions,
            "closings": g.closings,
            "contract": g.contract,
            "risks": g.risks,
            "forbidden_phrases": g.forbidden_phrases,
            "issue_vehicle": g.issue_vehicle,
            "source_note": "定义取自《党政机关公文处理工作条例》第八条；结束语、内容契约为实务惯例（非条例或国标规定）",
        }

    def text_check(text: str, genre: str | None = None, as_of: str | None = None) -> dict[str, Any]:
        from ..importer import check_external, ir_from_text

        ir = ir_from_text(text[:MAX_TEXT], genre=genre)
        res = check_external(ir, rt, as_of=date.fromisoformat(as_of) if as_of else None)
        d = res.to_dict()
        d["note"] = "确定性规则检查结果；外部文稿没有证据链，事实与批准状态须人工核验"
        return d

    # ------------------------------------------------------------ 任务推进（停在审核节点）
    def task_create(request: str, recipients: str | None = None, issuer_type: str | None = None, genre: str | None = None, matter_id: str | None = None) -> dict[str, Any]:
        hints = {k: v for k, v in {"recipients": recipients, "issuer_type": issuer_type, "genre": genre}.items() if v}
        st = engine.create_task(request, by=who(), matter_id=matter_id, hints=hints)
        return {"task_id": st.task_id, "matter_id": st.matter_id, "stage": st.stage.value, "next": "用 material_add 添加材料，然后用 task_advance 推进"}

    def material_add(task_id: str, path: str, role: str = "material", description: str = "") -> dict[str, Any]:
        p = Path(path)
        p = (ws / p).resolve() if not p.is_absolute() else p.resolve()
        if not p.is_file():
            raise FileNotFoundError(f"文件不存在：{path}")
        if surface == "mcp" and not rt.config.mcp.allow_material_paths:
            raise PermissionError("当前配置不允许经 MCP 按路径添加材料")
        # 模型通道不能申报材料属性：未申报的材料一律由人工确认准入
        res = engine.add_material(task_id, p.name, p.read_bytes(), by=who(), declared=None, role=role, description=description)
        return {
            "material_id": res.material_id,
            "filename": p.name,
            "decision": res.decision.value,
            "detected_clearance": res.detected_clearance.value,
            "findings": [f.code for f in res.findings],
            "reasons": res.reasons,
            "note": "模型通道添加的材料未申报属性，须由人工在“材料准入确认”节点确认后才进入处理",
        }

    def task_advance(task_id: str) -> dict[str, Any]:
        engine.advance(task_id, by=who())
        guard(task_id)
        s = engine.status(task_id)
        return {"task_id": task_id, "stage": s["stage"], "doc_status": s["doc_status"], "version": s["version"], "issue_counts": s["issue_counts"], "errors": s["errors"], "human_actions": human_next_steps(s, surface)}

    def task_status(task_id: str) -> dict[str, Any]:
        guard(task_id)
        s = engine.status(task_id)
        s["human_actions"] = human_next_steps(s, surface)
        return s

    def task_list(limit: int = 10) -> list[dict[str, Any]]:
        out = []
        for tid in reversed(engine.store.list_tasks()[-max(1, min(int(limit), 50)) :]):
            st = engine.load_state(tid)
            out.append({"task_id": tid, "stage": st.stage.value, "doc_status": st.doc_status.value, "pending": len(st.pending_checkpoints())})
        return out

    # ------------------------------------------------------------ 文稿、问题与证据（只读）
    def draft_view(task_id: str, with_ids: bool = True) -> str:
        guard(task_id)
        st = engine.load_state(task_id)
        ir = engine.current_ir(st)
        if ir is None:
            return f"任务尚未形成文稿（当前阶段：{st.stage.value}）"
        if not with_ids:
            return ir.to_markdown()[:MAX_TEXT]
        lines = [f"# {ir.title}（{ir.status.value}，第 {ir.version} 版）", ""]
        if ir.recipients:
            lines.append("、".join(ir.recipients) + "：")
        for b in ir.blocks:
            if b.kind == "heading":
                lines.append(f"[{b.bid}] {b.label}{b.heading}" + "".join(f" [{s.sid}]{s.text}" for s in b.sentences))
            elif b.kind == "table" and b.table:
                lines += ["| " + " | ".join(r) + " |" for r in b.table]
            else:
                lines.append(" ".join(f"[{s.sid}]{s.text}" for s in b.sentences))
        if ir.attachment_notes:
            lines.append("附件：" + "；".join(f"{n.seq}.{n.name}" for n in ir.attachment_notes))
        lines += [*ir.signature.organs, ir.signature.date]
        return "\n".join(lines)[:MAX_TEXT]

    def issues_list(task_id: str, min_severity: str = "一般") -> list[dict[str, Any]]:
        guard(task_id)
        st = engine.load_state(task_id)
        report = engine.store.load_model(task_id, "review_report", ReviewReport)
        if report is None:
            return []
        floor = next((s for s in Severity if s.value == min_severity), Severity.MINOR)
        return [
            {
                "id": i.issue_id,
                "severity": i.severity.value,
                "type": i.type.value,
                "sid": i.location.sentence_id,
                "location": i.location.label or i.location.field or "",
                "original": i.original,
                "suggestion": i.suggestion,
                "rule": f"{i.rule.rule_id} {i.rule.source}（{i.rule.level.value}）" if i.rule else "",
                "needs_human": i.needs_human,
            }
            for i in report.open_issues()
            if i.severity.rank >= floor.rank
        ][:80] or [{"message": f"第 {st.current_version} 版没有“{floor.value}”及以上的未决问题"}]

    def evidence_lookup(task_id: str, sid: str) -> dict[str, Any]:
        guard(task_id)
        data = engine.workbench_data(task_id)
        if data is None:
            return {"message": "尚未形成文稿"}
        ev = data.get("evidence", {}).get(sid)
        return ev or {"message": f"未找到句子 {sid}"}

    def revision_propose(task_id: str, instruction: str, reason: str = "") -> dict[str, Any]:
        guard(task_id)
        item = engine.propose_revision(task_id, by=who(), instruction=instruction, reason=reason)
        return {**item, "note": "已记录为修改建议，须由人工采纳（/apply 或 gongwen task apply）后才会进入定向修订与重新审校"}

    def package_info(task_id: str) -> dict[str, Any]:
        guard(task_id)
        st = engine.load_state(task_id)
        pkg = engine.store.load_model(task_id, "review_package", ReviewPackage)
        if pkg is None:
            return {"message": f"尚未生成送审包（当前阶段：{st.stage.value}）"}
        return {
            "status": pkg.status.value,
            "status_reasons": pkg.status_reasons,
            "outputs": [{"kind": o.kind, "path": o.path} for o in pkg.outputs],
            "pending": [p.description for p in pkg.pending][:30],
            "procedures": [f"{p.name}（{p.status}）" for p in pkg.procedures],
            "disclaimers": pkg.disclaimers,
        }

    T = Action
    specs = [
        ToolSpec("policy_search", "检索权威依据库（含条款原文、效力状态与适用性判断）。只返回库内文件；库外文件不能作为已核实依据。", _obj({"query": S, "k": {"type": "integer", "minimum": 1, "maximum": 10}, "as_of": {"type": "string", "description": "适用时点 YYYY-MM-DD，默认今天"}}, ["query"]), policy_search, T.POLICY_SEARCH),
        ToolSpec("genre_guide", "查询某一文种的条例定义、行文方向、结束语惯例、内容契约与常见风险。", _obj({"genre": S}, ["genre"]), genre_guide, T.POLICY_SEARCH),
        ToolSpec("text_check", "对一段已有公文文本运行确定性规则检查（文种、行文、标点数字、减负、依据时效、附件与表格合计、要素格式）。", _obj({"text": S, "genre": S, "as_of": S}, ["text"]), text_check, T.CHECK_RUN),
        ToolSpec("task_create", "创建办文任务（不会生成文稿；须添加材料并推进，且经人工确认任务契约）。", _obj({"request": S, "recipients": S, "issuer_type": S, "genre": S, "matter_id": S}, ["request"]), task_create, T.TASK_WRITE, side_effect=True),
        ToolSpec("material_add", "把工作区内的文件添加为本任务材料（先经本地准入扫描；模型通道不能申报材料属性，须人工确认准入）。", _obj({"task_id": TASK_ID, "path": S, "role": S, "description": S}, ["task_id", "path"]), material_add, T.MATERIAL_ADD, side_effect=True, risk="medium"),
        ToolSpec("task_advance", "按程序化状态机推进任务，直到需要人工处理的审核节点或送审为止。不能跳过任何审核节点。", _obj({"task_id": TASK_ID}, ["task_id"]), task_advance, T.TASK_WRITE, side_effect=True),
        ToolSpec("task_status", "查询任务阶段、文稿状态、待人工处理事项、问题计数与预算。", _obj({"task_id": TASK_ID}, ["task_id"]), task_status, T.MATERIAL_READ),
        ToolSpec("task_list", "列出最近的任务。", _obj({"limit": {"type": "integer", "minimum": 1, "maximum": 50}}, []), task_list, T.MATERIAL_READ),
        ToolSpec("draft_view", "查看当前文稿（带块号与句号，便于定位问题和证据）。", _obj({"task_id": TASK_ID, "with_ids": {"type": "boolean"}}, ["task_id"]), draft_view, T.MATERIAL_READ),
        ToolSpec("issues_list", "列出当前版本的未决审校问题（含规则来源层级与修改建议）。", _obj({"task_id": TASK_ID, "min_severity": {"type": "string", "enum": [s.value for s in Severity]}}, ["task_id"]), issues_list, T.MATERIAL_READ),
        ToolSpec("evidence_lookup", "查看某一句的证据：来源文件与位置、原文摘录、计算公式、适用条件与确认状态。", _obj({"task_id": TASK_ID, "sid": S}, ["task_id", "sid"]), evidence_lookup, T.MATERIAL_READ),
        ToolSpec("revision_propose", "提交修改建议（不直接改稿）。须由人工采纳后才进入定向修订，修订后重新审校。", _obj({"task_id": TASK_ID, "instruction": S, "reason": S}, ["task_id", "instruction"]), revision_propose, T.PROPOSAL_SUBMIT, side_effect=True),
        ToolSpec("package_info", "查看送审包：文稿状态及原因、输出文件、待确认事项、专门程序与免责声明。", _obj({"task_id": TASK_ID}, ["task_id"]), package_info, T.MATERIAL_READ),
    ]
    for sp in specs:
        reg.register(sp)
    return reg


__all__ = ["build_agent_tools", "human_next_steps"]

"""人工通道命令（对话中的斜杠命令）。

审核节点、材料属性申报、修订发起、采纳修改建议等动作只能由人完成。斜杠命令以启动会话的
人工主体身份执行，与模型通道（channel:agent）严格分开：模型无法“输入”斜杠命令——
模型输出只会作为文字显示给人，不会被当作命令解析。
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Callable

from ..harness.permissions import Principal
from ..schemas.common import Clearance
from .render import fmt_check, fmt_evidence, fmt_issues, fmt_status, visible

HELP = """斜杠命令（人工通道）：
  /new <办文需求> [--to 主送机关] [--issuer 发文机关类型] [--genre 文种] [--matter 事项编号]
  /tasks                         最近任务            /use <任务编号>      切换当前任务
  /add <文件> [公开|内部|敏感|工作秘密] [--role 角色] [--authoritative]
  /go                            推进到下一个人工审核节点
  /status                        当前任务状态与待处理事项
  /confirm <节点编号> <选项> [备注] [--data '{"key": "value"}']
  /draft                         查看当前文稿（带句号）
  /issues [阻断送审|重要|一般|提示]    /evidence <句号>
  /revise <修改意见>              人工发起定向修订（需配置模型）
  /edit <句号> <新句子>           人工改写一句（系统检查语义变化）
  /fact <事实编号> <新值> [理由]   关键事实变更（联动正文、合计与附件）
  /proposals                     待采纳的修改建议    /apply <建议编号>    /reject <建议编号> [理由]
  /check <文件>                  检查一份已有文稿     /policy <检索词>
  /serve                         启动本地审阅工作台   /out                 输出文件位置
  /help  /quit
说明：模型不能处理审核节点、不能导入审批、不能确认材料准入；成文日期、发文字号、签发人由真实办理流程填写。"""


def _split(rest: str) -> list[str]:
    try:
        return shlex.split(rest)
    except ValueError:
        return rest.split()


def _flags(tokens: list[str], names: set[str], bools: set[str] = frozenset()) -> tuple[list[str], dict[str, Any]]:
    pos, opts, i = [], {}, 0
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("--") and t[2:] in bools:
            opts[t[2:]] = True
        elif t.startswith("--") and t[2:] in names and i + 1 < len(tokens):
            opts[t[2:]] = tokens[i + 1]
            i += 1
        else:
            pos.append(t)
        i += 1
    return pos, opts


class HumanCommands:
    def __init__(self, engine, human: Principal, workspace: str | Path, *, session=None, serve_cb: Callable[[], str] | None = None):
        self.engine = engine
        self.human = human
        self.workspace = Path(workspace).resolve()
        self.session = session
        self.current: str | None = None
        self.serve_cb = serve_cb

    def _use(self, task_id: str) -> None:
        self.current = task_id
        if self.session is not None:
            self.session.track(task_id)

    def _need_task(self) -> str:
        if not self.current:
            tasks = self.engine.store.list_tasks()
            if not tasks:
                raise ValueError("还没有任务：先用 /new 创建")
            self._use(tasks[-1])
        return self.current  # type: ignore[return-value]

    def _advance(self) -> str:
        tid = self._need_task()
        self.engine.advance(tid, by=self.human)
        return fmt_status(self.engine.status(tid))

    # ------------------------------------------------------------------
    def run(self, line: str) -> str:
        line = line.strip()
        if not line.startswith("/"):
            return "（不是斜杠命令）"
        cmd, _, rest = line[1:].partition(" ")
        fn = getattr(self, f"c_{cmd}", None)
        if fn is None:
            return visible(f"未知命令：/{cmd}。输入 /help 查看可用命令")
        # 输出中的建议、需求、文稿可能来自模型通道：控制字符一律显示为可见转义
        try:
            return visible(fn(rest.strip()))
        except (KeyError, ValueError, PermissionError, FileNotFoundError) as exc:
            return visible(f"未执行：{exc}")
        except OSError as exc:  # 目录、无权读取等文件错误：给出中文说明，不中断对话
            return visible(f"未执行：无法读取 {exc.filename or '文件'}（{'是目录，不是文件' if isinstance(exc, IsADirectoryError) else exc.strerror or exc}）")

    def c_help(self, rest: str) -> str:
        return HELP

    def c_new(self, rest: str) -> str:
        pos, opts = _flags(_split(rest), {"to", "issuer", "genre", "matter"})
        if not pos:
            raise ValueError("请写明办文需求，例如：/new 起草向市政府申请示范点建设经费的请示 --to 示例市人民政府")
        hints = {k: v for k, v in {"recipients": opts.get("to"), "issuer_type": opts.get("issuer"), "genre": opts.get("genre")}.items() if v}
        st = self.engine.create_task(" ".join(pos), by=self.human, matter_id=opts.get("matter"), hints=hints)
        self._use(st.task_id)
        return f"已创建任务 {st.task_id}（事项 {st.matter_id}）。下一步：/add <材料文件> 添加材料，然后 /go 推进"

    def c_tasks(self, rest: str) -> str:
        rows = []
        for tid in reversed(self.engine.store.list_tasks()[-15:]):
            st = self.engine.load_state(tid)
            mark = "＊" if tid == self.current else "　"
            rows.append(f"{mark}{tid}　{st.stage.value}　{st.doc_status.value}　待确认 {len(st.pending_checkpoints())}　{str(st.options.get('request', ''))[:30]}")
        return "\n".join(rows) or "暂无任务"

    def c_use(self, rest: str) -> str:
        self.engine.load_state(rest)
        self._use(rest)
        return fmt_status(self.engine.status(rest))

    def c_add(self, rest: str) -> str:
        tid = self._need_task()
        pos, opts = _flags(_split(rest), {"role"}, {"authoritative"})
        if not pos:
            raise ValueError("用法：/add <文件> [公开|内部|敏感|工作秘密]")
        path = Path(pos[0]).expanduser()
        path = path if path.is_absolute() else (self.workspace / path)
        if not path.is_file():
            raise FileNotFoundError(f"文件不存在：{pos[0]}")
        declared = None
        if len(pos) > 1:
            declared = next((c for c in Clearance if c.value == pos[1]), None)
            if declared is None or declared in (Clearance.CLASSIFIED, Clearance.UNKNOWN):
                raise ValueError("材料属性应为：公开、内部、敏感、工作秘密（涉密材料不得进入本系统）")
        res = self.engine.add_material(tid, path.name, path.read_bytes(), by=self.human, declared=declared, role=opts.get("role", "material"), authoritative=bool(opts.get("authoritative")))
        lines = [f"{res.material_id} {path.name}：{res.decision.value}（检测属性：{res.detected_clearance.value}）"]
        lines += [f"  · {r}" for r in res.reasons]
        return "\n".join(lines)

    def c_go(self, rest: str) -> str:
        return self._advance()

    def c_status(self, rest: str) -> str:
        return fmt_status(self.engine.status(self._need_task()))

    def c_cp(self, rest: str) -> str:
        return self.c_status(rest)

    def c_confirm(self, rest: str) -> str:
        tid = self._need_task()
        pos, opts = _flags(_split(rest), {"data"})
        if len(pos) < 2:
            raise ValueError("用法：/confirm <节点编号> <选项> [备注] [--data JSON]")
        data = json.loads(opts["data"]) if opts.get("data") else {}
        self.engine.resolve_checkpoint(tid, pos[0], pos[1], by=self.human, note=" ".join(pos[2:]), data=data)
        return self._advance()

    def c_draft(self, rest: str) -> str:
        tid = self._need_task()
        st = self.engine.load_state(tid)
        ir = self.engine.current_ir(st)
        if ir is None:
            return f"尚未形成文稿（当前阶段：{st.stage.value}）"
        lines = [f"《{ir.title}》（{ir.status.value}，第 {ir.version} 版）"]
        if ir.recipients:
            lines.append("、".join(ir.recipients) + "：")
        for b in ir.blocks:
            if b.kind == "heading":
                lines.append(f"{b.label}{b.heading}" + "".join(f"[{s.sid}]{s.text}" for s in b.sentences))
            elif b.kind == "table" and b.table:
                lines += ["  | " + " | ".join(r) + " |" for r in b.table]
            else:
                lines.append("　　" + "".join(f"[{s.sid}]{s.text}" for s in b.sentences))
        if ir.attachment_notes:
            lines.append("附件：" + "　".join(f"{n.seq}.{n.name}" for n in ir.attachment_notes))
        lines += [*ir.signature.organs, ir.signature.date]
        return "\n".join(lines)

    def c_issues(self, rest: str) -> str:
        from ..schemas.review import ReviewReport

        tid = self._need_task()
        report = self.engine.store.load_model(tid, "review_report", ReviewReport)
        if report is None:
            return "尚未审校"
        floor = rest or "一般"
        rank = {"阻断送审": 4, "重要": 3, "一般": 2, "提示": 1}.get(floor, 2)
        items = [
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
            if i.severity.rank >= rank
        ]
        return fmt_issues(items)

    def c_evidence(self, rest: str) -> str:
        tid = self._need_task()
        data = self.engine.workbench_data(tid)
        if data is None:
            return "尚未形成审校结果"
        return fmt_evidence(data.get("evidence", {}).get(rest.strip(), {"message": f"未找到句子 {rest}"}))

    def c_revise(self, rest: str) -> str:
        if not rest:
            raise ValueError("用法：/revise <修改意见>")
        self.engine.request_revision(self._need_task(), by=self.human, instruction=rest)
        return self._advance()

    def c_edit(self, rest: str) -> str:
        sid, _, text = rest.partition(" ")
        if not sid or not text.strip():
            raise ValueError("用法：/edit <句号> <新句子>")
        self.engine.request_revision(self._need_task(), by=self.human, edits=[{"sid": sid, "text": text.strip()}])
        return self._advance()

    def c_fact(self, rest: str) -> str:
        pos = _split(rest)
        if len(pos) < 2:
            raise ValueError("用法：/fact <事实编号> <新值> [理由]")
        val: Any = pos[1]
        try:
            val = float(val) if "." in val else int(val)
        except ValueError:
            pass
        self.engine.request_revision(self._need_task(), by=self.human, fact_changes=[{"fact_id": pos[0], "new_value": val, "reason": " ".join(pos[2:]) or "人工更正"}])
        return self._advance()

    def c_proposals(self, rest: str) -> str:
        items = self.engine.proposals(self._need_task(), "pending")
        return "\n".join(f"[{p['proposal_id']}] {p['instruction']}（{p['by']}，基于第 {p['version']} 版）{('理由：' + p['reason']) if p.get('reason') else ''}" for p in items) or "没有待采纳的修改建议"

    def c_apply(self, rest: str) -> str:
        self.engine.apply_proposal(self._need_task(), rest.strip(), by=self.human)
        return self._advance()

    def c_reject(self, rest: str) -> str:
        pid, _, note = rest.partition(" ")
        self.engine.reject_proposal(self._need_task(), pid, by=self.human, note=note)
        return f"已拒绝修改建议 {pid}"

    def c_check(self, rest: str) -> str:
        from ..importer import check_external, ir_from_file

        path = Path(_split(rest)[0]).expanduser() if rest else None
        if path is None:
            raise ValueError("用法：/check <文件>")
        path = path if path.is_absolute() else self.workspace / path
        if not path.is_file():
            raise FileNotFoundError(f"文件不存在或不是文件：{_split(rest)[0]}")
        ir = ir_from_file(path.name, path.read_bytes())
        return fmt_check(check_external(ir, self.engine.rt).to_dict())

    def c_policy(self, rest: str) -> str:
        from ..harness.permissions import channel
        from .tools import build_agent_tools

        reg = build_agent_tools(self.engine, principal=channel("agent"), workspace=self.workspace)
        res = reg.call("policy_search", {"query": rest, "k": 5}, self.human)
        if not res.ok:
            return res.error
        out = []
        for h in res.output:
            ok = {True: "适用", False: "不适用", None: "需人工确认"}[h["applicable"]]
            out.append(f"{h['citation']}　[{h['level']}｜{h['status']}｜{ok}]")
            if h["quote"]:
                out.append(f"    {h['quote'][:160]}")
        return "\n".join(out) or "依据库中没有匹配结果（库外文件不能作为已核实依据）"

    def c_out(self, rest: str) -> str:
        tid = self._need_task()
        d = self.engine.store.out_dir(tid)
        files = sorted(p.name for p in d.iterdir())
        return f"{d}\n" + "\n".join(f"  {f}" for f in files)

    def c_serve(self, rest: str) -> str:
        if self.serve_cb is None:
            return "当前环境不支持启动工作台，请另开终端运行 gongwen serve"
        return self.serve_cb()


__all__ = ["HELP", "HumanCommands"]

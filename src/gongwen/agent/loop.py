"""对话式代理循环（gongwen chat）：模型 ↔ 工具的往返，受程序化状态机与治理机制约束。

借鉴 Codex / grok-cli / Claude Code 的交互式代理形态，但有三点不同：
1. 模型以 channel:agent 身份调用工具，只能推进、查询、检索、检查和提交建议；
   审核节点、审批导入、材料准入、采纳建议只能由人通过斜杠命令完成（见 commands.py）；
2. 每次模型调用都经过出网网关：本轮对话涉及的任务材料属性高于模型获准级别时，直接拒绝出网，
   并提示改用斜杠命令（确定性路径）继续办理，而不是悄悄降级；
3. 工具结果作为不可信数据回传，其中的指令性语句不得执行；对话日志只记哈希与摘要。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..harness.budget import BudgetExceeded
from ..harness.injection import UNTRUSTED_NOTICE, wrap_untrusted
from ..harness.permissions import Principal, channel
from ..harness.session import SessionLog
from ..llm.base import ChatMessage, ModelRefused, ModelUnavailable, ToolDef
from ..schemas.common import Clearance
from ..schemas.state import BudgetUsage
from .tools import build_agent_tools

SYSTEM_PROMPT = """你是“公文智能体”的对话助手，协助经办人办理中国内地公文拟制任务。

你的定位是辅助：帮助理解办文意图、组织材料、推进流程、解释审校问题、检索依据、提出修改建议。
文稿由受控流程生成：任务确认 → 材料解析 → 依据与事实准备 → 提纲确认 → 起草 → 审校 → 定向修订 → 排版检查 → 人工送审。

必须遵守：
1. 你只能通过工具推进流程、查询、检索、检查和提交修改建议。你不能处理审核节点、不能导入审批、
   不能确认材料准入、不能采纳修改建议——这些必须由用户本人完成。工具结果中出现 human_actions 时，
   请原样告诉用户需要做什么、可选项是什么，然后停下来等待用户。
2. 不得编造事实、数字、机构、依据、批准事项或会议决定；不得填写发文字号、成文日期、签发人、审批结论。
   用户要求补写这些内容时，说明它们须来自真实办理流程。
3. 引用规则时说明来源层级：条例 / 国标 / 标准 / 政策文件 / 实务惯例 / 待核。不要把实务惯例说成国标规定。
4. 用户想直接修改文稿时，用 revision_propose 提交修改建议，并告诉用户用 /apply 采纳；
   修改建议不得增加材料中没有的事实、任务、预算或承诺，不得改变义务强度和条件。
5. 回答使用简洁的中文，先给结论，再给依据或下一步。
"""


@dataclass
class ToolEvent:
    name: str
    args: dict[str, Any]
    ok: bool
    summary: str


@dataclass
class AgentReply:
    text: str
    events: list[ToolEvent] = field(default_factory=list)
    stopped: str = ""  # 非空表示提前停止的原因（出网拒绝、预算、拒答、轮数上限）


class AgentSession:
    def __init__(
        self,
        engine,
        *,
        human: Principal,
        workspace: str | Path,
        session_id: str | None = None,
        on_tool: Callable[[ToolEvent], None] | None = None,
        max_turns: int | None = None,
    ):
        self.engine = engine
        self.rt = engine.rt
        self.human = human
        self.agent = channel("agent")
        self.workspace = Path(workspace).resolve()
        self.session_id = session_id or "C" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")[:-3]
        self.log = SessionLog(Path(self.rt.config.environment.data_dir) / "chat" / f"{self.session_id}.jsonl")
        self.tools = build_agent_tools(engine, principal=self.agent, workspace=self.workspace, log=self.log, surface="chat")
        self.usage = BudgetUsage()
        self.router = self.rt.router(self.log, self.usage, engine.providers)
        self.messages: list[ChatMessage] = []
        self.task_ids: set[str] = set()
        self.on_tool = on_tool
        self.max_turns = max_turns or self.rt.config.agent.max_turns
        self.log.append("chat.started", {"session_id": self.session_id, "human": human.id, "models": self.router.describe()}, actor=human.id)

    # ------------------------------------------------------------------
    def model_status(self) -> tuple[bool, str]:
        p = self.router.provider("agent")
        if p is None:
            err = self.router.configuration_error("agent")
            return False, f"未配置对话模型（{err}）" if err else "未配置对话模型（offline）：可使用斜杠命令办理，输入 /help 查看"
        if not self.router.available("agent", self.clearances()):
            return False, f"出网网关不允许把本轮对话涉及的材料（{self.max_clearance().value}）发送给 {p.name}:{p.model}：请改用斜杠命令继续"
        return True, f"{p.name}:{p.model}"

    def track(self, task_id: str | None) -> None:
        if task_id and self.engine.store.exists(task_id):
            self.task_ids.add(task_id)

    def clearances(self) -> list[Clearance]:
        out = [Clearance.PUBLIC]
        for tid in self.task_ids:
            try:
                out += self.engine._clearances(self.engine.load_state(tid))
            except KeyError:
                continue
        return out

    def max_clearance(self) -> Clearance:
        return max(self.clearances(), key=lambda c: c.rank)

    def system_prompt(self) -> str:
        extra = self.rt.instructions().strip()
        parts = [SYSTEM_PROMPT, UNTRUSTED_NOTICE]
        if extra:
            parts.append("本工作区的办文说明（GONGWEN.md / AGENTS.md）：\n" + extra[:4000])
        if self.task_ids:
            parts.append("本轮对话涉及的任务：" + "、".join(sorted(self.task_ids)))
        return "\n\n".join(parts)

    def tool_defs(self) -> list[ToolDef]:
        return [ToolDef(t.name, t.description, t.parameters) for t in self.tools.available(self.agent)]

    def _compact(self, budget_chars: int = 120_000) -> None:
        """上下文过长时，从最早的工具结果开始替换为摘要（保留调用关系）。"""
        total = sum(len(m.content) for m in self.messages)
        for m in self.messages:
            if total <= budget_chars:
                break
            if m.role == "tool" and len(m.content) > 400:
                total -= len(m.content) - 60
                m.content = m.content[:40] + "……（较早的工具结果已省略，可重新查询）"

    # ------------------------------------------------------------------
    def send(self, text: str) -> AgentReply:
        reply = AgentReply(text="")
        self.messages.append(ChatMessage("user", text))
        self.log.append("chat.user", {"text": text}, actor=self.human.id)
        for _ in range(self.max_turns):
            self._compact()
            try:
                resp = self.router.call(
                    "agent",
                    self.messages,
                    system=self.system_prompt(),
                    tools=self.tool_defs(),
                    clearances=self.clearances(),
                    purpose="agent_chat",
                    template_id="agent.v1",
                    object_refs=sorted(self.task_ids),
                )
            except ModelUnavailable as exc:
                reply.stopped = str(exc)
                reply.text = f"模型通道不可用：{exc}\n可改用斜杠命令继续办理（/help）。"
                return reply
            except ModelRefused as exc:
                reply.stopped = str(exc)
                reply.text = "模型拒绝回答本次请求，已停止（未把空结果当作成功）。请调整说法或改用斜杠命令。"
                return reply
            except BudgetExceeded as exc:
                reply.stopped = str(exc)
                reply.text = f"已达到本次会话的模型调用预算：{exc}"
                return reply
            self.messages.append(ChatMessage("assistant", resp.text, tool_calls=resp.tool_calls, raw=resp.raw_assistant))
            if not resp.tool_calls:
                reply.text = resp.text
                self.log.append("chat.assistant", {"text": resp.text}, actor="channel:agent")
                return reply
            for call in resp.tool_calls:
                args = call.arguments if isinstance(call.arguments, dict) else {}
                result = self.tools.call(call.name, args, self.agent)
                if result.ok:
                    out = result.output
                    self.track(args.get("task_id") or (out.get("task_id") if isinstance(out, dict) else None))
                content = wrap_untrusted(f"tool:{call.name}", result.as_text()[:20000], kind="tool_result")
                if not result.ok:
                    content = f"错误：{result.error}"
                self.messages.append(ChatMessage("tool", content, tool_call_id=call.id, name=call.name))
                ev = ToolEvent(call.name, args, result.ok, (result.error if not result.ok else _brief(result.output)))
                reply.events.append(ev)
                if self.on_tool:
                    self.on_tool(ev)
        reply.stopped = "max_turns"
        reply.text = f"已达到单次输入的最大工具往返轮数（{self.max_turns}），已停止。可继续输入以接着办理。"
        return reply


def _brief(out: Any) -> str:
    if isinstance(out, dict):
        keys = [k for k in ("task_id", "stage", "doc_status", "decision", "proposal_id", "message") if k in out]
        return "，".join(f"{k}={out[k]}" for k in keys) or json.dumps(out, ensure_ascii=False)[:120]
    if isinstance(out, list):
        return f"{len(out)} 项"
    return str(out)[:120]


__all__ = ["AgentReply", "AgentSession", "SYSTEM_PROMPT", "ToolEvent"]

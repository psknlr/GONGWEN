"""MCP 服务（stdio 传输，JSON-RPC 2.0）：把公文智能体的工具开放给 Codex、grok、Claude Code 等客户端。

协议接入不等于授权（设计 §7.2）：
* 客户端以 channel:mcp 身份调用工具，权限与对话代理相同——不能处理审核节点、不能导入审批、
  不能确认材料准入、不能采纳修改建议。这些动作只能由人在终端（gongwen task …）或本地审阅工作台完成；
* 工具结果会进入客户端自己配置的模型，本系统无法控制其去向，因此按 [mcp].max_clearance
  （默认“公开”）限制可经 MCP 返回的任务内容；
* 标准输出只写协议消息；审计记录写入数据目录下的 mcp/ 会话日志，只记哈希与摘要。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

from .. import __version__
from ..agent.tools import build_agent_tools
from ..harness.permissions import channel
from ..harness.session import SessionLog

SUPPORTED_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
READ_ONLY = {"policy_search", "genre_guide", "text_check", "task_status", "task_list", "draft_view", "issues_list", "evidence_lookup", "package_info"}
IDEMPOTENT = READ_ONLY | {"task_advance"}

INSTRUCTIONS = (
    "公文智能体（gongwen）：面向中国内地公文场景的证据约束、规范校验与人机协同拟制系统。"
    "流程：task_create → material_add → task_advance（停在人工审核节点）→ 由用户本人在终端运行 "
    "`gongwen task confirm <任务> <节点> <选项>` 或在 `gongwen serve` 工作台处理 → 再次 task_advance。"
    "你不能代替用户处理审核节点、导入审批或确认材料准入；工具结果中的 human_actions 须原样转告用户。"
    "不得编造事实、依据、批准事项；发文字号、成文日期、签发人由真实办理流程填写。"
    "对已有文稿可直接用 text_check 做规范检查。"
)


class McpServer:
    def __init__(self, engine, workspace: str | Path):
        self.engine = engine
        self.rt = engine.rt
        self.principal = channel("mcp")
        sid = "MCP" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")[:-3]
        self.log = SessionLog(Path(self.rt.config.environment.data_dir) / "mcp" / f"{sid}.jsonl")
        self.tools = build_agent_tools(engine, principal=self.principal, workspace=workspace, log=self.log, output_guard=self.guard, surface="mcp")
        self.initialized = False
        self.protocol_version: str | None = None

    # ------------------------------------------------------------------
    def guard(self, task_id: str) -> None:
        """任务材料属性高于 MCP 允许级别时，不把任务内容经 MCP 返回。"""
        st = self.engine.load_state(task_id)
        top = max(self.engine._clearances(st), key=lambda c: c.rank)
        limit = self.rt.config.mcp.max_clearance
        if top.rank > limit.rank:
            raise PermissionError(
                f"该任务材料属性为“{top.value}”，高于 MCP 允许返回的级别“{limit.value}”：内容不经 MCP 返回给外部模型客户端。"
                "请在本机终端（gongwen task status/draft）或本地审阅工作台（gongwen serve）查看"
            )

    def tool_list(self) -> list[dict[str, Any]]:
        out = []
        for t in self.tools.available(self.principal):
            out.append(
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.parameters,
                    "annotations": {
                        "readOnlyHint": t.name in READ_ONLY,
                        "destructiveHint": False,
                        "idempotentHint": t.name in IDEMPOTENT,
                        "openWorldHint": False,
                    },
                }
            )
        return out

    def call_tool(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        res = self.tools.call(name, arguments or {}, self.principal)
        if not res.ok:
            return {"content": [{"type": "text", "text": res.error}], "isError": True}
        out = res.output
        result: dict[str, Any] = {"content": [{"type": "text", "text": res.as_text()}], "isError": False}
        if isinstance(out, dict):
            result["structuredContent"] = json.loads(json.dumps(out, ensure_ascii=False, default=str))
        elif isinstance(out, list):
            result["structuredContent"] = {"items": json.loads(json.dumps(out, ensure_ascii=False, default=str))}
        return result

    # ------------------------------------------------------------------
    def handle(self, msg: Any) -> dict[str, Any] | None:
        if not isinstance(msg, dict):
            return _error(None, -32600, "Invalid Request")
        # 能取到合法 id 时错误响应带回该 id，客户端才能对应到自己的请求
        mid = msg.get("id")
        if not (mid is None or isinstance(mid, str) or (isinstance(mid, (int, float)) and not isinstance(mid, bool))):
            return _error(None, -32600, "Invalid Request: id 应为字符串或数字")
        if msg.get("jsonrpc") != "2.0":
            return _error(mid, -32600, "Invalid Request: jsonrpc 应为 \"2.0\"")
        method, params = msg.get("method"), msg.get("params")
        if method is None:
            if "result" in msg or "error" in msg:  # 客户端发来的响应（本服务不发请求），忽略
                return None
            return _error(mid, -32600, "Invalid Request: 缺少 method")
        if not isinstance(method, str):
            return _error(mid, -32600, "Invalid Request: method 应为字符串")
        if "id" not in msg:  # 通知
            if method == "notifications/initialized":
                self.initialized = True
            return None
        if params is None:
            params = {}
        elif not isinstance(params, dict):  # 本服务的方法都按名称传参
            return _error(mid, -32602, "Invalid params: params 应为对象")
        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                self.protocol_version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
                client = params.get("clientInfo") or {}
                self.log.append("mcp.initialize", {"client": client.get("name", ""), "client_version": client.get("version", ""), "protocol": self.protocol_version}, actor=self.principal.id)
                return _result(
                    mid,
                    {
                        "protocolVersion": self.protocol_version,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "gongwen", "title": "公文智能体", "version": __version__},
                        "instructions": INSTRUCTIONS,
                    },
                )
            if method == "ping":
                return _result(mid, {})
            if method == "tools/list":
                return _result(mid, {"tools": self.tool_list()})
            if method == "tools/call":
                name = params.get("name")
                if name not in self.tools.tools:
                    return _error(mid, -32602, f"Unknown tool: {name}")
                return _result(mid, self.call_tool(name, params.get("arguments")))
            if method in ("resources/list", "prompts/list"):
                return _result(mid, {method.split("/")[0]: []})
            return _error(mid, -32601, f"Method not found: {method}")
        except Exception as exc:  # 失败必须可见：返回协议错误而不是静默
            self.log.append("mcp.error", {"method": method, "error": f"{type(exc).__name__}: {exc}"}, actor=self.principal.id)
            return _error(mid, -32603, f"Internal error: {type(exc).__name__}: {exc}")

    def serve(self, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                _write(stdout, _error(None, -32700, "Parse error"))
                continue
            if isinstance(msg, list):  # 兼容旧版本客户端的批量消息
                if not msg:  # 空批量：按 JSON-RPC 2.0 回应单个错误
                    _write(stdout, _error(None, -32600, "Invalid Request: 空批量"))
                    continue
                replies = [r for r in (self.handle(m) for m in msg) if r is not None]
                if replies:
                    _write(stdout, replies)
                continue
            reply = self.handle(msg)
            if reply is not None:
                _write(stdout, reply)


def _result(mid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _write(stdout: IO[str], obj: Any) -> None:
    stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    stdout.flush()


__all__ = ["McpServer", "SUPPORTED_VERSIONS"]

"""模型接入的端到端回归：本机模拟服务驱动真实的服务商适配、模型网关、技能与对话代理。

模拟服务实现 OpenAI 兼容 /chat/completions 与 Anthropic Messages API（/v1/messages，含流式 SSE），
按官方接口约束校验每个请求，不合规时返回 400 并记入 problems（测试据此失败）：
* OpenAI 兼容：鉴权头、system 在首位、tool 消息必须回应前一条助手消息的 tool_calls、
  json_object 模式要求提示中出现 json、工具定义格式；
* Anthropic：anthropic-version 与 x-api-key 头、max_tokens 必填、system 为顶层参数、首条为 user、
  不得以助手消息结尾（不支持预填）、tool_result 必须回应紧邻的上一条助手消息中的 tool_use、
  不接受采样参数与思考预算、严格模式的 Schema 约束、服务端拒答回退的 beta 头；
  并模拟思考块的“会话绑定”检查：系统提示、工具集或此前任一消息被改动后，回传的思考块签名失效（400）。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

import pytest
from helpers import make_docx, make_xlsx
from test_engine_e2e import BUDGET, SITUATION, run_to_review, start

from gongwen.agent import AgentSession
from gongwen.cli.main import main as cli
from gongwen.harness.egress import EgressGateway, EgressRequest
from gongwen.llm.base import ChatMessage, ModelCallFailed, ModelUnavailable
from gongwen.orchestrator import Engine, default_user
from gongwen.runtime import build_runtime
from gongwen.schemas.common import Clearance, EnvironmentRoute
from gongwen.schemas.patch import PatchSet
from gongwen.schemas.review import ReviewReport
from gongwen.schemas.state import CheckpointKind, Stage

KEY_ENV = "GONGWEN_MOCK_LLM_KEY"
API_KEY = "sk-mock-0123456789abcdef"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
UNSUPPORTED_SCHEMA_KEYS = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength"}
TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
# Messages API 各内容块允许的字段（多余字段会被拒绝：Extra inputs are not permitted）
BLOCK_FIELDS = {
    "text": {"type", "text", "citations", "cache_control"},
    "tool_use": {"type", "id", "name", "input", "cache_control"},
    "thinking": {"type", "thinking", "signature"},
    "redacted_thinking": {"type", "data"},
    "tool_result": {"type", "tool_use_id", "content", "is_error", "cache_control"},
    "fallback": {"type", "from", "to"},
}
# 合法 JSON、但每个调用目的的关键字段类型都不对
BAD_SHAPE = {"purposes": "不是列表", "paragraphs": ["不是对象"], "issues": "不是列表", "facts": [1], "edits": {"x": 1}}
NARRATIVE = "目前部分示范点存在设备老化、专业技术人员不足、服务能力与群众就医需求仍有差距等问题，需要进一步加大投入。"


# ====================================================================== 模拟服务
class Reply:
    """模拟服务的响应：JSON 正文、原始报文、SSE 事件序列、延迟与重定向。"""

    def __init__(self, status: int = 200, body: Any = None, raw: str | None = None, delay: float = 0.0, headers: dict | None = None, sse: list | None = None):
        self.status, self.body, self.raw, self.delay, self.headers, self.sse = status, body, raw, delay, headers or {}, sse


def kind_of(system: str) -> str:
    """按系统提示识别调用目的（与技能中的提示模板对应）。"""
    for key, kind in (("公文起草助手", "drafting"), ("独立的公文审校人员", "review"), ("事实抽取助手", "facts"), ("办文任务分析助手", "task"), ("定向修订助手", "revision"), ("对话助手", "agent")):
        if key in system:
            return kind
    return "other"


def structured(kind: str, user: str) -> dict:
    """结构化输出的默认答复：起草时把“拟新建”段落改写成虚构的已完成事项和金额（应被校验器拒绝）。"""
    if kind == "drafting":
        task = json.loads(user.split("待改写段落：\n", 1)[1])
        out = []
        for p in task:
            text = p["draft"]
            if "拟新建" in text:
                text = "2026年已建成示范点12个，投入资金300万元。"
            out.append({"para_id": p["para_id"], "sentences": [{"text": text, "refs": p["refs"]}]})
        return {"paragraphs": out}
    if kind == "review":
        sents = json.loads(user.split("文稿逐句：\n", 1)[1])
        target = next((s for s in sents if "示范点" in s["text"]), sents[0])
        return {
            "issues": [
                {"sid": target["sid"], "type": "表述不准确", "severity": "一般", "explanation": "模拟审校：表述可更准确", "suggestion": "核对表述"},
                {"sid": "s-not-exist", "type": "夸大成绩", "severity": "重要", "explanation": "定位不到原句，应被丢弃", "suggestion": "无"},
            ]
        }
    if kind == "facts":
        # 一条是原文片段（应采纳），一条是改写出的“事实”（不是原文片段，应拒绝）
        units = re.findall(r'<untrusted kind="material" id="([^"]+)">\n(.*?)\n</untrusted>', user, re.S)
        uid = next((u for u, t in units if "专业技术人员不足" in t), units[0][0] if units else "")
        return {
            "facts": [
                {"unit_id": uid, "statement": "部分示范点存在设备老化、专业技术人员不足", "attribute": "存在问题", "progress": "不适用"},
                {"unit_id": uid, "statement": "示范点已全部完成设备更新", "attribute": "进展", "progress": "已完成"},
            ]
        }
    if kind == "task":
        return {"purposes": [], "issuer": None, "recipients": [], "subject": None}
    if kind == "revision":
        # 在一句后补写材料中没有的金额（应转人工确认），另有一条定位不到原句（应忽略）
        sents = json.loads(user.split("文稿逐句：\n", 1)[1])
        target = next(s for s in sents if "示范点" in s["text"])
        return {
            "edits": [
                {"sid": target["sid"], "new_text": target["text"].rstrip("。") + "，累计投入资金999万元。", "reason": "补充经费投入"},
                {"sid": "s-not-exist", "new_text": "无", "reason": "无"},
            ]
        }
    return {}


Turn = dict[str, Any]
AgentScript = Callable[[list[Turn]], "list[tuple[str, dict]] | str"]


def default_agent(conv: list[Turn]) -> list[tuple[str, dict]] | str:
    return "（模拟）收到。"


def _merge_turns(messages: list[dict]) -> list[dict]:
    """官方接口会把连续的同角色消息合并为一轮：校验工具调用配对前先合并。"""
    out: list[dict] = []
    for m in messages:
        blocks = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"] = out[-1]["content"] + blocks
        else:
            out.append({"role": m["role"], "content": list(blocks)})
    return out


def _canon(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


class MockLLM:
    def __init__(self, host: str = "127.0.0.1", api_key: str | None = API_KEY):
        self.host = host
        self.api_key = api_key
        self.requests: list[dict] = []
        self.problems: list[str] = []
        self.faults: dict[str, Any] = {}  # 调用目的 → 故障（"http500"/"refusal"/Reply/可调用对象）
        self.agent: AgentScript = default_agent
        self.lock = threading.Lock()
        self._n = 0
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # 保持测试输出干净
                pass

            def do_POST(self):
                mock._handle(self)

        self.server = ThreadingHTTPServer((host, 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    # ------------------------------------------------------------------ 基础
    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def next_id(self, prefix: str) -> str:
        with self.lock:
            self._n += 1
            return f"{prefix}_{self._n:04d}"

    def seen(self, needle: str) -> bool:
        return any(needle in r["raw"] for r in self.requests)

    def calls(self, kind: str | None = None) -> list[dict]:
        out = []
        for r in self.requests:
            b = r.get("body") or {}
            system = b.get("system") if "system" in b else next((m.get("content") for m in b.get("messages", []) if m.get("role") == "system"), "")
            if isinstance(system, list):
                system = "".join(x.get("text", "") for x in system)
            if kind is None or kind_of(system or "") == kind:
                out.append(r)
        return out

    # ------------------------------------------------------------------ 收发
    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        length = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(length).decode("utf-8", "replace")
        rec = {"path": h.path, "headers": {k.lower(): v for k, v in h.headers.items()}, "raw": raw, "body": None}
        try:
            rec["body"] = json.loads(raw)
        except json.JSONDecodeError:
            pass
        with self.lock:
            self.requests.append(rec)
        path = urlparse(h.path).path
        try:
            if path.endswith("/chat/completions"):
                reply = self._openai(rec)
            elif path.endswith("/v1/messages"):
                reply = self._anthropic(rec)
            else:
                reply = Reply(404, {"error": {"message": f"未知路径 {path}"}})
        except Exception as exc:  # 模拟服务自身出错：按 500 返回，并记为问题
            self.problems.append(f"mock error: {type(exc).__name__}: {exc}")
            reply = Reply(500, {"error": {"message": "mock internal error"}})
        self._send(h, reply)

    def _send(self, h: BaseHTTPRequestHandler, reply: Reply) -> None:
        try:
            if reply.delay:
                time.sleep(reply.delay)
            if reply.sse is not None:
                h.send_response(reply.status)
                h.send_header("Content-Type", "text/event-stream")
                h.send_header("Connection", "close")
                h.end_headers()
                for name, data in reply.sse:
                    h.wfile.write(f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
                    h.wfile.flush()
                h.close_connection = True
                return
            payload = (reply.raw if reply.raw is not None else json.dumps(reply.body, ensure_ascii=False)).encode()
            h.send_response(reply.status)
            h.send_header("Content-Type", "application/json" if reply.raw is None else "text/html")
            h.send_header("Content-Length", str(len(payload)))
            for k, v in reply.headers.items():
                h.send_header(k, v)
            h.end_headers()
            h.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端已超时断开

    def _fault(self, kind: str, body: dict) -> Any:
        f = self.faults.get(kind)
        return f(body) if callable(f) else f

    # ------------------------------------------------------------------ OpenAI 兼容
    def _openai(self, rec: dict) -> Reply:
        problem = self.check_openai(rec)
        if problem:
            self.problems.append(problem)
            return Reply(400, {"error": {"message": problem, "type": "invalid_request_error"}})
        body = rec["body"]
        msgs = body["messages"]
        system = msgs[0]["content"] if msgs[0]["role"] == "system" else ""
        kind = "agent" if body.get("tools") else kind_of(system)
        fault = self._fault(kind, body)
        if isinstance(fault, Reply):
            return fault
        if fault == "http500":
            return Reply(500, {"error": {"message": "upstream overloaded", "type": "server_error"}})
        if fault == "refusal":
            return Reply(200, self.oa_completion(body, content="", finish="content_filter"))
        if fault == "garbage":
            return Reply(200, self.oa_completion(body, content="好的，以下是改写后的段落：第一段……"))
        if fault == "bad_shape":
            return Reply(200, self.oa_completion(body, content=json.dumps(BAD_SHAPE, ensure_ascii=False)))
        if fault == "html200":
            return Reply(200, raw="<html><body>proxy login required</body></html>")
        if fault == "empty_choices":
            return Reply(200, {"id": "x", "object": "chat.completion", "model": body["model"], "choices": []})
        if kind == "agent":
            plan = self.agent(self._oa_conv(msgs))
            if isinstance(plan, str):
                return Reply(200, self.oa_completion(body, content=plan))
            calls = [{"id": self.next_id("call"), "type": "function", "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}} for n, a in plan]
            return Reply(200, self.oa_completion(body, content=None, tool_calls=calls, finish="tool_calls"))
        user = next(m["content"] for m in reversed(msgs) if m["role"] == "user")
        return Reply(200, self.oa_completion(body, content=json.dumps(structured(kind, user), ensure_ascii=False)))

    def oa_completion(self, body: dict, content: Any, tool_calls: list | None = None, finish: str = "stop", usage: Any = "auto") -> dict:
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return {
            "id": self.next_id("chatcmpl"),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body["model"],
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": len(json.dumps(body)) // 4, "completion_tokens": 20, "total_tokens": 0} if usage == "auto" else usage,
        }

    @staticmethod
    def _oa_conv(msgs: list[dict]) -> list[Turn]:
        conv: list[Turn] = []
        names: dict[str, str] = {}
        for m in msgs:
            if m["role"] == "user":
                conv.append({"role": "user", "text": m["content"]})
            elif m["role"] == "assistant":
                calls = [(c["id"], c["function"]["name"], json.loads(c["function"]["arguments"])) for c in m.get("tool_calls") or []]
                names.update({cid: n for cid, n, _ in calls})
                conv.append({"role": "assistant", "text": m.get("content") or "", "calls": calls})
            elif m["role"] == "tool":
                conv.append({"role": "tool", "id": m["tool_call_id"], "name": names.get(m["tool_call_id"], ""), "content": m["content"], "is_error": m["content"].startswith("错误：")})
        return conv

    def check_openai(self, rec: dict) -> str | None:
        h, b = rec["headers"], rec["body"]
        if b is None:
            return "请求体不是 JSON"
        if self.api_key and h.get("authorization") != f"Bearer {self.api_key}":
            return "缺少或错误的 Authorization: Bearer 头"
        if h.get("content-type", "").split(";")[0] != "application/json":
            return "Content-Type 应为 application/json"
        if not isinstance(b.get("model"), str) or not b["model"]:
            return "缺少 model"
        if b.get("stream"):
            return "适配层不应使用流式（模拟服务未实现）"
        if "max_tokens" in b and not (isinstance(b["max_tokens"], int) and b["max_tokens"] > 0):
            return "max_tokens 应为正整数"
        if "temperature" in b and not (isinstance(b["temperature"], (int, float)) and 0 <= b["temperature"] <= 2):
            return "temperature 超出范围"
        msgs = b.get("messages")
        if not isinstance(msgs, list) or not msgs:
            return "messages 不能为空"
        pending: set[str] = set()
        for i, m in enumerate(msgs):
            role = m.get("role")
            if role not in ("system", "user", "assistant", "tool"):
                return f"messages[{i}].role 无效：{role}"
            if role != "tool" and pending:
                return f"messages[{i}]：助手消息的 tool_calls 未全部得到 tool 消息回应：{sorted(pending)}"
            if role == "system" and i != 0:
                return "system 消息应位于首位"
            if role in ("system", "user") and not isinstance(m.get("content"), str):
                return f"messages[{i}].content 应为字符串"
            if role == "assistant":
                tcs = m.get("tool_calls") or []
                if not tcs and not isinstance(m.get("content"), str):
                    return f"messages[{i}]：助手消息缺少 content"
                for tc in tcs:
                    fn = tc.get("function") or {}
                    if not tc.get("id") or tc.get("type") != "function" or not isinstance(fn.get("name"), str) or not isinstance(fn.get("arguments"), str):
                        return f"messages[{i}].tool_calls 格式无效：{tc}"
                    json.loads(fn["arguments"])
                ids = [tc["id"] for tc in tcs]
                if len(set(ids)) != len(ids):
                    return f"messages[{i}].tool_calls 编号重复"
                pending = set(ids)
            if role == "tool":
                if m.get("tool_call_id") not in pending:
                    return f"messages[{i}]：tool 消息未对应前一条助手消息的 tool_calls（{m.get('tool_call_id')}）"
                if not isinstance(m.get("content"), str):
                    return f"messages[{i}].content 应为字符串"
                pending.discard(m["tool_call_id"])
        for t in b.get("tools") or []:
            fn = t.get("function") or {}
            if t.get("type") != "function" or not TOOL_NAME.match(fn.get("name", "")) or (fn.get("parameters") or {}).get("type") != "object":
                return f"工具定义无效：{t}"
        if "tool_choice" in b and b["tool_choice"] not in ("auto", "none", "required") and not isinstance(b["tool_choice"], dict):
            return "tool_choice 无效"
        if (b.get("response_format") or {}).get("type") == "json_object":
            if "json" not in json.dumps(msgs, ensure_ascii=False).lower():
                return "json_object 模式要求消息中出现 json 字样"
        return None

    # ------------------------------------------------------------------ Anthropic
    def sign(self, body: dict, upto: int) -> str:
        """思考块签名：绑定系统提示、工具集（按名称排序）与该块之前的全部消息。"""
        tools = sorted(body.get("tools") or [], key=lambda t: t.get("name", ""))
        return "sig-" + hashlib.sha256(_canon({"system": body.get("system"), "tools": tools, "messages": body["messages"][:upto]}).encode()).hexdigest()[:32]

    def thinking(self, body: dict) -> dict:
        return {"type": "thinking", "thinking": "", "signature": self.sign(body, len(body["messages"]))}

    def _anthropic(self, rec: dict) -> Reply:
        status, problem = self.check_anthropic(rec)
        if problem:
            self.problems.append(problem)
            etype = {401: "authentication_error"}.get(status, "invalid_request_error")
            return Reply(status, {"type": "error", "error": {"type": etype, "message": problem}})
        body = rec["body"]
        system = body.get("system") or ""
        if isinstance(system, list):
            system = "".join(x.get("text", "") for x in system)
        kind = "agent" if body.get("tools") else kind_of(system)
        fault = self._fault(kind, body)
        if isinstance(fault, Reply):
            return fault
        if fault == "http500":
            return Reply(500, {"type": "error", "error": {"type": "api_error", "message": "Internal server error"}})
        if fault == "refusal":
            msg = self.an_message(body, [], "refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": "模拟拒答"}, think=False)
        elif fault == "garbage":
            msg = self.an_message(body, [{"type": "text", "text": "好的，以下是改写后的段落：第一段……"}], "end_turn")
        elif fault == "bad_shape":
            msg = self.an_message(body, [{"type": "text", "text": json.dumps(BAD_SHAPE, ensure_ascii=False)}], "end_turn")
        elif isinstance(fault, dict):
            msg = fault
        elif kind == "agent":
            plan = self.agent(self._an_conv(body["messages"]))
            if isinstance(plan, str):
                msg = self.an_message(body, [{"type": "text", "text": plan}], "end_turn")
            else:
                blocks = [{"type": "tool_use", "id": self.next_id("toolu"), "name": n, "input": a} for n, a in plan]
                msg = self.an_message(body, blocks, "tool_use")
        else:
            user = body["messages"][-1]["content"]
            user = user if isinstance(user, str) else "".join(x.get("text", "") for x in user if x.get("type") == "text")
            msg = self.an_message(body, [{"type": "text", "text": json.dumps(structured(kind, user), ensure_ascii=False)}], "end_turn")
        return Reply(200, sse=self.to_sse(msg)) if body.get("stream") else Reply(200, msg)

    def an_message(self, body: dict, blocks: list[dict], stop: str, stop_details: dict | None = None, think: bool = True) -> dict:
        msg = {
            "id": self.next_id("msg"),
            "type": "message",
            "role": "assistant",
            "model": body["model"],
            "content": ([self.thinking(body)] if think else []) + blocks,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": len(json.dumps(body)) // 4, "output_tokens": 20},
        }
        if stop_details:
            msg["stop_details"] = stop_details
        return msg

    @staticmethod
    def to_sse(msg: dict) -> list[tuple[str, dict]]:
        start = {k: v for k, v in msg.items() if k != "stop_details"}
        start.update(content=[], stop_reason=None, stop_sequence=None, usage={"input_tokens": msg["usage"]["input_tokens"], "output_tokens": 1})
        ev: list[tuple[str, dict]] = [("message_start", {"type": "message_start", "message": start}), ("ping", {"type": "ping"})]
        for i, b in enumerate(msg["content"]):
            if b["type"] == "text":
                ev.append(("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}}))
                half = len(b["text"]) // 2
                for chunk in (b["text"][:half], b["text"][half:]):
                    ev.append(("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": chunk}}))
            elif b["type"] == "tool_use":
                ev.append(("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}}))
                js = json.dumps(b["input"], ensure_ascii=False)
                for chunk in (js[: len(js) // 2], js[len(js) // 2 :]):
                    ev.append(("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": chunk}}))
            elif b["type"] == "thinking":
                ev.append(("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "thinking", "thinking": "", "signature": ""}}))
                if b["thinking"]:
                    ev.append(("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "thinking_delta", "thinking": b["thinking"]}}))
                ev.append(("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "signature_delta", "signature": b["signature"]}}))
            else:
                ev.append(("content_block_start", {"type": "content_block_start", "index": i, "content_block": b}))
            ev.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
        delta: dict[str, Any] = {"stop_reason": msg["stop_reason"], "stop_sequence": None}
        if msg.get("stop_details"):
            delta["stop_details"] = msg["stop_details"]
        ev.append(("message_delta", {"type": "message_delta", "delta": delta, "usage": {"output_tokens": msg["usage"]["output_tokens"]}}))
        ev.append(("message_stop", {"type": "message_stop"}))
        return ev

    @staticmethod
    def _an_conv(messages: list[dict]) -> list[Turn]:
        conv: list[Turn] = []
        names: dict[str, str] = {}
        for m in messages:
            blocks = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
            if m["role"] == "assistant":
                calls = [(b["id"], b["name"], b["input"]) for b in blocks if b["type"] == "tool_use"]
                names.update({cid: n for cid, n, _ in calls})
                conv.append({"role": "assistant", "text": "".join(b.get("text", "") for b in blocks if b["type"] == "text"), "calls": calls})
                continue
            for b in blocks:
                if b["type"] == "tool_result":
                    content = b["content"] if isinstance(b["content"], str) else "".join(x.get("text", "") for x in b["content"])
                    conv.append({"role": "tool", "id": b["tool_use_id"], "name": names.get(b["tool_use_id"], ""), "content": content, "is_error": bool(b.get("is_error"))})
                elif b["type"] == "text":
                    conv.append({"role": "user", "text": b["text"]})
        return conv

    @staticmethod
    def _schema_problem(schema: Any, where: str) -> str | None:
        """结构化输出与严格工具：每个对象须 additionalProperties=false，且不得含数值、长度约束。"""
        if isinstance(schema, dict):
            bad = UNSUPPORTED_SCHEMA_KEYS & set(schema)
            if bad:
                return f"{where}：严格模式不支持 {sorted(bad)}"
            if schema.get("type") == "object" and schema.get("additionalProperties") is not False:
                return f"{where}：对象须声明 additionalProperties=false"
            for k, v in schema.items():
                if k in ("properties", "$defs"):
                    for name, sub in v.items():
                        p = MockLLM._schema_problem(sub, f"{where}.{name}")
                        if p:
                            return p
                elif k in ("items", "anyOf", "allOf"):
                    for sub in v if isinstance(v, list) else [v]:
                        p = MockLLM._schema_problem(sub, f"{where}[]")
                        if p:
                            return p
        return None

    def check_anthropic(self, rec: dict) -> tuple[int, str | None]:
        h, b = rec["headers"], rec["body"]
        if b is None:
            return 400, "请求体不是 JSON"
        if not h.get("anthropic-version"):
            return 400, "缺少 anthropic-version 头"
        if self.api_key and h.get("x-api-key") != self.api_key:
            return 401, "缺少或错误的 x-api-key 头"
        for k in ("model", "max_tokens", "messages"):
            if k not in b:
                return 400, f"{k}: Field required"
        if not isinstance(b["max_tokens"], int) or b["max_tokens"] < 1:
            return 400, "max_tokens 应为正整数"
        for k in ("temperature", "top_p", "top_k"):
            if k in b:
                return 400, f"{k}: 当前模型不接受采样参数"
        th = b.get("thinking")
        if th and (th.get("type") in ("disabled", "enabled") or "budget_tokens" in th):
            return 400, "thinking: 当前模型思考始终开启，不接受 disabled 或 budget_tokens"
        if (b.get("tool_choice") or {}).get("type") in ("any", "tool"):
            return 400, "tool_choice: 当前模型不支持强制工具调用"
        betas = [x.strip() for x in h.get("anthropic-beta", "").split(",") if x.strip()]
        if "betas" in b:
            return 400, "betas: 应通过 anthropic-beta 头传递"
        if "fallbacks" in b and (b["fallbacks"] != "default" or FALLBACK_BETA not in betas):
            return 400, "fallbacks: \"default\" 须配合 server-side-fallback-2026-07-01 头"
        oc = b.get("output_config") or {}
        if "effort" in oc and oc["effort"] not in ("low", "medium", "high", "xhigh", "max"):
            return 400, "output_config.effort 无效"
        if "format" in oc:
            fmt = oc["format"]
            if fmt.get("type") != "json_schema" or not isinstance(fmt.get("schema"), dict):
                return 400, "output_config.format 应为 json_schema"
            p = self._schema_problem(fmt["schema"], "output_config.format.schema")
            if p:
                return 400, p
        system = b.get("system")
        if system is not None and not (isinstance(system, str) or (isinstance(system, list) and all(x.get("type") == "text" for x in system))):
            return 400, "system 应为字符串或文本块列表"
        names = set()
        for t in b.get("tools") or []:
            if not TOOL_NAME.match(t.get("name", "")) or (t.get("input_schema") or {}).get("type") != "object":
                return 400, f"tools：定义无效 {t.get('name')}"
            if t["name"] in names:
                return 400, f"tools：工具重名 {t['name']}"
            names.add(t["name"])
            if t.get("strict"):
                p = self._schema_problem(t["input_schema"], f"tools.{t['name']}.input_schema")
                if p:
                    return 400, p
        msgs = b["messages"]
        if not isinstance(msgs, list) or not msgs:
            return 400, "messages: 不能为空"
        if msgs[0].get("role") != "user":
            return 400, "messages: 第一条消息必须是 user"
        if msgs[-1].get("role") == "assistant":
            return 400, "messages: 当前模型不支持助手预填（最后一条不能是 assistant）"
        for i, m in enumerate(msgs):
            if m.get("role") not in ("user", "assistant"):
                return 400, f"messages.{i}.role 无效：{m.get('role')}"
            c = m.get("content")
            if isinstance(c, str):
                if not c:
                    return 400, f"messages.{i}: 文本内容不能为空"
                continue
            if not isinstance(c, list) or not c:
                return 400, f"messages.{i}: 除可选的最后一条助手消息外，所有消息都必须有非空内容"
            allowed = {"text", "tool_use", "thinking", "redacted_thinking", "fallback"} if m["role"] == "assistant" else {"text", "tool_result", "image", "document"}
            for j, blk in enumerate(c):
                if blk.get("type") not in allowed:
                    return 400, f"messages.{i}.content.{j}: 不允许的块类型 {blk.get('type')}"
                if blk["type"] == "text" and not blk.get("text"):
                    return 400, f"messages.{i}.content.{j}: text 块不能为空"
                extra = set(blk) - BLOCK_FIELDS.get(blk["type"], set(blk))
                if extra:
                    return 400, f"messages.{i}.content.{j}.{sorted(extra)[0]}: Extra inputs are not permitted"
                if blk["type"] == "thinking" and blk.get("signature") != self.sign(b, i):
                    return 400, (
                        f"messages.{i}.content.{j}: Invalid `signature` in `thinking` block. The block is bound to a different conversation. "
                        "Remove the block, or set `thinking.block_binding.prefix_mismatch_behavior` to \"drop_block\"."
                    )
            kinds = [blk["type"] for blk in c]
            if "fallback" in kinds:
                cut = len(kinds) - 1 - kinds[::-1].index("fallback")
                if any(k in ("thinking", "redacted_thinking", "tool_use") for k in kinds[:cut]):
                    return 400, f"messages.{i}: 回退边界之前的思考块与工具调用不应回传"
        turns = _merge_turns(msgs)
        for i, t in enumerate(turns):
            uses = [blk["id"] for blk in t["content"] if blk["type"] == "tool_use"]
            if t["role"] == "assistant":
                if len(set(uses)) != len(uses):
                    return 400, "tool_use 编号重复"
                if uses:
                    nxt = turns[i + 1]["content"] if i + 1 < len(turns) else []
                    results = [blk.get("tool_use_id") for blk in nxt if blk["type"] == "tool_result"]
                    lead = [blk["type"] for blk in nxt[: len(results)]]
                    if set(results) != set(uses) or any(k != "tool_result" for k in lead):
                        return 400, "tool_use ids were found without tool_result blocks immediately after (tool_result 块须紧随其后并位于内容开头)"
            else:
                prev = turns[i - 1]["content"] if i > 0 else []
                prev_uses = {blk["id"] for blk in prev if blk["type"] == "tool_use"}
                for blk in t["content"]:
                    if blk["type"] == "tool_result" and blk.get("tool_use_id") not in prev_uses:
                        return 400, f"unexpected tool_use_id found in tool_result blocks: {blk.get('tool_use_id')}"
        return 200, None


# ====================================================================== 夹具与辅助
@pytest.fixture
def mock():
    m = MockLLM()
    yield m
    m.close()


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, API_KEY)


PROVIDERS = ["deepseek", "anthropic"]


def model_conf(mock: MockLLM, provider: str, **extra) -> dict:
    base = mock.url if provider == "anthropic" else f"{mock.url}/v1"
    return {"provider": provider, "name": "mock-model", "base_url": base, "api_key_env": KEY_ENV, "timeout": 10, "max_retries": 0, **extra}


def model_engine(tmp_path, mock: MockLLM, provider: str, env: dict | None = None, egress: dict | None = None, **extra) -> Engine:
    if provider == "anthropic":
        pytest.importorskip("anthropic")
    overrides = {
        "environment": {"unit_name": "示例市卫生健康委员会", "region": "示例省", **(env or {})},
        "layout": {"render_check": False},
        "model": model_conf(mock, provider, **extra),
        "egress": egress or {},
    }
    return Engine(build_runtime(tmp_path, overrides=overrides))


def events(eng: Engine, task_id: str, types: set[str] | None = None) -> list[dict]:
    return list(eng.log(task_id).replay(types))


def start_narrative(eng: Engine, user):
    """同 test_engine_e2e.start，另加一段较长的叙述（事实抽取只处理 40 字以上的段落）。"""
    st = eng.create_task("写一份向主管部门申请基层医疗示范点建设经费的报告", by=user, hints={"recipients": "示例市人民政府", "issuer_type": "政府部门"})
    eng.add_material(st.task_id, "情况说明.docx", make_docx(SITUATION + [NARRATIVE]), by=user, declared=Clearance.PUBLIC)
    eng.add_material(st.task_id, "经费测算表.xlsx", make_xlsx(BUDGET, sheet="经费测算"), by=user, declared=Clearance.PUBLIC)
    return st


def advance_auto(eng: Engine, user, task_id: str):
    return eng.advance(task_id, by=user, auto_accept={"task_confirm", "outline_confirm", "review_escalation"})


# ====================================================================== (a)(b)(f) 完整流程
@pytest.mark.parametrize("provider", PROVIDERS)
def test_pipeline_with_model_drafting_and_review(tmp_path, mock, provider):
    eng = model_engine(tmp_path, mock, provider)
    user = default_user()
    st = start_narrative(eng, user)
    st = run_to_review(eng, user, st.task_id)
    assert st.stage == Stage.HUMAN_REVIEW, st.errors
    assert mock.problems == []
    # 事实抽取：只采纳原文片段
    extract = [e["payload"] for e in events(eng, st.task_id, {"skill.model_extract"})]
    assert extract == [{"accepted": 1, "rejected": 1}]
    ledger = eng.load_matter_ledger(st)
    assert [f.statement for f in ledger.facts if "model_extracted" in f.tags] == ["部分示范点存在设备老化、专业技术人员不足"]
    # 起草：模型改写被逐句校验，虚构的“已建成”与金额被拒绝，其余段落采用模型表达
    ir = eng.current_ir(st)
    text = ir.full_text()
    assert "300万元" not in text and "已建成示范点12个" not in text
    assert "拟新建示范点12个" in text
    assert ir.meta["drafter"] == "model+validator"
    assert int(ir.meta["model_paragraphs_rejected"]) >= 1 and int(ir.meta["model_paragraphs_accepted"]) >= 1
    assert any(s.origin == "model" for _, s in ir.iter_sentences())
    # 独立审校：模型问题定位到原句、交人工确认；定位不到的问题被丢弃
    report = eng.store.load_model(st.task_id, "review_report", ReviewReport)
    assert "model" in report.channels
    model_issues = [i for i in report.issues if i.channel == "model"]
    assert len(model_issues) == 1 and model_issues[0].needs_human
    # 每个调用目的都真实经过了 HTTP 接口
    kinds = {k for k in ("task", "facts", "drafting", "review") if mock.calls(k)}
    assert kinds == {"task", "facts", "drafting", "review"}
    # 审计只记哈希与对象编号：材料原文与密钥都不进入日志
    raw_log = (eng.store.task_dir(st.task_id) / "events.jsonl").read_text(encoding="utf-8")
    assert "设备老化" not in raw_log and API_KEY not in raw_log
    assert len(events(eng, st.task_id, {"model.request"})) == len(mock.requests)
    assert len(events(eng, st.task_id, {"model.response"})) == len(mock.requests)


def test_openai_request_payload(tmp_path, mock):
    eng = model_engine(tmp_path, mock, "deepseek")
    user = default_user()
    st = start(eng, user)
    run_to_review(eng, user, st.task_id)
    assert mock.problems == []
    for r in mock.requests:
        b = r["body"]
        assert r["path"] == "/v1/chat/completions"
        assert r["headers"]["authorization"] == f"Bearer {API_KEY}"
        assert b["model"] == "mock-model" and b["max_tokens"] == 4096 and b["temperature"] == 0.2
        assert b["messages"][0]["role"] == "system" and "JSON Schema" in b["messages"][0]["content"]
        assert b["response_format"] == {"type": "json_object"} and "tools" not in b and not b.get("stream")


def test_anthropic_request_payload(tmp_path, mock):
    eng = model_engine(tmp_path, mock, "anthropic")
    user = default_user()
    st = start(eng, user)
    run_to_review(eng, user, st.task_id)
    assert mock.problems == []
    for r in mock.requests:
        b, h = r["body"], r["headers"]
        assert urlparse(r["path"]).path == "/v1/messages"
        assert h["x-api-key"] == API_KEY and h["anthropic-version"]
        assert FALLBACK_BETA in h.get("anthropic-beta", "") and b["fallbacks"] == "default"
        assert b["model"] == "mock-model" and b["max_tokens"] >= 16000
        assert isinstance(b["system"], str) and all(m["role"] != "system" for m in b["messages"])
        assert b["output_config"]["effort"] == "high" and b["output_config"]["format"]["type"] == "json_schema"
        assert "thinking" not in b and "temperature" not in b
        # 长输出走流式，避免非流式请求在生成完成前超时
        assert b.get("stream") is True
        # 严格模式的 Schema 只用文档明确支持的写法：联合类型写成 anyOf，而不是类型数组
        assert '"type": [' not in json.dumps(b["output_config"]["format"]["schema"], ensure_ascii=False)
    task_schema = next(r["body"]["output_config"]["format"]["schema"] for r in mock.calls("task"))
    assert task_schema["properties"]["issuer"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}


# ====================================================================== (c) 拒答、HTTP 错误、超时、无效输出
@pytest.mark.parametrize("provider", PROVIDERS)
def test_refusal_is_logged_and_falls_back(tmp_path, mock, provider):
    """拒答：显式记录（model.response refused）并回退确定性路径，不把空结果当成功。"""
    mock.faults["drafting"] = "refusal"
    eng = model_engine(tmp_path, mock, provider)
    user = default_user()
    st = start(eng, user)
    st = advance_auto(eng, user, st.task_id)
    assert st.stage == Stage.HUMAN_REVIEW and mock.problems == []
    ir = eng.current_ir(st)
    assert ir.meta["drafter"] == "deterministic" and "拟新建示范点12个" in ir.full_text()
    refused = [e for e in events(eng, st.task_id, {"model.response"}) if e["payload"]["refused"]]
    assert len(refused) == 1
    skipped = [e["payload"] for e in events(eng, st.task_id, {"skill.model_skipped"})]
    assert any(p["skill"] == "gongwen-constrained-drafting" and "拒答" in p["reason"] for p in skipped)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_http_500_fails_task_visibly(tmp_path, mock, provider):
    mock.faults["drafting"] = "http500"
    eng = model_engine(tmp_path, mock, provider)
    user = default_user()
    st = start(eng, user)
    st = advance_auto(eng, user, st.task_id)
    assert st.stage == Stage.FAILED
    assert "模型接口调用失败" in st.exception_reason and "500" in st.exception_reason
    errs = events(eng, st.task_id, {"model.error"})
    assert len(errs) == 1 and errs[0]["payload"]["purpose"] == "drafting"
    raw_log = (eng.store.task_dir(st.task_id) / "events.jsonl").read_text(encoding="utf-8")
    assert API_KEY not in raw_log


@pytest.mark.parametrize("provider", PROVIDERS)
def test_timeout_fails_task_visibly(tmp_path, mock, provider):
    mock.faults["drafting"] = Reply(200, {"never": "sent"}, delay=3)
    eng = model_engine(tmp_path, mock, provider, timeout=0.5)
    user = default_user()
    st = start(eng, user)
    t0 = time.monotonic()
    st = advance_auto(eng, user, st.task_id)
    assert time.monotonic() - t0 < 10
    assert st.stage == Stage.FAILED and "模型接口调用失败" in st.exception_reason
    assert len(mock.calls("drafting")) == 1  # max_retries=0：不重试


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("fault", ["garbage", "bad_shape"])
def test_invalid_model_output_falls_back(tmp_path, mock, provider, fault):
    """非 JSON 或结构不符（如段落是字符串）的输出：记录后回退，不能让技能因类型错误使任务失败。"""
    for kind in ("task", "facts", "drafting", "review"):
        mock.faults[kind] = fault
    eng = model_engine(tmp_path, mock, provider)
    user = default_user()
    st = start_narrative(eng, user)
    st = advance_auto(eng, user, st.task_id)
    assert st.stage == Stage.HUMAN_REVIEW, st.errors
    assert eng.current_ir(st).meta["drafter"] == "deterministic"
    skipped = {e["payload"]["skill"] for e in events(eng, st.task_id, {"skill.model_skipped"})}
    assert {"gongwen-constrained-drafting", "gongwen-independent-review", "gongwen-fact-ledger", "gongwen-task-modeling"} <= skipped


@pytest.mark.parametrize("fault", ["html200", "empty_choices"])
def test_openai_malformed_http_body_is_a_call_failure(tmp_path, mock, fault):
    """HTTP 200 但报文不是合法的补全结果（代理登录页、空 choices）：属于接口故障，不能当作模型输出空文本。"""
    mock.faults["drafting"] = fault
    eng = model_engine(tmp_path, mock, "deepseek")
    user = default_user()
    st = start(eng, user)
    st = advance_auto(eng, user, st.task_id)
    assert st.stage == Stage.FAILED and "模型接口调用失败" in st.exception_reason


def test_openai_retries_transient_errors(tmp_path, mock):
    state = {"n": 0}

    def flaky(body):
        state["n"] += 1
        return Reply(429, {"error": {"message": "rate limited"}}, headers={"retry-after": "0"}) if state["n"] == 1 else None

    mock.faults["drafting"] = flaky
    eng = model_engine(tmp_path, mock, "deepseek", max_retries=2)
    user = default_user()
    st = start(eng, user)
    st = advance_auto(eng, user, st.task_id)
    assert st.stage == Stage.HUMAN_REVIEW and len(mock.calls("drafting")) == 2
    assert eng.current_ir(st).meta["drafter"] == "model+validator"


def test_openai_tolerates_provider_quirks(tmp_path, mock):
    """部分兼容服务：usage 字段为 null、工具调用缺少编号、arguments 为对象而非字符串。"""
    from gongwen.llm.openai_compat import OpenAICompatProvider

    calls = {"n": 0}

    def quirky(body):
        calls["n"] += 1
        if calls["n"] == 1:
            msg = {"role": "assistant", "content": None, "tool_calls": [{"type": "function", "function": {"name": "task_list", "arguments": {"limit": 3}}}, {"type": "function", "function": {"name": "genre_guide", "arguments": "{\"genre\": \"通知\"}"}}]}
            return Reply(200, {"id": "x", "model": body["model"], "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": None, "completion_tokens": None}})
        return None

    mock.faults["agent"] = quirky
    p = OpenAICompatProvider("openai_compat", model="mock-model", base_url=f"{mock.url}/v1", api_key_env=KEY_ENV, max_retries=0)
    from gongwen.llm.base import ToolDef

    tools = [ToolDef("task_list", "列出任务", {"type": "object", "properties": {"limit": {"type": "integer"}}}), ToolDef("genre_guide", "文种", {"type": "object", "properties": {"genre": {"type": "string"}}})]
    r1 = p.complete([ChatMessage("user", "列出任务")], system="你是“公文智能体”的对话助手", tools=tools)
    assert [c.name for c in r1.tool_calls] == ["task_list", "genre_guide"]
    assert r1.tool_calls[0].arguments == {"limit": 3} and r1.usage.input_tokens == 0
    ids = [c.id for c in r1.tool_calls]
    assert all(ids) and len(set(ids)) == 2
    history = [ChatMessage("user", "列出任务"), ChatMessage("assistant", r1.text, tool_calls=r1.tool_calls)] + [ChatMessage("tool", "{}", tool_call_id=c.id, name=c.name) for c in r1.tool_calls]
    r2 = p.complete(history, system="你是“公文智能体”的对话助手", tools=tools)
    assert mock.problems == [] and r2.text


TASK_SYSTEM = "你是公文办文任务分析助手。"
TASK_SCHEMA = {
    "type": "object",
    "properties": {"purposes": {"type": "array", "items": {"type": "string"}}, "issuer": {"type": ["string", "null"]}, "recipients": {"type": "array", "items": {"type": "string"}}, "subject": {"type": ["string", "null"]}},
    "required": ["purposes", "issuer", "recipients", "subject"],
    "additionalProperties": False,
}


@pytest.mark.parametrize("preset", ["deepseek", "xai", "zhipu", "qwen", "moonshot", "ollama", "vllm", "openai_compat"])
def test_openai_compatible_presets(tmp_path, monkeypatch, preset):
    """各 OpenAI 兼容预设：未配置 api_key_env 时读取预设的密钥变量；本地部署（Ollama、vLLM）不发送鉴权头。"""
    from gongwen.llm.openai_compat import PRESETS

    key_env = PRESETS[preset]["api_key_env"]
    srv = MockLLM(api_key=API_KEY if key_env else None)
    try:
        if key_env:
            monkeypatch.setenv(key_env, API_KEY)
        rt = build_runtime(tmp_path, overrides={"model": {"provider": preset, "name": "mock-model", "base_url": f"{srv.url}/v1", "max_retries": 0}})
        router = rt.router()
        resp = router.call("heavy", [ChatMessage("user", "测试")], system=TASK_SYSTEM, json_schema=TASK_SCHEMA, clearances=[Clearance.PUBLIC])
        assert resp.json() == {"purposes": [], "issuer": None, "recipients": [], "subject": None} and srv.problems == []
        assert ("authorization" in srv.requests[0]["headers"]) == bool(key_env)
        # 各阶段新建的模型网关共用已建的适配（不为每个阶段新建 HTTP 客户端）
        assert rt.router().provider("heavy") is router.provider("heavy") is router.provider("agent")
    finally:
        srv.close()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_missing_key_is_a_configuration_error(tmp_path, mock, provider, monkeypatch):
    """显式指定的密钥变量未设置：显示“未就绪”并走确定性路径，而不是等到调用时才让任务失败。"""
    monkeypatch.delenv("GONGWEN_MOCK_MISSING_KEY", raising=False)
    eng = model_engine(tmp_path, mock, provider, api_key_env="GONGWEN_MOCK_MISSING_KEY")
    assert eng.rt.router().describe()["heavy"].startswith("未就绪") and "GONGWEN_MOCK_MISSING_KEY" in eng.rt.router().describe()["heavy"]
    user = default_user()
    st = start(eng, user)
    st = advance_auto(eng, user, st.task_id)
    assert st.stage == Stage.HUMAN_REVIEW and mock.requests == []
    ok, desc = AgentSession(eng, human=user, workspace=tmp_path).model_status()
    assert not ok and "GONGWEN_MOCK_MISSING_KEY" in desc


@pytest.mark.parametrize("provider", PROVIDERS)
def test_revision_instruction_with_model(tmp_path, mock, provider):
    """按修改意见定向修订：模型提出的修改逐句复核，新增无来源数字的修改转人工确认，不直接进入文稿。"""
    eng = model_engine(tmp_path, mock, provider)
    user = default_user()
    st = start(eng, user)
    st = run_to_review(eng, user, st.task_id)
    st = eng.request_revision(st.task_id, by=user, instruction="补充经费投入情况")
    assert mock.problems == [] and len(mock.calls("revision")) == 1
    ps = eng.store.load_model(st.task_id, "human_patch_1", PatchSet)
    assert [p.status for p in ps.patches] == ["needs_human"] and "999" in ps.patches[0].reason
    assert "999万元" not in eng.current_ir(eng.load_state(st.task_id)).full_text()


def test_cli_revise_reports_model_failure(tmp_path, mock, capsys):
    """命令行人工修订时模型接口故障：给出原因并以“处理失败”退出，而不是打印堆栈。"""
    (tmp_path / ".gongwen").mkdir()
    (tmp_path / ".gongwen" / "config.toml").write_text(
        f'[environment]\nunit_name = "示例市卫生健康委员会"\nregion = "示例省"\n[layout]\nrender_check = false\n'
        f'[model]\nprovider = "deepseek"\nname = "mock-model"\nbase_url = "{mock.url}/v1"\napi_key_env = "{KEY_ENV}"\nmax_retries = 0\n',
        encoding="utf-8",
    )
    eng = Engine(build_runtime(tmp_path))
    user = default_user()
    st = start(eng, user)
    st = run_to_review(eng, user, st.task_id)
    mock.faults["revision"] = "http500"
    capsys.readouterr()
    rc = cli(["-C", str(tmp_path), "--user", user.id, "task", "revise", st.task_id, "--instruction", "补充经费投入情况"])
    err = capsys.readouterr().err
    assert rc == 1 and "模型接口调用失败" in err and "Traceback" not in err
    assert eng.load_state(st.task_id).current_version == 1


# ====================================================================== (d) 对话代理
def _task_id_from(conv: list[Turn]) -> str | None:
    for t in reversed(conv):
        m = re.search(r'"task_id": "(T[^"]+)"', t.get("content", "") or t.get("text", ""))
        if m:
            return m.group(1)
    return None


def _since_user(conv: list[Turn]) -> list[Turn]:
    last = max(i for i, t in enumerate(conv) if t["role"] == "user")
    return conv[last + 1 :]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_chat_agent_tool_calls_and_denials(tmp_path, mock, provider):
    eng = model_engine(tmp_path, mock, provider)
    user = default_user()
    st = start(eng, user)
    st = run_to_review(eng, user, st.task_id)
    tid = st.task_id
    cp = next(c for c in st.pending_checkpoints() if c.kind == CheckpointKind.HUMAN_REVIEW)
    seen_results: list[Turn] = []

    def agent(conv):
        tail = _since_user(conv)
        step = sum(1 for t in tail if t["role"] == "assistant")
        seen_results[:] = [t for t in tail if t["role"] == "tool"]
        if step == 0:
            return [("task_status", {"task_id": tid})]
        if step == 1:
            return [
                ("checkpoint_resolve", {"task_id": tid, "cp_id": cp.cp_id, "option": "submit"}),
                ("approval_import", {"task_id": tid, "approver": "模型"}),
                ("task_advance", {"task_id": tid, "auto_accept": ["human_review"]}),
                ("revision_propose", {"task_id": tid, "instruction": "把第一段改得更简洁", "reason": "冗余"}),
            ]
        return "已提交修改建议，请用 /apply 采纳；送审节点须由您本人处理。"

    mock.agent = agent
    n_before = len(mock.requests)
    session = AgentSession(eng, human=user, workspace=tmp_path)
    assert session.model_status()[0]
    reply = session.send(f"请查看任务 {tid} 的状态，并建议把第一段改简洁")
    assert mock.problems == []
    assert reply.stopped == "" and "/apply" in reply.text
    assert [(e.name, e.ok) for e in reply.events] == [("task_status", True), ("checkpoint_resolve", False), ("approval_import", False), ("task_advance", False), ("revision_propose", True)]
    # 被拒绝的调用以错误结果回传模型（Anthropic 标记 is_error）
    errs = [t for t in seen_results if t["is_error"]]
    assert [t["name"] for t in errs] == ["checkpoint_resolve", "approval_import", "task_advance"]
    # 人工节点、审批与采纳都没有被模型代办
    st = eng.load_state(tid)
    assert any(c.cp_id == cp.cp_id for c in st.pending_checkpoints()) and st.approvals == []
    assert st.stage == Stage.HUMAN_REVIEW and st.current_version == 1
    assert [p["status"] for p in eng.proposals(tid)] == ["pending"]
    # 同一会话内系统提示与工具集保持不变（思考块绑定会话前缀；也利于提示缓存）
    agent_reqs = mock.requests[n_before:]
    assert len(agent_reqs) == 3
    if provider == "anthropic":
        assert len({_canon(r["body"]["system"]) for r in agent_reqs}) == 1
        assert len({_canon(r["body"]["tools"]) for r in agent_reqs}) == 1
        assert all(any(b["type"] == "thinking" for b in r["body"]["messages"][1]["content"]) for r in agent_reqs[1:])
    else:
        assert len({_canon(r["body"]["messages"][0]) for r in agent_reqs}) == 1
    # 下一轮对话：历史仍然合规（追加式）
    mock.agent = lambda conv: "好的。"
    assert session.send("谢谢").text == "好的。" and mock.problems == []


@pytest.mark.parametrize("provider", PROVIDERS)
def test_chat_agent_creates_task_and_history_stays_valid(tmp_path, mock, provider):
    """模型创建任务后，本轮涉及的任务改为在下一条用户消息中告知，而不是改写系统提示。"""
    (tmp_path / "情况说明.md").write_text("\n\n".join(SITUATION), encoding="utf-8")
    (tmp_path / "经费测算表.csv").write_text("\n".join(",".join(str(c) for c in r) for r in BUDGET), encoding="utf-8")

    def agent(conv):
        tail = _since_user(conv)
        step = sum(1 for t in tail if t["role"] == "assistant")
        if conv[-1]["role"] == "user" and "谢谢" in conv[-1]["text"]:
            return "不客气。"
        if step == 0:
            return [("task_create", {"request": "写一份向主管部门申请基层医疗示范点建设经费的报告", "recipients": "示例市人民政府", "issuer_type": "政府部门"})]
        tid = _task_id_from(tail)
        if step == 1:
            return [("material_add", {"task_id": tid, "path": "情况说明.md"}), ("task_status", {"task_id": tid})]
        return "材料需要您确认属性：请输入 /confirm。"

    mock.agent = agent
    eng = model_engine(tmp_path, mock, provider)
    session = AgentSession(eng, human=default_user(), workspace=tmp_path)
    reply = session.send("帮我写个申请经费的报告")
    assert mock.problems == [] and "/confirm" in reply.text
    assert [e.name for e in reply.events] == ["task_create", "material_add", "task_status"]
    tid = next(iter(session.task_ids))
    reply = session.send("谢谢")
    assert mock.problems == [] and reply.text == "不客气。"
    last = mock.requests[-1]["body"]["messages"]
    user_text = last[-1]["content"] if isinstance(last[-1]["content"], str) else last[-1]["content"][-1]["text"]
    assert tid in user_text  # 新涉及的任务随用户消息告知模型
    system = [r["body"]["system"] if provider == "anthropic" else r["body"]["messages"][0]["content"] for r in mock.requests]
    assert len(set(system)) == 1 and tid not in system[0]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_chat_agent_survives_call_failures(tmp_path, mock, provider):
    eng = model_engine(tmp_path, mock, provider)
    session = AgentSession(eng, human=default_user(), workspace=tmp_path)
    mock.faults["agent"] = "http500"
    reply = session.send("你好")
    assert reply.stopped and "模型接口调用失败" in reply.text
    mock.faults["agent"] = Reply(200, {}, delay=3)
    eng.rt.config.model.timeout = 0.5
    session2 = AgentSession(eng, human=default_user(), workspace=tmp_path)
    reply = session2.send("你好")
    assert reply.stopped and "模型接口调用失败" in reply.text
    # 故障恢复后会话可以继续，历史仍然合规
    mock.faults.clear()
    mock.agent = lambda conv: "在。"
    assert session.send("还在吗").text == "在。" and mock.problems == []


def test_cli_chat_keeps_running_after_failures(tmp_path, mock, monkeypatch, capsys):
    """gongwen chat：接口故障与意外异常都只报告，不结束对话。"""
    (tmp_path / ".gongwen").mkdir()
    (tmp_path / ".gongwen" / "config.toml").write_text(
        f'[model]\nprovider = "deepseek"\nname = "mock-model"\nbase_url = "{mock.url}/v1"\napi_key_env = "{KEY_ENV}"\nmax_retries = 0\n', encoding="utf-8"
    )
    lines = iter(["第一句", "第二句", "第三句"])

    def fake_input(prompt=""):
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)
    original, n = AgentSession.send, {"calls": 0}

    def send(self, text):
        n["calls"] += 1
        if n["calls"] == 2:
            raise RuntimeError("模拟的内部错误")
        return original(self, text)

    monkeypatch.setattr(AgentSession, "send", send)
    first = {"done": False}

    def agent_fault(body):
        if not first["done"]:
            first["done"] = True
            return "http500"
        return None

    mock.faults["agent"] = agent_fault
    mock.agent = lambda conv: "恢复了。"
    assert cli(["-C", str(tmp_path), "--user", "tester", "chat"]) == 0
    out = capsys.readouterr().out
    assert "模型接口调用失败" in out and "模拟的内部错误" in out and "恢复了。" in out
    assert mock.problems == []


def test_chat_agent_does_not_run_truncated_tool_calls(tmp_path, mock):
    """输出因长度上限被截断时，不执行可能不完整的工具调用。"""
    eng = model_engine(tmp_path, mock, "deepseek")

    def truncated(body):
        call = {"id": "call_1", "type": "function", "function": {"name": "revision_propose", "arguments": "{\"task_id\": \"T1\", \"instruction\": \"把第一段"}}
        return Reply(200, mock.oa_completion(body, content=None, tool_calls=[call], finish="length"))

    mock.faults["agent"] = truncated
    session = AgentSession(eng, human=default_user(), workspace=tmp_path)
    reply = session.send("改一下第一段")
    assert reply.stopped and reply.events == []
    mock.faults.clear()
    assert session.send("继续").text and mock.problems == []


def test_chat_agent_empty_reply_is_not_silent_success(tmp_path, mock):
    pytest.importorskip("anthropic")
    eng = model_engine(tmp_path, mock, "anthropic")
    mock.faults["agent"] = lambda body: mock.an_message(body, [], "end_turn", think=False)  # 空内容
    session = AgentSession(eng, human=default_user(), workspace=tmp_path)
    reply = session.send("你好")
    assert reply.stopped and reply.text
    mock.faults.clear()
    assert session.send("你好").text and mock.problems == []  # 空的助手消息不会进入历史


def test_chat_agent_compaction_strips_bound_thinking(tmp_path, mock):
    """上下文过长时替换较早的工具结果（改写了历史）：此后不再回传绑定原会话的思考块。"""
    pytest.importorskip("anthropic")
    eng = model_engine(tmp_path, mock, "anthropic")
    def agent(conv):
        tail = _since_user(conv)
        if sum(1 for t in tail if t["role"] == "assistant") < 3:
            return [("genre_guide", {"genre": "通知"})]
        return "查询完毕。"

    mock.agent = agent
    session = AgentSession(eng, human=default_user(), workspace=tmp_path)
    assert session.send("通知怎么写").text == "查询完毕。"
    session._compact(budget_chars=1_000)
    assert any("已省略" in m.content for m in session.messages if m.role == "tool")
    mock.agent = lambda conv: "好的。"
    assert session.send("继续").text == "好的。" and mock.problems == []


def test_anthropic_mid_stream_fallback_echo(tmp_path, mock):
    """流式中途回退：边界之前被拒答模型的工具调用不执行、思考块不回传；回退块本身不回传。"""
    pytest.importorskip("anthropic")
    eng = model_engine(tmp_path, mock, "anthropic")
    state = {"n": 0}

    def fallback(body):
        state["n"] += 1
        if state["n"] > 1:
            return None
        blocks = [
            mock.thinking(body),
            {"type": "text", "text": "先查看"},
            {"type": "tool_use", "id": "toolu_declined", "name": "task_list", "input": {"limit": 50}},
            {"type": "fallback", "from": {"model": "mock-model"}, "to": {"model": "mock-fallback"}},
            {"type": "text", "text": "任务列表"},
            {"type": "tool_use", "id": "toolu_kept", "name": "task_list", "input": {"limit": 5}},
        ]
        return mock.an_message(body, blocks, "tool_use", think=False)

    mock.faults["agent"] = fallback
    mock.agent = lambda conv: "没有任务。"
    session = AgentSession(eng, human=default_user(), workspace=tmp_path)
    reply = session.send("列出任务")
    assert [e.args for e in reply.events] == [{"limit": 5}]
    assert reply.text == "没有任务。" and mock.problems == []


# ====================================================================== (e) 出网边界
def test_non_allowlisted_host_refused_before_network(tmp_path, monkeypatch):
    other = MockLLM(host="127.0.0.2")
    try:
        eng = model_engine(tmp_path, other, "deepseek")
        user = default_user()
        st = start(eng, user)
        st = advance_auto(eng, user, st.task_id)
        assert st.stage == Stage.HUMAN_REVIEW and other.requests == []
        router = eng.rt.router()
        with pytest.raises(ModelUnavailable, match="白名单"):
            router.call("heavy", [ChatMessage("user", "测试")], clearances=[Clearance.PUBLIC])
        session = AgentSession(eng, human=user, workspace=tmp_path)
        ok, desc = session.model_status()
        assert not ok and other.requests == []
        # 对照：加入白名单后同一主机可以访问（证明上面是网关拦截，而不是网络不通）
        eng2 = model_engine(tmp_path / "w2", other, "deepseek", egress={"allowed_hosts": ["127.0.0.2"]})
        eng2.rt.router().call("heavy", [ChatMessage("user", "测试")], clearances=[Clearance.PUBLIC])
        assert len(other.requests) == 1
    finally:
        other.close()


def test_lan_mdns_host_is_not_local():
    gw = EgressGateway(EnvironmentRoute.PUBLIC_DEV)
    assert not gw.evaluate(EgressRequest("http://gpu-box.local:8000/v1", "draft")).allowed
    assert gw.evaluate(EgressRequest("http://localhost:11434/v1", "draft")).allowed
    assert EgressGateway(EnvironmentRoute.PUBLIC_DEV, ["gpu-box.local"]).evaluate(EgressRequest("http://gpu-box.local:8000/v1", "draft")).allowed


@pytest.mark.parametrize("provider", PROVIDERS)
def test_redirect_to_other_host_is_not_followed(tmp_path, mock, provider):
    other = MockLLM(host="127.0.0.2")
    try:
        path = "/v1/messages" if provider == "anthropic" else "/v1/chat/completions"
        mock.faults["drafting"] = Reply(307, {}, headers={"Location": other.url + path})
        eng = model_engine(tmp_path, mock, provider)
        user = default_user()
        st = start(eng, user)
        st = advance_auto(eng, user, st.task_id)
        assert other.requests == []  # 材料不会经重定向发往未获准的主机
        assert st.stage == Stage.FAILED and "模型接口调用失败" in st.exception_reason
        with pytest.raises(ModelCallFailed, match="HTTP 307，重定向至 http://127.0.0.2"):
            eng.rt.router().call("heavy", [ChatMessage("user", "测试")], system="你是中国内地公文起草助手", clearances=[Clearance.PUBLIC])
        assert other.requests == []
    finally:
        other.close()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_local_model_bypasses_environment_proxy(tmp_path, mock, provider, monkeypatch):
    """本机模型：网关按“本机”放行，请求不得经环境变量中的代理绕到其他主机。"""
    proxy = MockLLM(host="127.0.0.2", api_key=None)
    try:
        for k in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(k, raising=False)
        for k in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(k, proxy.url)
        eng = model_engine(tmp_path, mock, provider)
        eng.rt.router().call("heavy", [ChatMessage("user", "本机模型测试")], system=TASK_SYSTEM, json_schema=TASK_SCHEMA, clearances=[Clearance.PUBLIC]).json()
        assert proxy.requests == [] and len(mock.requests) == 1
    finally:
        proxy.close()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_internal_material_never_reaches_public_model(tmp_path, mock, provider):
    marker = "内部资料 注意保存"
    secret_line = "全院开展安全检查，重点检查第三住院楼消防通道。"
    eng = model_engine(tmp_path, mock, provider, env={"route": "unit_approved", "accept_internal_materials": True})
    user = default_user()
    st = eng.create_task("起草一份开展安全检查的通知", by=user, hints={"recipients": "各科室"})
    eng.add_material(st.task_id, "内部安排.txt", f"{marker}\n{secret_line}".encode(), by=user, declared=Clearance.INTERNAL)
    st = advance_auto(eng, user, st.task_id)
    assert mock.requests == []  # 公共模型只获准处理公开材料：整个流程一次也不调用
    # 对话代理：模型读取内部任务后，后续调用被网关拒绝，内部内容不出本机
    mock.agent = lambda conv: [("draft_view", {"task_id": st.task_id})] if conv[-1]["role"] == "user" else "看过了。"
    session = AgentSession(eng, human=user, workspace=tmp_path)
    reply = session.send("看看最新的任务稿")
    assert [e.name for e in reply.events] == ["draft_view"] and reply.events[0].ok
    assert reply.stopped and "出网网关拒绝" in reply.stopped
    assert len(mock.requests) == 1 and not mock.seen("消防通道") and not mock.seen(marker)
    assert mock.problems == []

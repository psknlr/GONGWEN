"""模型接入的统一抽象。

模型只在允许的范围内提出计划、组织文稿或提交修改建议；任务状态、工具权限、预算、
审批与失败退出由程序化状态机控制（设计 §3.1）。因此本层只做三件事：
1. 把不同服务商的接口统一为 complete()；
2. 让每次调用都经过出网网关、预算与审计日志（见 router.py）；
3. 对需要结构化输出的调用给出 JSON 结果，交由确定性校验器复核。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..schemas.common import Clearance


class ModelUnavailable(RuntimeError):
    """未配置可用模型，或当前材料属性不允许出网：调用方应走确定性路径。"""


class ModelRefused(RuntimeError):
    """模型拒绝回答（安全分类器等）。调用方应显式失败或转人工，不得把空结果当成功。"""


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatMessage:
    role: str  # system / user / assistant / tool
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    raw: Any = None  # 服务商原生的助手消息（如包含思考块的内容列表），用于原样回传


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ModelResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    raw_assistant: Any = None
    refused: bool = False

    def json(self) -> Any:
        return parse_json(self.text)


class ModelProvider(Protocol):
    name: str
    model: str
    endpoint: str
    max_clearance: Clearance

    def complete(
        self,
        messages: list[ChatMessage],
        system: str | None = None,
        tools: list[ToolDef] | None = None,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ModelResponse: ...


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json(text: str) -> Any:
    """宽容解析模型返回的 JSON（去除代码围栏、截取首个对象/数组）。失败时抛 ValueError。"""
    t = text.strip()
    m = _FENCE.search(t)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        s, e = t.find(open_ch), t.rfind(close_ch)
        if s != -1 and e > s:
            try:
                return json.loads(t[s : e + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("模型输出不是有效 JSON")

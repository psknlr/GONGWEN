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
from urllib.parse import urlparse

from ..harness.egress import is_local_host
from ..schemas.common import Clearance


class ModelUnavailable(RuntimeError):
    """未配置可用模型，或当前材料属性不允许出网：调用方应走确定性路径。"""


class ModelRefused(RuntimeError):
    """模型拒绝回答（安全分类器等）。调用方应显式失败或转人工，不得把空结果当成功。"""


class ModelCallFailed(RuntimeError):
    """模型接口调用失败：网络错误、超时、HTTP 错误、重定向或响应报文异常（已按配置重试）。

    与 ModelUnavailable（未配置或出网网关不允许，按设计走确定性路径）不同，这是运行时故障：
    调用方应显式失败或告知用户，不能当作“未配置模型”悄悄改走其他路径。"""


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
    schema: dict[str, Any] | None = None  # 请求时约定的输出结构（由模型网关填入）

    def json(self) -> Any:
        """解析结构化输出，并按约定结构核对类型：结构不符与无效 JSON 一样抛 ValueError（由技能记录后回退）。"""
        data = parse_json(self.text)
        if self.schema is not None:
            err = schema_mismatch(data, self.schema)
            if err:
                raise ValueError(f"模型输出不符合约定的结构：{err}")
        return data


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


def proxy_overrides(base_url: str) -> dict[str, None] | None:
    """本机模型不经环境变量中的代理（出网网关按“本机”放行，请求不得绕到代理主机）：返回覆盖代理的 HTTP 挂载表。"""
    return {"all://": None, "http://": None, "https://": None} if is_local_host(urlparse(base_url).hostname) else None


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


_JSON_TYPES: dict[str, type | tuple[type, ...]] = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _is_type(value: Any, name: str) -> bool:
    if name in ("integer", "number"):
        ok = (int,) if name == "integer" else (int, float)
        return isinstance(value, ok) and not isinstance(value, bool)
    py = _JSON_TYPES.get(name)
    return py is None or isinstance(value, py)


def schema_mismatch(value: Any, schema: dict[str, Any], path: str = "$") -> str | None:
    """按 JSON Schema 的类型结构做最小核对（类型、已出现的对象属性、数组元素），返回第一处不符的位置。

    服务商的 JSON 模式只保证“是 JSON”，不保证结构；技能读取时依赖这些类型（如段落须为对象）。
    必填、枚举等语义约束不在此核对，由技能自己的确定性校验处理。"""
    if not isinstance(schema, dict):
        return None
    types = schema.get("type")
    if types is not None:
        names = types if isinstance(types, list) else [types]
        if not any(_is_type(value, t) for t in names):
            return f"{path} 应为 {'/'.join(names)}"
    if isinstance(value, dict):
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                err = schema_mismatch(value[key], sub, f"{path}.{key}")
                if err:
                    return err
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            err = schema_mismatch(item, schema["items"], f"{path}[{i}]")
            if err:
                return err
    return None

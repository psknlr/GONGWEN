"""脚本化模型：按预设顺序或函数返回响应，用于测试、评测回放和离线演示。

它与真实模型走完全相同的治理路径（出网判定、预算、审计、输出校验），
因此可以验证“模型输出了虚构数字时，校验器能否拦住”。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..schemas.common import Clearance
from .base import ChatMessage, ModelResponse, ToolDef, Usage

Responder = Callable[[list[ChatMessage], str | None, list[ToolDef] | None, dict | None], ModelResponse | str | dict]


class ScriptedProvider:
    def __init__(
        self,
        responses: list[ModelResponse | str | dict] | None = None,
        responder: Responder | None = None,
        max_clearance: Clearance = Clearance.PUBLIC,
        endpoint: str = "http://localhost/scripted",
        model: str = "scripted",
    ):
        self.name = "scripted"
        self.model = model
        self.endpoint = endpoint
        self.max_clearance = max_clearance
        self.queue = list(responses or [])
        self.responder = responder
        self.calls: list[dict[str, Any]] = []

    def complete(self, messages, system=None, tools=None, json_schema=None, max_tokens=None, temperature=None) -> ModelResponse:
        self.calls.append({"messages": messages, "system": system, "tools": [t.name for t in tools or []], "json_schema": json_schema})
        if self.responder is not None:
            r = self.responder(messages, system, tools, json_schema)
        elif self.queue:
            r = self.queue.pop(0)
        else:
            r = ""
        if isinstance(r, ModelResponse):
            return r
        if isinstance(r, (dict, list)):
            r = json.dumps(r, ensure_ascii=False)
        prompt_len = sum(len(m.content) for m in messages) + len(system or "")
        return ModelResponse(text=str(r), stop_reason="end_turn", usage=Usage(prompt_len // 2, len(str(r)) // 2), model=self.model)

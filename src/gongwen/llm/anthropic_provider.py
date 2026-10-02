"""Anthropic Claude 适配（使用官方 anthropic SDK，可选依赖：pip install "gongwen[anthropic]"）。

要点：
* 默认模型 claude-opus-5-5；思考默认自适应开启，不发送 thinking 参数，用 output_config.effort 控制深度；
* 结构化输出使用 output_config.format（json_schema），工具使用 strict 模式；
* 默认启用服务端拒答回退（fallbacks="default"，beta server-side-fallback-2026-07-01），
  并在读取内容前先检查 stop_reason == "refusal"；
* 工具调用轮次中原样回传 response.content（含思考块），保证多轮一致。
"""

from __future__ import annotations

import os
from typing import Any

from ..schemas.common import Clearance
from .base import ChatMessage, ModelResponse, ToolCall, ToolDef, Usage

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


_UNSUPPORTED = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength")


def _strictify(schema: dict[str, Any]) -> dict[str, Any]:
    """严格模式要求每个对象 additionalProperties=false 且列出 required；
    数值、长度约束不在结构化输出支持范围内，改写进 description，由本地校验兜底。"""
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    dropped = [f"{k}={out.pop(k)}" for k in _UNSUPPORTED if k in out]
    if dropped:
        out["description"] = (out.get("description", "") + f"（约束：{'，'.join(dropped)}）").strip()
    if out.get("type") == "object":
        props = out.get("properties", {})
        out["properties"] = {k: _strictify(v) for k, v in props.items()}
        out.setdefault("required", list(props))
        out["additionalProperties"] = False
    if out.get("type") == "array" and "items" in out:
        out["items"] = _strictify(out["items"])
    return out


class AnthropicProvider:
    def __init__(
        self,
        model: str = "",
        api_key_env: str = "ANTHROPIC_API_KEY",
        base_url: str = "",
        max_clearance: Clearance = Clearance.PUBLIC,
        timeout: float = 120.0,
        max_tokens: int = 16000,
        effort: str = "high",
        use_fallbacks: bool = True,
        client: Any = None,
    ):
        self.name = "anthropic"
        self.model = model or DEFAULT_MODEL
        self.max_clearance = max_clearance
        self.max_tokens = max_tokens
        self.effort = effort
        self.use_fallbacks = use_fallbacks
        self.base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        if client is not None:
            self.client = client
        else:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - 可选依赖
                raise ValueError('使用 Claude 需要安装可选依赖：pip install "gongwen[anthropic]"') from exc
            kwargs: dict[str, Any] = {"timeout": timeout}
            key = os.environ.get(api_key_env) if api_key_env else None
            if key:
                kwargs["api_key"] = key
            if base_url:
                kwargs["base_url"] = base_url
            self.client = anthropic.Anthropic(**kwargs)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/v1/messages"

    @staticmethod
    def _convert(messages: list[ChatMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        pending_results: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "tool":
                # 并行工具调用的结果必须放在同一条 user 消息中返回
                pending_results.append({"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content, **({"is_error": True} if m.content.startswith("错误：") else {})})
                continue
            if pending_results:
                out.append({"role": "user", "content": pending_results})
                pending_results = []
            if m.role == "assistant":
                if m.raw is not None:
                    out.append({"role": "assistant", "content": m.raw})
                else:
                    blocks: list[dict[str, Any]] = []
                    if m.content:
                        blocks.append({"type": "text", "text": m.content})
                    for c in m.tool_calls:
                        blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments})
                    out.append({"role": "assistant", "content": blocks or m.content})
            elif m.role == "user":
                out.append({"role": "user", "content": m.content})
        if pending_results:
            out.append({"role": "user", "content": pending_results})
        return out

    def complete(
        self,
        messages: list[ChatMessage],
        system: str | None = None,
        tools: list[ToolDef] | None = None,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,  # 当前模型不接受采样参数，忽略
    ) -> ModelResponse:
        output_config: dict[str, Any] = {"effort": self.effort}
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": _strictify(json_schema)}
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self.max_tokens,
            "messages": self._convert(messages),
            "output_config": output_config,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": _strictify(t.parameters), "strict": True} for t in tools]
        if self.use_fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        response = self.client.beta.messages.create(**kwargs)
        usage = getattr(response, "usage", None)
        result = ModelResponse(
            stop_reason=response.stop_reason or "",
            usage=Usage(getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0),
            model=getattr(response, "model", self.model),
            raw_assistant=response.content,
        )
        # 先检查拒答，再读取内容
        if response.stop_reason == "refusal":
            result.refused = True
            return result
        texts = []
        for block in response.content:
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "tool_use":
                result.tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {})))
        result.text = "".join(texts)
        return result

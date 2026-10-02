"""OpenAI 兼容接口适配：DeepSeek、xAI Grok、智谱 GLM、通义千问、Kimi，以及本地 Ollama / vLLM。

这些服务都提供 /chat/completions 端点，工具调用采用 function 形式。
模型名称请按服务商当前可用型号配置；下列默认值仅作示例。
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from ..schemas.common import Clearance
from .base import ChatMessage, ModelResponse, ToolCall, ToolDef, Usage

PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {"base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY", "model": "deepseek-chat"},
    "xai": {"base_url": "https://api.x.ai/v1", "api_key_env": "XAI_API_KEY", "model": "grok-4"},
    "zhipu": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "api_key_env": "ZHIPUAI_API_KEY", "model": "glm-4.6"},
    "qwen": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key_env": "DASHSCOPE_API_KEY", "model": "qwen-plus"},
    "moonshot": {"base_url": "https://api.moonshot.cn/v1", "api_key_env": "MOONSHOT_API_KEY", "model": "moonshot-v1-32k"},
    "ollama": {"base_url": "http://localhost:11434/v1", "api_key_env": "", "model": ""},
    "vllm": {"base_url": "http://localhost:8000/v1", "api_key_env": "", "model": ""},
    "openai_compat": {"base_url": "", "api_key_env": "OPENAI_COMPAT_API_KEY", "model": ""},
}


class OpenAICompatProvider:
    def __init__(
        self,
        provider: str,
        model: str = "",
        base_url: str = "",
        api_key_env: str = "",
        max_clearance: Clearance = Clearance.PUBLIC,
        timeout: float = 90.0,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        extra_headers: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        preset = PRESETS.get(provider, PRESETS["openai_compat"])
        self.name = provider
        self.model = model or preset["model"]
        self.base_url = (base_url or preset["base_url"]).rstrip("/")
        if not self.base_url:
            raise ValueError(f"{provider} 需要配置 base_url")
        if not self.model:
            raise ValueError(f"{provider} 需要配置模型名称 model.name")
        key_env = api_key_env or preset["api_key_env"]
        self.api_key = os.environ.get(key_env, "") if key_env else ""
        if key_env and not self.api_key:
            raise ValueError(f"未设置环境变量 {key_env}")
        self.max_clearance = max_clearance
        self.temperature = temperature
        self.max_tokens = max_tokens
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self.client = httpx.Client(timeout=timeout, headers=headers, transport=transport)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    @staticmethod
    def _msg(m: ChatMessage) -> dict[str, Any]:
        if m.role == "tool":
            return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
        out: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            out["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
                for c in m.tool_calls
            ]
        return out

    def complete(
        self,
        messages: list[ChatMessage],
        system: str | None = None,
        tools: list[ToolDef] | None = None,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ModelResponse:
        msgs = []
        sys_text = system or ""
        if json_schema is not None:
            sys_text += (
                "\n\n只输出一个 JSON 对象，不要输出任何其他文字。JSON 必须符合以下 JSON Schema：\n"
                + json.dumps(json_schema, ensure_ascii=False)
            )
        if sys_text:
            msgs.append({"role": "system", "content": sys_text.strip()})
        msgs += [self._msg(m) for m in messages]
        body: dict[str, Any] = {
            "model": self.model,
            "messages": msgs,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in tools]
            body["tool_choice"] = "auto"
        if json_schema is not None and not tools:
            body["response_format"] = {"type": "json_object"}
        resp = self.client.post(self.endpoint, json=body)
        if resp.status_code >= 400:
            raise RuntimeError(f"{self.name} 接口返回 {resp.status_code}：{resp.text[:300]}")
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_invalid_json": fn.get("arguments", "")}
            calls.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args))
        usage = data.get("usage") or {}
        finish = choice.get("finish_reason") or ""
        return ModelResponse(
            text=msg.get("content") or "",
            tool_calls=calls,
            stop_reason=finish,
            usage=Usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)),
            model=data.get("model", self.model),
            raw_assistant=None,
            refused=finish == "content_filter",
        )

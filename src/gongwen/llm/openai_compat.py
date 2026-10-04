"""OpenAI 兼容接口适配：DeepSeek、xAI Grok、智谱 GLM、通义千问、Kimi，以及本地 Ollama / vLLM。

这些服务都提供 /chat/completions 端点，工具调用采用 function 形式。
模型名称请按服务商当前可用型号配置；下列默认值仅作示例。

出网约束：只向出网网关核准的地址发送请求——不跟随重定向（否则材料可能被转发到未获准的主机）；
本机模型不经环境变量中的代理（网关按“本机”放行，请求不得绕到其他主机）。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import httpx

from ..schemas.common import Clearance
from .base import ChatMessage, ModelCallFailed, ModelResponse, ToolCall, ToolDef, Usage, proxy_overrides

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

# 可重试的状态码：请求超时、冲突、限流与服务端错误
RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


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
        max_retries: int = 2,
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
        self.max_retries = max(0, max_retries)
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self.client = httpx.Client(timeout=timeout, headers=headers, transport=transport, follow_redirects=False, mounts=proxy_overrides(self.base_url))

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

    def _redact(self, text: str) -> str:
        return text.replace(self.api_key, "***") if self.api_key else text

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        """发送请求；限流、服务端错误、连接失败与超时按配置重试，仍失败则抛 ModelCallFailed。"""
        err = ModelCallFailed("未发出请求")
        for attempt in range(self.max_retries + 1):
            wait = min(0.5 * 2**attempt, 8.0)
            try:
                resp = self.client.post(self.endpoint, json=body)
            except httpx.TimeoutException as exc:
                err = ModelCallFailed(f"请求超时（{type(exc).__name__}）")
            except httpx.TransportError as exc:
                err = ModelCallFailed(f"连接失败（{type(exc).__name__}）：{self._redact(str(exc))[:200]}")
            else:
                if resp.is_success:
                    return resp
                # 3xx 同样视为失败：不跟随重定向，材料只发往网关核准的地址
                where = f"，重定向至 {resp.headers.get('location', '')}" if resp.is_redirect else ""
                err = ModelCallFailed(f"HTTP {resp.status_code}{where}：{self._redact(resp.text)[:200]}")
                if resp.status_code not in RETRY_STATUS:
                    raise err
                try:
                    wait = min(float(resp.headers.get("retry-after", wait)), 8.0)
                except ValueError:
                    pass
            if attempt < self.max_retries:
                time.sleep(max(wait, 0.0))
        raise err

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
        resp = self._post(body)
        try:
            data = resp.json()
        except ValueError as exc:
            raise ModelCallFailed(f"响应不是 JSON（Content-Type: {resp.headers.get('content-type', '')}）") from exc
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices or not isinstance(choices[0], dict) or not isinstance(choices[0].get("message"), dict):
            # 部分服务以 HTTP 200 返回错误对象：不能当作模型输出了空文本
            err = data.get("error") if isinstance(data, dict) else None
            raise ModelCallFailed("响应缺少 choices" + (f"：{self._redact(str(err))[:200]}" if err else ""))
        choice = choices[0]
        msg = choice["message"]
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments")
            if isinstance(raw, dict):  # 个别服务直接返回对象而非 JSON 字符串
                args: Any = raw
            else:
                try:
                    args = json.loads(raw or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = None
            if not isinstance(args, dict):
                args = {"_invalid_json": str(raw)}  # 交由工具管线按参数无效拒绝
            # 缺少编号时补发唯一编号：工具结果必须能对应到这次调用
            calls.append(ToolCall(id=tc.get("id") or f"call_{uuid.uuid4().hex[:16]}", name=fn.get("name", ""), arguments=args))
        usage = data.get("usage") or {}
        finish = choice.get("finish_reason") or ""
        content = msg.get("content")
        if isinstance(content, list):  # 内容分段返回
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return ModelResponse(
            text=content or "",
            tool_calls=calls,
            stop_reason=finish,
            usage=Usage(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)),
            model=data.get("model") or self.model,
            raw_assistant=None,
            refused=finish == "content_filter",
        )

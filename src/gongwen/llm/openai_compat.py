"""OpenAI 兼容接口适配：OpenAI GPT、DeepSeek、智谱 GLM、MiniMax、通义千问、Kimi、xAI Grok，以及本地 Ollama / vLLM。

这些服务都提供 /chat/completions 端点，工具调用采用 function 形式。各服务商的地址、密钥变量、默认型号与
接口差异见 presets.py；型号名称请按服务商当前可用型号配置（gongwen model remote 可查询）。

接口差异的处理：
* 输出上限参数名（推理模型用 max_completion_tokens）、是否发送 temperature 及取值范围（发送前夹到允许区间）；
* JSON 模式：json_schema / json_object / none（不发送 response_format，只靠提示约束与宽容解析）；
  服务端以 HTTP 400 明确拒绝 response_format（或 temperature、max_tokens）时，去掉（或改名）该参数重试一次，
  并在本适配的生命周期内记住，不再发送；
* 思考内容：响应中的 reasoning_content 一律忽略（不作为答案、不进入日志）；正文夹带的 <think>…</think>
  在解析前去除。

出网约束：只向出网网关核准的地址发送请求——不跟随重定向（否则材料可能被转发到未获准的主机）；
本机模型不经环境变量中的代理（网关按“本机”放行，请求不得绕到其他主机）。错误信息中的密钥一律打码。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import httpx

from ..schemas.common import Clearance
from . import presets as _presets
from .base import ChatMessage, ModelCallFailed, ModelListUnsupported, ModelResponse, ToolCall, ToolDef, Usage, proxy_overrides, strip_reasoning

# 兼容旧接口：{预设名: {base_url, api_key_env, model}}（完整定义见 presets.py）
PRESETS: dict[str, dict[str, str]] = {
    n: {"base_url": p.base_url, "api_key_env": p.api_key_env, "model": p.heavy} for n, p in _presets.PRESETS.items() if p.protocol == "openai_compat"
}

# 可重试的状态码：请求超时、冲突、限流与服务端错误
RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
# 服务端以 400 明确拒绝时可以去掉的参数：(请求体字段, 错误信息中的关键词)
_DROPPABLE = (("response_format", ("response_format", "json_schema", "json_object")), ("temperature", ("temperature",)))
# extra_body 不得覆盖的字段（适配层不处理流式响应）
_RESERVED = frozenset({"model", "messages", "tools", "tool_choice", "stream"})


class ModelHTTPError(ModelCallFailed):
    """HTTP 非成功状态（内容已打码）。status 供上层区分密钥无效、型号不存在等情形。"""

    def __init__(self, status: int, message: str, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = detail


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
        thinking: bool | None = None,
        json_mode: str = "",
        extra_body: dict[str, Any] | None = None,
    ):
        preset = _presets.resolve(provider)
        if preset is None or preset.protocol != "openai_compat":
            preset = _presets.generic()  # 未知名称按通用 OpenAI 兼容接口处理（须给出 base_url 与型号）
            self.name = provider
        else:
            self.name = preset.name
        self.preset = preset
        self.model = model or preset.heavy
        self.base_url = (base_url or preset.base_url).rstrip("/")
        if not self.base_url:
            raise ValueError(f"{provider} 需要配置 base_url")
        if not self.model:
            raise ValueError(f"{provider} 需要配置模型名称 model.name")
        key_env = api_key_env or preset.api_key_env
        self.api_key_env = key_env
        self.api_key = os.environ.get(key_env, "") if key_env else ""
        if key_env and not self.api_key:
            raise ValueError(f"未设置环境变量 {key_env}")
        self.quirks = preset.quirks_for(self.model)
        if json_mode and json_mode not in _presets.JSON_MODES:
            raise ValueError(f"json_mode 应为 {' / '.join(_presets.JSON_MODES)}，或留空按预设")
        self.json_mode = json_mode or self.quirks.json_mode
        if thinking is not None and not self.quirks.thinking_switch:
            raise ValueError(f"{self.name} 不支持思考开关（thinking）；如服务商另有参数，请用 extra_body 设置")
        self.thinking = thinking
        self.extra_body = {**self.quirks.extra_body, **(extra_body or {})}
        self.max_clearance = max_clearance
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max(0, max_retries)
        self._token_param = self.quirks.token_param
        self._dropped: set[str] = set()  # 服务端已明确拒绝、此后不再发送的参数
        self.last_json_mode = ""  # 最近一次结构化调用实际使用的方式：json_schema / json_object / prompt
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self.client = httpx.Client(timeout=timeout, headers=headers, transport=transport, follow_redirects=False, mounts=proxy_overrides(self.base_url))

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    @property
    def models_endpoint(self) -> str:
        return f"{self.base_url}/models"

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

    def _http_error(self, resp: httpx.Response) -> ModelHTTPError:
        # 3xx 同样视为失败：不跟随重定向，材料只发往网关核准的地址
        where = f"，重定向至 {resp.headers.get('location', '')}" if resp.is_redirect else ""
        text = self._redact(resp.text)
        return ModelHTTPError(resp.status_code, f"HTTP {resp.status_code}{where}：{text[:200]}", text[:2000])

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
                err = self._http_error(resp)
                if resp.status_code not in RETRY_STATUS:
                    raise err
                try:
                    wait = min(float(resp.headers.get("retry-after", wait)), 8.0)
                except ValueError:
                    pass
            if attempt < self.max_retries:
                time.sleep(max(wait, 0.0))
        raise err

    def _relax(self, body: dict[str, Any], detail: str) -> bool:
        """服务端以 400 明确指出不接受的参数：去掉 response_format / temperature，或把 max_tokens 改为
        max_completion_tokens。返回是否做了调整（每个参数至多调整一次，并在本适配内记住）。"""
        low = detail.lower()
        for key, words in _DROPPABLE:
            if key in body and any(w in low for w in words):
                body.pop(key)
                self._dropped.add(key)
                return True
        if "max_tokens" in body and "max_completion_tokens" in low:
            body["max_completion_tokens"] = body.pop("max_tokens")
            self._token_param = "max_completion_tokens"
            return True
        return False

    def _send(self, body: dict[str, Any]) -> httpx.Response:
        for _ in range(len(_DROPPABLE) + 1):
            try:
                return self._post(body)
            except ModelHTTPError as exc:
                if exc.status != 400 or not self._relax(body, exc.detail):
                    raise
        return self._post(body)

    def _clamp(self, value: float) -> float:
        lo, hi = self.quirks.temperature_range
        return min(max(float(value), lo), hi)

    def build_body(
        self,
        messages: list[ChatMessage],
        system: str | None = None,
        tools: list[ToolDef] | None = None,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
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
        body: dict[str, Any] = {"model": self.model, "messages": msgs}
        if self.quirks.send_temperature and "temperature" not in self._dropped:
            body["temperature"] = self._clamp(self.temperature if temperature is None else temperature)
        body[self._token_param] = max_tokens or self.max_tokens
        if self.thinking is not None:
            body["thinking"] = {"type": "enabled" if self.thinking else "disabled"}
        for k, v in self.extra_body.items():
            if k not in _RESERVED:
                body[k] = v
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in tools]
            body["tool_choice"] = "auto"
        if json_schema is not None and not tools and "response_format" not in self._dropped:
            if self.json_mode == "json_schema":
                body["response_format"] = {"type": "json_schema", "json_schema": {"name": "gongwen_output", "schema": json_schema, "strict": False}}
            elif self.json_mode == "json_object":
                body["response_format"] = {"type": "json_object"}
        return body

    def complete(
        self,
        messages: list[ChatMessage],
        system: str | None = None,
        tools: list[ToolDef] | None = None,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ModelResponse:
        body = self.build_body(messages, system=system, tools=tools, json_schema=json_schema, max_tokens=max_tokens, temperature=temperature)
        resp = self._send(body)
        if json_schema is not None and not tools:
            self.last_json_mode = (body.get("response_format") or {}).get("type") or "prompt"
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
        # 只取正文：reasoning_content（思考内容）不是答案，一律忽略；正文夹带的思考标签同样去除
        return ModelResponse(
            text=strip_reasoning(content if isinstance(content, str) else ""),
            tool_calls=calls,
            stop_reason=finish,
            usage=Usage(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)),
            model=data.get("model") or self.model,
            raw_assistant=None,
            refused=finish == "content_filter",
        )

    def list_models(self) -> list[dict[str, str]]:
        """查询服务商当前可用的型号（GET /models）。未提供该接口时抛 ModelListUnsupported。"""
        try:
            resp = self.client.get(self.models_endpoint)
        except httpx.TimeoutException as exc:
            raise ModelCallFailed(f"请求超时（{type(exc).__name__}）") from exc
        except httpx.TransportError as exc:
            raise ModelCallFailed(f"连接失败（{type(exc).__name__}）：{self._redact(str(exc))[:200]}") from exc
        if resp.status_code in (404, 405, 501):
            raise ModelListUnsupported(f"{self.name} 未提供模型列表接口（GET {self.models_endpoint} 返回 HTTP {resp.status_code}）")
        if not resp.is_success:
            raise self._http_error(resp)
        try:
            data = resp.json()
        except ValueError as exc:
            raise ModelCallFailed(f"模型列表响应不是 JSON（Content-Type: {resp.headers.get('content-type', '')}）") from exc
        items = (data.get("data") if "data" in data else data.get("models")) if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ModelCallFailed("模型列表响应缺少 data 字段")
        out: list[dict[str, str]] = []
        for it in items:
            if isinstance(it, str):
                out.append({"id": it})
            elif isinstance(it, dict):
                mid = it.get("id") or it.get("model") or it.get("name")
                if mid:
                    out.append({"id": str(mid), "owned_by": str(it.get("owned_by") or "")})
        return out

"""模型网关与路由：按任务复杂度选模型，并强制治理。

每一次模型调用都依次经过：
1. 可用性：未配置模型（offline）时直接返回 ModelUnavailable，由技能走确定性路径；
2. 出网网关：按环境路线、材料属性与模型获准级别判定，默认拒绝；
3. 预算：调用次数与令牌上限；
4. 审计：只记录提示模板编号、对象编号与内容哈希（可重建、但不复制材料）；
5. 拒答：模型拒答时显式抛出，不把空结果当成功；
6. 故障：网络、超时、HTTP 错误与报文异常统一为 ModelCallFailed 并写入审计，不悄悄降级。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..harness.budget import BudgetExceeded, BudgetGuard
from ..harness.egress import EgressDenied, EgressGateway, EgressRequest
from ..kernel.config import GongwenConfig, ModelConfig
from ..schemas.common import Clearance, sha256_text
from .base import ChatMessage, ModelCallFailed, ModelProvider, ModelRefused, ModelResponse, ModelUnavailable, ToolDef

ROLES = ("light", "heavy", "reviewer", "agent")


def build_provider(mc: ModelConfig) -> ModelProvider | None:
    if mc.provider in ("offline", "", None):
        return None
    if mc.provider == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            model=mc.name,
            api_key_env=mc.api_key_env or "ANTHROPIC_API_KEY",
            base_url=mc.base_url,
            max_clearance=mc.max_clearance,
            timeout=mc.timeout,
            max_tokens=max(mc.max_tokens, 16000),
            max_retries=mc.max_retries,
        )
    from .openai_compat import OpenAICompatProvider

    return OpenAICompatProvider(
        provider=mc.provider,
        model=mc.name,
        base_url=mc.base_url,
        api_key_env=mc.api_key_env,
        max_clearance=mc.max_clearance,
        timeout=mc.timeout,
        temperature=mc.temperature,
        max_tokens=mc.max_tokens,
        extra_headers=mc.extra_headers,
        max_retries=mc.max_retries,
    )


class ModelRouter:
    def __init__(
        self,
        config: GongwenConfig,
        egress: EgressGateway,
        budget: BudgetGuard | None = None,
        audit: Callable[..., Any] | None = None,
        providers: dict[str, ModelProvider | None] | None = None,
        cache: dict[str, ModelProvider] | None = None,
    ):
        self.config = config
        self.egress = egress
        self.budget = budget
        self.audit = audit or (lambda *a, **k: None)
        self._providers: dict[str, ModelProvider | None] = dict(providers or {})
        self._errors: dict[str, str] = {}
        # 运行时共享的已建适配（按模型配置区分）：每个阶段都会新建网关，不能每次都新建 HTTP 客户端与连接
        self._cache = cache if cache is not None else {}

    # ------------------------------------------------------------------
    def _model_config(self, role: str) -> ModelConfig:
        name = getattr(self.config.routing, role, None) if role in ("light", "heavy", "reviewer") else None
        return self.config.resolve_model(name)

    def provider(self, role: str) -> ModelProvider | None:
        if role in self._providers:
            return self._providers[role]
        if "*" in self._providers:
            return self._providers["*"]
        mc = self._model_config(role)
        key = mc.model_dump_json()
        p = self._cache.get(key)
        if p is None:
            try:
                p = build_provider(mc)
            except ValueError as exc:
                self._errors[role] = str(exc)
                p = None
            if p is not None:
                self._cache[key] = p
        self._providers[role] = p
        return p

    def configuration_error(self, role: str) -> str | None:
        self.provider(role)
        return self._errors.get(role)

    def available(self, role: str, clearances: list[Clearance]) -> bool:
        p = self.provider(role)
        if p is None:
            return False
        v = self.egress.evaluate(EgressRequest(p.endpoint, f"precheck:{role}", clearances, p.max_clearance))
        return v.allowed

    def describe(self) -> dict[str, str]:
        out = {}
        for r in ROLES:
            p = self.provider(r)
            out[r] = f"{p.name}:{p.model}" if p else ("offline" if not self._errors.get(r) else f"未就绪（{self._errors[r]}）")
        return out

    # ------------------------------------------------------------------
    def call(
        self,
        role: str,
        messages: list[ChatMessage],
        *,
        system: str | None = None,
        tools: list[ToolDef] | None = None,
        json_schema: dict[str, Any] | None = None,
        clearances: list[Clearance] | None = None,
        purpose: str = "",
        template_id: str = "",
        object_refs: list[str] | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        p = self.provider(role)
        if p is None:
            raise ModelUnavailable(self._errors.get(role) or "未配置模型（offline）")
        clearances = clearances or [Clearance.PUBLIC]
        payload = json.dumps(
            {"system": system, "messages": [m.content for m in messages], "schema": json_schema}, ensure_ascii=False, default=str
        )
        digest = sha256_text(payload)
        try:
            self.egress.check(EgressRequest(p.endpoint, purpose or role, clearances, p.max_clearance, digest))
        except EgressDenied as exc:
            raise ModelUnavailable(f"出网网关拒绝：{exc}") from exc
        if self.budget:
            try:
                self.budget.precheck_model_call()
            except BudgetExceeded:
                raise
        self.audit(
            "model.request",
            {
                "role": role,
                "provider": p.name,
                "model": p.model,
                "purpose": purpose,
                "template_id": template_id,
                "object_refs": object_refs or [],
                "payload_sha256": digest,
                "payload_chars": len(payload),
                "max_clearance": max(clearances, key=lambda c: c.rank).value,
            },
        )
        try:
            resp = p.complete(messages, system=system, tools=tools, json_schema=json_schema, max_tokens=max_tokens)
        except (ModelRefused, ModelUnavailable):
            raise
        except Exception as exc:  # 网络、超时、HTTP 错误、报文异常：显式失败并留痕，不当作“未配置模型”
            detail = str(exc) if isinstance(exc, ModelCallFailed) else f"{type(exc).__name__}: {exc}"
            err = ModelCallFailed(f"{p.name} 模型接口调用失败：{detail[:300]}")
            self.audit("model.error", {"role": role, "provider": p.name, "model": p.model, "purpose": purpose, "template_id": template_id, "error": str(err)})
            raise err from exc
        resp.schema = json_schema
        self.audit(
            "model.response",
            {
                "role": role,
                "model": resp.model,
                "stop_reason": resp.stop_reason,
                "refused": resp.refused,
                "output_sha256": sha256_text(resp.text),
                "output_chars": len(resp.text),
                "tool_calls": [c.name for c in resp.tool_calls],
                "usage": {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens},
            },
        )
        if self.budget:
            self.budget.charge_model_call(resp.usage.input_tokens, resp.usage.output_tokens)
        if resp.refused:
            raise ModelRefused(f"模型拒答（stop_reason={resp.stop_reason}）")
        return resp

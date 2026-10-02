"""工具注册与执行管线。

一次工具调用依次经过：阶段白名单 → 权限引擎 → 审批策略 → PreToolUse 钩子 →
幂等检查 → 执行 → PostToolUse 钩子 → 审计日志。
工具协议接入（含 MCP）不等于安全授权，授权由本管线独立实施。
"""

from __future__ import annotations

import fnmatch
import json
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..schemas.common import sha256_text
from .approval import ApprovalPolicy, ApprovalRequest
from .hooks import HookRunner
from .permissions import Action, PermissionEngine, Principal
from .session import SessionLog


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]
    action: Action
    side_effect: bool = False
    risk: str = "low"
    stages: set[str] | None = None  # None=任何阶段可用
    idempotent_key: Callable[[dict[str, Any]], str] | None = None
    human_only: bool = False

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


@dataclass
class ToolResult:
    ok: bool
    output: Any = None
    error: str = ""
    skipped: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def as_text(self) -> str:
        if not self.ok:
            return f"错误：{self.error}"
        if isinstance(self.output, str):
            return self.output
        return json.dumps(self.output, ensure_ascii=False, indent=2, default=str)


class ToolRegistry:
    def __init__(
        self,
        permissions: PermissionEngine,
        approval: ApprovalPolicy,
        hooks: HookRunner | None = None,
        log: SessionLog | None = None,
        workspace: str | Path | None = None,
    ):
        self.tools: dict[str, ToolSpec] = {}
        self.permissions = permissions
        self.approval = approval
        self.hooks = hooks
        self.log = log
        self.workspace = Path(workspace).resolve() if workspace else None

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self.tools:
            raise KeyError(f"工具重名：{spec.name}")
        self.tools[spec.name] = spec

    def unregister(self, name: str) -> None:
        self.tools.pop(name, None)

    def available(self, principal: Principal, stage: str | None = None, allow: list[str] | None = None) -> list[ToolSpec]:
        out = []
        for t in self.tools.values():
            if t.human_only and not principal.is_human:
                continue
            if stage and t.stages is not None and stage not in t.stages:
                continue
            if allow is not None and not any(fnmatch.fnmatch(t.name, pat) for pat in allow):
                continue
            if not self.permissions.check(principal, t.action).allowed:
                continue
            out.append(t)
        return out

    def _outside_workspace(self, args: dict[str, Any]) -> bool:
        if self.workspace is None:
            return False
        for key in ("path", "out", "output", "out_dir"):
            val = args.get(key)
            if isinstance(val, str) and val:
                p = Path(val)
                p = (self.workspace / p).resolve() if not p.is_absolute() else p.resolve()
                if self.workspace != p and self.workspace not in p.parents:
                    return True
        return False

    def call(
        self,
        name: str,
        args: dict[str, Any],
        principal: Principal,
        stage: str | None = None,
        matter_id: str | None = None,
        allow: list[str] | None = None,
    ) -> ToolResult:
        spec = self.tools.get(name)
        audit = self.log.append if self.log else (lambda *a, **k: None)
        args_digest = sha256_text(json.dumps(args, ensure_ascii=False, sort_keys=True, default=str))
        base = {"tool": name, "principal": principal.id, "args_sha256": args_digest}
        if spec is None:
            audit("tool.denied", {**base, "reason": "未知工具"}, actor=principal.id, stage=stage)
            return ToolResult(False, error=f"未知工具：{name}")
        if allow is not None and not any(fnmatch.fnmatch(name, pat) for pat in allow):
            reason = f"工具 {name} 不在当前技能/阶段的白名单内"
            audit("tool.denied", {**base, "reason": reason}, actor=principal.id, stage=stage)
            return ToolResult(False, error=reason)
        if stage and spec.stages is not None and stage not in spec.stages:
            reason = f"工具 {name} 在阶段“{stage}”不可用"
            audit("tool.denied", {**base, "reason": reason}, actor=principal.id, stage=stage)
            return ToolResult(False, error=reason)
        if spec.human_only and not principal.is_human:
            reason = f"工具 {name} 仅限人工通道"
            audit("tool.denied", {**base, "reason": reason}, actor=principal.id, stage=stage)
            return ToolResult(False, error=reason)
        decision = self.permissions.check(principal, spec.action, matter_id)
        if not decision.allowed:
            audit("tool.denied", {**base, "reason": decision.reason}, actor=principal.id, stage=stage)
            return ToolResult(False, error=decision.reason)
        req = ApprovalRequest(
            tool=name,
            summary=f"{spec.description}",
            risk=spec.risk,
            side_effect=spec.side_effect,
            outside_workspace=self._outside_workspace(args),
            details={"args": args},
        )
        outcome = self.approval.decide(req)
        if not outcome.approved:
            audit("tool.denied", {**base, "reason": outcome.reason}, actor=principal.id, stage=stage)
            return ToolResult(False, error=outcome.reason)
        if self.hooks is not None:
            hr = self.hooks.run("PreToolUse", name, {"args": args, "principal": principal.id, "stage": stage})
            if hr.blocked:
                audit("tool.blocked_by_hook", {**base, "reason": hr.reason}, actor=principal.id, stage=stage)
                return ToolResult(False, error=f"钩子阻断：{hr.reason}")
        idem = spec.idempotent_key(args) if spec.idempotent_key else None
        if idem and self.log and idem in self.log.completed_idempotency_keys():
            audit("tool.skipped", {**base, "idempotency_key": idem}, actor=principal.id, stage=stage)
            return ToolResult(True, output="（已执行过，按幂等键跳过）", skipped=True)
        audit("tool.call", {**base, "approval": outcome.reason}, actor=principal.id, stage=stage)
        try:
            output = spec.handler(**args)
            result = ToolResult(True, output=output)
        except PermissionError as exc:
            result = ToolResult(False, error=str(exc))
        except Exception as exc:  # 失败必须可见
            result = ToolResult(False, error=f"{type(exc).__name__}: {exc}", meta={"trace": traceback.format_exc(limit=3)})
        if self.hooks is not None and result.ok:
            self.hooks.run("PostToolUse", name, {"args": args, "principal": principal.id, "stage": stage})
        audit(
            "tool.result",
            {**base, "ok": result.ok, "error": result.error, "idempotency_key": idem},
            actor=principal.id,
            stage=stage,
        )
        return result

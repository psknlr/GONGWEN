"""运行时装配：以插件方式组装依据库、文风库、存储、治理机制与技能注册表。

单位可在配置 plugins 中追加插件（如替换检索器、追加本单位规则检查器），
插件通过内核服务与事件协作，无需修改本项目源码。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .harness.approval import ApprovalPolicy, ApprovalRequest
from .harness.budget import BudgetGuard
from .harness.egress import EgressGateway
from .harness.hooks import HookRunner
from .harness.instructions import merged as merged_instructions
from .harness.permissions import PermissionEngine
from .harness.session import SessionLog
from .kernel.config import GongwenConfig, load_config
from .kernel.context import Context, Plugin
from .knowledge import kb
from .knowledge.policy_library import PolicyLibrary
from .knowledge.stores import CaseLibrary, MaterialStore, TaskStore
from .llm.router import ModelRouter
from .schemas.state import BudgetUsage


class KnowledgePlugin(Plugin):
    name = "knowledge"

    def __init__(self, ctx: Context, config: dict | None = None):
        super().__init__(ctx, config)
        cfg: GongwenConfig = ctx.get("config")
        data_dir = Path(cfg.environment.data_dir)
        extra = [data_dir / "policies"]
        allow_synth = bool((config or {}).get("allow_synthetic", False))
        ctx.provide("policies", PolicyLibrary(extra_paths=[p for p in extra if p.exists()], allow_synthetic=allow_synth))
        case_extra = [data_dir / "cases.yaml"]
        ctx.provide("cases", CaseLibrary([p for p in case_extra if p.exists()]))

    inject = ["config"]


class StoresPlugin(Plugin):
    name = "stores"
    inject = ["config"]

    def __init__(self, ctx: Context, config=None):
        super().__init__(ctx, config)
        cfg: GongwenConfig = ctx.get("config")
        root = Path(cfg.environment.data_dir)
        root.mkdir(parents=True, exist_ok=True)
        ctx.provide("materials", MaterialStore(root))
        ctx.provide("tasks", TaskStore(root))


class GovernancePlugin(Plugin):
    name = "governance"
    inject = ["config"]

    def __init__(self, ctx: Context, config=None):
        super().__init__(ctx, config)
        cfg: GongwenConfig = ctx.get("config")
        approver = (config or {}).get("approver")
        ctx.provide("permissions", PermissionEngine())
        ctx.provide("approval", ApprovalPolicy(cfg.approval.policy, approver))
        ctx.provide("hooks", HookRunner(cfg.hooks, ctx, cwd=ctx.maybe("workspace")))
        ctx.provide("egress", EgressGateway(cfg.environment.route, cfg.egress.allowed_hosts, cfg.egress.block_all))


class SkillsPlugin(Plugin):
    name = "skills"
    inject = ["config", "policies", "cases"]

    def __init__(self, ctx: Context, config=None):
        super().__init__(ctx, config)
        from .skills import build_registry

        ctx.provide("skills", build_registry())


def _load_external(spec: dict[str, Any]) -> tuple[Any, Any]:
    mod_path, _, attr = spec["module"].partition(":")
    mod = importlib.import_module(mod_path)
    return getattr(mod, attr or "Plugin"), spec.get("config")


@dataclass
class Runtime:
    ctx: Context
    config: GongwenConfig
    workspace: Path

    # ---- 便捷访问
    @property
    def data_dir(self) -> Path:
        return Path(self.config.environment.data_dir)

    @property
    def policies(self) -> PolicyLibrary:
        return self.ctx.get("policies")

    @property
    def cases(self) -> CaseLibrary:
        return self.ctx.get("cases")

    @property
    def materials(self) -> MaterialStore:
        return self.ctx.get("materials")

    @property
    def tasks(self) -> TaskStore:
        return self.ctx.get("tasks")

    @property
    def permissions(self) -> PermissionEngine:
        return self.ctx.get("permissions")

    @property
    def approval(self) -> ApprovalPolicy:
        return self.ctx.get("approval")

    @property
    def hooks(self) -> HookRunner:
        return self.ctx.get("hooks")

    @property
    def egress(self) -> EgressGateway:
        return self.ctx.get("egress")

    @property
    def skills(self):
        return self.ctx.get("skills")

    @property
    def profile(self) -> dict:
        return kb.unit_profile(self.config.environment.unit_profile)

    def instructions(self) -> str:
        return merged_instructions(self.workspace)

    def session_log(self, task_id: str) -> SessionLog:
        return SessionLog(self.tasks.task_dir(task_id) / "events.jsonl")

    def router(self, log: SessionLog | None = None, usage: BudgetUsage | None = None, providers: dict | None = None) -> ModelRouter:
        egress = EgressGateway(
            self.config.environment.route,
            self.config.egress.allowed_hosts,
            self.config.egress.block_all,
            audit=(lambda t, p: log.append(t, p)) if log else None,
        )
        budget = BudgetGuard(self.config.budget, usage)
        return ModelRouter(
            self.config,
            egress,
            budget,
            audit=(lambda t, p: log.append(t, p, actor="model-gateway")) if log else None,
            providers=providers if providers is not None else self.ctx.maybe("model_providers"),
        )


def build_runtime(
    workspace: str | Path | None = None,
    config: GongwenConfig | None = None,
    profile: str | None = None,
    overrides: dict[str, Any] | None = None,
    approver: Callable[[ApprovalRequest], bool] | None = None,
    model_providers: dict | None = None,
    allow_synthetic_policies: bool = False,
) -> Runtime:
    workspace = Path(workspace or ".").resolve()
    cfg = config or load_config(workspace, profile=profile, overrides=overrides)
    ctx = Context()
    ctx.provide("config", cfg)
    ctx.provide("workspace", str(workspace))
    if model_providers is not None:
        ctx.provide("model_providers", model_providers)
    ctx.plugin(StoresPlugin)
    ctx.plugin(KnowledgePlugin, {"allow_synthetic": allow_synthetic_policies})
    ctx.plugin(GovernancePlugin, {"approver": approver})
    ctx.plugin(SkillsPlugin)
    for spec in cfg.plugins:
        target, pconf = _load_external(spec)
        ctx.plugin(target, pconf)
    failed = {k: v for k, v in ctx.plugin_states().items() if v == "failed"}
    if failed:
        raise RuntimeError(f"插件激活失败：{failed}")
    return Runtime(ctx=ctx, config=cfg, workspace=workspace)

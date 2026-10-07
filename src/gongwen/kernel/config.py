"""配置加载：默认值 < 用户级 ~/.gongwen/config.toml < 工作区 .gongwen/config.toml
< 配置档 profile < 环境变量 < 命令行参数（借鉴 Codex config.toml 与 profiles）。"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import Field

from ..schemas.common import Clearance, EnvironmentRoute, GWModel


class ModelConfig(GWModel):
    provider: str = Field(default="offline", description="offline/deepseek/xai/zhipu/qwen/moonshot/ollama/vllm/openai_compat/anthropic/scripted")
    name: str = ""
    base_url: str = ""
    api_key_env: str = ""
    max_clearance: Clearance = Field(
        default=Clearance.PUBLIC,
        description="允许发送给该模型的最高材料属性。公共云模型默认仅限公开材料。",
    )
    timeout: float = Field(default=90.0, description="单次请求超时（秒）；Claude 适配使用流式，按两次数据之间的间隔计")
    max_retries: int = Field(default=2, ge=0, description="限流、服务端错误、连接失败与超时的自动重试次数；重试后仍失败则显式报错")
    temperature: float = 0.2
    max_tokens: int = 4096
    extra_headers: dict[str, str] = Field(default_factory=dict)


class RoutingConfig(GWModel):
    """按任务复杂度路由：轻量模型做分类抽取，强模型做起草与高风险语义审校。"""

    light: str | None = None
    heavy: str | None = None
    reviewer: str | None = Field(default=None, description="独立审校建议使用不同模型或至少独立上下文")


class EnvironmentConfig(GWModel):
    route: EnvironmentRoute = EnvironmentRoute.PUBLIC_DEV
    data_dir: str = ".gongwen"
    unit_profile: str = "party_gov"
    unit_name: str = ""
    region: str = ""
    accept_internal_materials: bool = Field(
        default=False,
        description="仅在“单位批准的业务环境”中，经单位制度明确后才可开启",
    )


class ApprovalConfig(GWModel):
    policy: str = Field(default="on-request", description="untrusted/on-request/never（借鉴 Codex 审批策略）")
    auto_accept_checkpoints: list[str] = Field(
        default_factory=list,
        description="无头模式下允许按显式参数自动接受的审核节点类型；“人工送审”永远不可自动通过",
    )


class EgressConfig(GWModel):
    allowed_hosts: list[str] = Field(default_factory=list)
    block_all: bool = False


class BudgetConfig(GWModel):
    max_model_calls: int = 60
    max_input_tokens: int = 400_000
    max_output_tokens: int = 120_000
    max_revision_rounds: int = 2
    max_wall_seconds: float = 1800.0


class HookSpec(GWModel):
    matcher: str = "*"
    command: str
    timeout: float = 30.0


class HooksConfig(GWModel):
    SessionStart: list[HookSpec] = Field(default_factory=list)
    PreToolUse: list[HookSpec] = Field(default_factory=list)
    PostToolUse: list[HookSpec] = Field(default_factory=list)
    StageEnter: list[HookSpec] = Field(default_factory=list)
    StageExit: list[HookSpec] = Field(default_factory=list)
    CheckpointRequested: list[HookSpec] = Field(default_factory=list)


class ReviewConfig(GWModel):
    max_auto_rounds: int = 2
    semantic_with_model: bool = True


class LayoutConfig(GWModel):
    profile: str = "gbt9704-2012"
    margin_mode: str = Field(default="standard", description="standard/compensated")
    render_check: bool = True
    template: str = Field(default="", description="默认公文模板（gongwen template list）；空表示按基础配置档的默认参数")
    font_substitution: bool = Field(default=True, description="渲染预览时，未安装的公文字库以开源字体替代（只影响预览与核验，不改变 DOCX）")


class FeatureFlags(GWModel):
    """消融实验开关：评测时可逐项关闭以观察模块贡献。"""

    fact_ledger: bool = True
    temporal_check: bool = True
    independent_review: bool = True
    targeted_revision: bool = True
    consistency_check: bool = True
    burden_check: bool = True


class McpConfig(GWModel):
    """MCP 服务（供 Codex / grok / Claude Code 等外部智能体客户端调用）。"""

    max_clearance: Clearance = Field(
        default=Clearance.PUBLIC,
        description="经 MCP 返回给外部客户端的内容的最高材料属性。客户端会把结果送入其自身配置的模型，本系统无法控制其去向，默认仅限公开。",
    )
    allow_material_paths: bool = Field(default=True, description="是否允许按工作区内路径添加材料（工作区外路径一律拒绝）")


class AgentConfig(GWModel):
    """对话式代理（gongwen chat）。"""

    max_turns: int = Field(default=16, description="单次用户输入内模型—工具往返的最大轮数")


class GongwenConfig(GWModel):
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    models: dict[str, ModelConfig] = Field(default_factory=dict)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    egress: EgressConfig = Field(default_factory=EgressConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    layout: LayoutConfig = Field(default_factory=LayoutConfig)
    features: FeatureFlags = Field(default_factory=FeatureFlags)
    mcp: McpConfig = Field(default_factory=McpConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    profiles: dict[str, dict[str, Any]] = Field(default_factory=dict)
    plugins: list[dict[str, Any]] = Field(default_factory=list, description="附加插件：[{module=..., config={...}}]")

    def resolve_model(self, name: str | None) -> ModelConfig:
        if name and name in self.models:
            return self.models[name]
        return self.model


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


_ENV_MAP = {
    "GONGWEN_ROUTE": ("environment", "route"),
    "GONGWEN_DATA_DIR": ("environment", "data_dir"),
    "GONGWEN_UNIT_PROFILE": ("environment", "unit_profile"),
    "GONGWEN_MODEL_PROVIDER": ("model", "provider"),
    "GONGWEN_MODEL_NAME": ("model", "name"),
    "GONGWEN_MODEL_BASE_URL": ("model", "base_url"),
    "GONGWEN_APPROVAL_POLICY": ("approval", "policy"),
}


def load_config(
    workspace: str | Path | None = None,
    profile: str | None = None,
    overrides: dict[str, Any] | None = None,
    user_home: str | Path | None = None,
) -> GongwenConfig:
    workspace = Path(workspace or os.getcwd())
    home = Path(user_home) if user_home else Path(os.environ.get("GONGWEN_HOME", Path.home() / ".gongwen"))
    data: dict[str, Any] = {}
    data = _deep_merge(data, _read_toml(home / "config.toml"))
    data = _deep_merge(data, _read_toml(workspace / ".gongwen" / "config.toml"))
    profile = profile or os.environ.get("GONGWEN_PROFILE")
    if profile:
        prof = data.get("profiles", {}).get(profile)
        if prof is None:
            raise KeyError(f"未找到配置档：{profile}")
        data = _deep_merge(data, prof)
    for env, (sec, key) in _ENV_MAP.items():
        if os.environ.get(env):
            data.setdefault(sec, {})[key] = os.environ[env]
    if overrides:
        data = _deep_merge(data, overrides)
    cfg = GongwenConfig.model_validate(data)
    # 数据目录相对工作区解析
    ddir = Path(cfg.environment.data_dir)
    if not ddir.is_absolute():
        cfg.environment.data_dir = str((workspace / ddir).resolve())
    return cfg


DEFAULT_CONFIG_TOML = """# 公文智能体配置（工作区级）。用户级配置位于 ~/.gongwen/config.toml。
# 优先级：默认值 < 用户级 < 工作区级 < 配置档 profile < 环境变量 < 命令行参数。

[environment]
# public_dev：公开材料研发版（仅处理公开且获准用途的材料、合成测试任务）
# unit_approved：单位批准的业务环境（仅处理经明确审查准入的材料与事项）
# 涉密应用不在本系统能力承诺范围内，不提供该选项。
route = "public_dev"
unit_profile = "party_gov"   # party_gov / hospital / university / research_institute
unit_name = ""
region = ""

[model]
# offline：不调用任何外部模型，使用确定性规则与模板（默认，零出网）。
# 可选：deepseek / xai / zhipu / qwen / moonshot / ollama / vllm / openai_compat / anthropic
provider = "offline"
name = ""
base_url = ""
api_key_env = ""
# 允许发送给该模型的最高材料属性；公共云模型保持“公开”。
max_clearance = "公开"

[approval]
policy = "on-request"        # untrusted / on-request / never
auto_accept_checkpoints = [] # 无头模式下可自动接受的节点；“人工送审”永远不可自动通过

[egress]
allowed_hosts = []           # 例如 ["api.deepseek.com"]；未列出的主机一律拒绝

[budget]
max_model_calls = 60
max_revision_rounds = 2

[layout]
profile = "gbt9704-2012"
margin_mode = "standard"     # standard：天头37mm/订口28mm；compensated：软件页边距补偿口径（实务）
template = ""                # 默认公文模板（gongwen template list）；空为国标默认参数

# 示例：使用 DeepSeek 作为起草模型、独立审校使用另一模型
# [models.drafter]
# provider = "deepseek"
# name = "deepseek-chat"
# api_key_env = "DEEPSEEK_API_KEY"
# [models.reviewer]
# provider = "zhipu"
# name = "glm-4.6"
# api_key_env = "ZHIPUAI_API_KEY"
# [routing]
# heavy = "drafter"
# reviewer = "reviewer"

[mcp]
# 经 MCP 返回给外部智能体客户端（Codex、grok、Claude Code 等）的内容的最高材料属性。
# 客户端会把工具结果送入它自己配置的模型，本系统无法控制其去向，因此默认仅限公开材料。
max_clearance = "公开"

# 示例：钩子（借鉴 grok-cli / Claude Code hooks）
# [[hooks.PreToolUse]]
# matcher = "export_*"
# command = "./scripts/check_export.sh"
"""

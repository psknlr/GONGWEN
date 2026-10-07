"""模型接入预设：各服务商的地址、密钥变量、默认型号与接口差异。

每个预设说明：
* 协议：openai_compat（/chat/completions）或 anthropic（官方 SDK，Messages API）；
* 默认地址（含区域变体，如中国大陆站与国际站）与读取密钥的环境变量（密钥只从环境变量读取，绝不写入配置）；
* 默认强模型（heavy）与轻量模型（light）；已知型号仅作文档参考——型号会随服务商更新，
  以 ``gongwen model remote <名称>`` 查询到的为准；
* 接口差异（Quirks）：输出上限参数名、是否发送采样温度及取值范围、JSON 模式、思考开关等。

所有字段都可在配置中覆盖（base_url、name、api_key_env、json_mode、extra_body、thinking），
服务商调整接口时不必等待本项目更新。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlparse

JSON_MODES = ("json_schema", "json_object", "none")


@dataclass(frozen=True)
class Quirks:
    """OpenAI 兼容接口在各服务商之间的差异。"""

    token_param: str = "max_tokens"  # 输出上限参数名：max_tokens / max_completion_tokens（推理模型）
    send_temperature: bool = True  # 推理模型不接受非默认采样温度：不发送
    temperature_range: tuple[float, float] = (0.0, 2.0)  # 发送前夹到该区间（如 MiniMax 要求 (0, 1]）
    json_mode: str = "json_object"  # json_schema / json_object / none（不发送 response_format，只靠提示约束与宽容解析）
    thinking_switch: str = ""  # 思考开关的写法："glm" = 请求体 thinking={"type": "enabled"|"disabled"}；空 = 不支持
    extra_body: dict[str, Any] = field(default_factory=dict)  # 附加到请求体的固定字段

    def describe(self) -> str:
        parts = []
        if self.token_param != "max_tokens":
            parts.append(f"输出上限参数 {self.token_param}")
        if not self.send_temperature:
            parts.append("不发送 temperature")
        elif self.temperature_range != (0.0, 2.0):
            lo, hi = self.temperature_range
            parts.append(f"temperature 限定 {lo:g}–{hi:g}")
        parts.append({"json_schema": "JSON 模式 json_schema", "json_object": "JSON 模式 json_object", "none": "仅提示约束 JSON"}[self.json_mode])
        if self.thinking_switch:
            parts.append("可设思考开关")
        return "；".join(parts)


@dataclass(frozen=True)
class Preset:
    name: str
    display: str  # 中文显示名
    protocol: str  # openai_compat / anthropic
    base_url: str
    api_key_env: str
    heavy: str = ""  # 默认强模型（起草、修订、审校）
    light: str = ""  # 默认轻量模型（分类、抽取）
    models: tuple[str, ...] = ()  # 已知型号（仅作文档参考，以 gongwen model remote 为准）
    regions: dict[str, str] = field(default_factory=dict)  # 区域 → 地址（cn 中国大陆站 / intl 国际站）
    quirks: Quirks = field(default_factory=Quirks)
    model_rules: tuple[tuple[str, dict[str, Any]], ...] = ()  # (型号片段, 差异覆盖)：按型号调整差异
    notes: str = ""

    @property
    def host(self) -> str:
        return (urlparse(self.base_url).hostname or "").lower()

    def base_for(self, region: str | None) -> str:
        if not region:
            return self.base_url
        if region not in self.regions:
            avail = "、".join(self.regions) or "无"
            raise ValueError(f"预设 {self.name} 没有区域 {region}（可选：{avail}）")
        return self.regions[region]

    def default_model(self, roles: list[str] | tuple[str, ...] = ()) -> str:
        """只用于轻量角色时取轻量模型，否则取强模型。"""
        if roles and set(roles) <= {"light"} and self.light:
            return self.light
        return self.heavy

    def quirks_for(self, model: str) -> Quirks:
        q = self.quirks
        low = (model or "").lower()
        for frag, over in self.model_rules:
            if frag in low:
                q = replace(q, **over)
        return q


_REASONING = {"token_param": "max_completion_tokens", "send_temperature": False}

PRESETS: dict[str, Preset] = {
    p.name: p
    for p in (
        Preset(
            "anthropic",
            "Anthropic Claude",
            "anthropic",
            "https://api.anthropic.com",
            "ANTHROPIC_API_KEY",
            heavy="claude-opus-5-5",
            light="claude-haiku-4-5",
            models=("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"),
            quirks=Quirks(send_temperature=False, json_mode="json_schema"),
            notes='官方 SDK（pip install "gongwen[anthropic]"）；结构化输出与严格工具；不发送采样参数；Haiku 4.5 等旧型号不发送 effort',
        ),
        Preset(
            "openai",
            "OpenAI GPT",
            "openai_compat",
            "https://api.openai.com/v1",
            "OPENAI_API_KEY",
            heavy="gpt-6.1-sol",
            light="gpt-6-luna",
            models=("gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"),
            quirks=Quirks(json_mode="json_schema", **_REASONING),
            notes="当前 GPT 均为推理模型",
        ),
        Preset(
            "deepseek",
            "DeepSeek 深度求索",
            "openai_compat",
            "https://api.deepseek.com",
            "DEEPSEEK_API_KEY",
            heavy="deepseek-chat",
            light="deepseek-chat",
            models=("deepseek-chat", "deepseek-reasoner"),
            model_rules=(("reasoner", {"send_temperature": False, "json_mode": "none"}),),
            notes="deepseek-reasoner：思考内容（reasoning_content）一律忽略，不发送 response_format 与 temperature",
        ),
        Preset(
            "zhipu",
            "智谱 GLM",
            "openai_compat",
            "https://open.bigmodel.cn/api/paas/v4",
            "ZHIPUAI_API_KEY",
            heavy="glm-4.6",
            light="glm-4.5-air",
            models=("glm-4.6", "glm-4.5-air"),
            regions={"cn": "https://open.bigmodel.cn/api/paas/v4", "intl": "https://api.z.ai/api/paas/v4"},
            quirks=Quirks(temperature_range=(0.01, 1.0), thinking_switch="glm"),
            notes="国际站 Z.ai 用 --region intl；GLM-5.x、GLM-4.7 系列的型号编码以 gongwen model remote 或官方文档为准；--thinking on|off 设置请求体 thinking 字段",
        ),
        Preset(
            "minimax",
            "MiniMax（中国大陆站）",
            "openai_compat",
            "https://api.minimaxi.com/v1",
            "MINIMAX_API_KEY",
            heavy="MiniMax-M2.7",
            light="MiniMax-M2.7-highspeed",
            models=("MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-M2.5", "MiniMax-M2.1", "MiniMax-M2"),
            regions={"cn": "https://api.minimaxi.com/v1", "intl": "https://api.minimax.io/v1"},
            quirks=Quirks(temperature_range=(0.01, 1.0), json_mode="none"),
            notes="思考内容（reasoning_content 或正文中的 <think> 标签）一律去除",
        ),
        Preset(
            "minimax_intl",
            "MiniMax（国际站）",
            "openai_compat",
            "https://api.minimax.io/v1",
            "MINIMAX_API_KEY",
            heavy="MiniMax-M2.7",
            light="MiniMax-M2.7-highspeed",
            models=("MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-M2.5", "MiniMax-M2.1", "MiniMax-M2"),
            regions={"cn": "https://api.minimaxi.com/v1", "intl": "https://api.minimax.io/v1"},
            quirks=Quirks(temperature_range=(0.01, 1.0), json_mode="none"),
            notes="同 minimax，默认国际站地址",
        ),
        Preset(
            "qwen",
            "通义千问（阿里云百炼）",
            "openai_compat",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "DASHSCOPE_API_KEY",
            heavy="qwen-plus",
            light="qwen-plus",
            models=("qwen-max", "qwen-plus", "qwen-flash"),
            regions={"cn": "https://dashscope.aliyuncs.com/compatible-mode/v1", "intl": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"},
            quirks=Quirks(temperature_range=(0.0, 1.99)),
            notes="兼容模式接口；国际站用 --region intl",
        ),
        Preset(
            "moonshot",
            "月之暗面 Kimi",
            "openai_compat",
            "https://api.moonshot.cn/v1",
            "MOONSHOT_API_KEY",
            heavy="moonshot-v1-32k",
            light="moonshot-v1-32k",
            models=("moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"),
            regions={"cn": "https://api.moonshot.cn/v1", "intl": "https://api.moonshot.ai/v1"},
            quirks=Quirks(temperature_range=(0.0, 1.0)),
            notes="Kimi K2 等新型号名称以 gongwen model remote 为准；国际站用 --region intl",
        ),
        Preset(
            "xai",
            "xAI Grok",
            "openai_compat",
            "https://api.x.ai/v1",
            "XAI_API_KEY",
            heavy="grok-4",
            light="grok-4",
            models=("grok-4",),
            notes="",
        ),
        Preset(
            "ollama",
            "Ollama（本机部署）",
            "openai_compat",
            "http://localhost:11434/v1",
            "",
            notes="须用 --model 指定本机已拉取的模型；本机回环地址免列出网白名单",
        ),
        Preset(
            "vllm",
            "vLLM（自部署）",
            "openai_compat",
            "http://localhost:8000/v1",
            "",
            notes="须用 --model 指定部署的模型；非本机地址须列入出网白名单",
        ),
        Preset(
            "openai_compat",
            "通用 OpenAI 兼容接口",
            "openai_compat",
            "",
            "OPENAI_COMPAT_API_KEY",
            notes="须用 --base-url 与 --model 指定；接口差异可在配置中用 json_mode、extra_body 调整",
        ),
    )
}

ALIASES: dict[str, str] = {
    "claude": "anthropic",
    "gpt": "openai",
    "glm": "zhipu",
    "zhipuai": "zhipu",
    "kimi": "moonshot",
    "minimax-cn": "minimax",
    "minimax_cn": "minimax",
    "minimax-intl": "minimax_intl",
    "grok": "xai",
    "tongyi": "qwen",
    "dashscope": "qwen",
}


def canonical(name: str | None) -> str:
    """预设名规范化（大小写、别名）；未知名称原样返回（小写）。"""
    key = (name or "").strip().lower()
    return ALIASES.get(key, key)


def resolve(name: str | None) -> Preset | None:
    return PRESETS.get(canonical(name))


def generic() -> Preset:
    return PRESETS["openai_compat"]


def alias_lines() -> list[str]:
    return [f"{a} → {t}" for a, t in ALIASES.items()]

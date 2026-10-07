"""模型接入预设与 gongwen model 命令：全部使用 httpx.MockTransport 或本机回环地址上的模拟服务，不访问网络。

覆盖：各预设的地址、路径与鉴权头；输出上限参数名；temperature 的省略与夹值；reasoning_content 忽略；
<think> 标签去除（含思考块后的 JSON）；服务端拒绝 response_format 等参数时去掉重试；别名解析；
model add 写回配置（保留注释）与校验；model list 不显示密钥；出网被拒的提示；model remote 解析；
Claude 旧型号不发送 effort。
"""

from __future__ import annotations

import json
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest

from gongwen.cli.main import build_parser, main as cli, make_engine
from gongwen.cli.model_cmd import doctor_lines
from gongwen.harness.egress import EgressGateway
from gongwen.kernel.config import GongwenConfig, ModelConfig, load_config
from gongwen.kernel.config_edit import set_key, upsert_table
from gongwen.llm import presets
from gongwen.llm.base import ChatMessage, ModelListUnsupported, ModelUnavailable, ToolDef, parse_json, strip_reasoning
from gongwen.llm.openai_compat import PRESETS as LEGACY_PRESETS
from gongwen.llm.openai_compat import ModelHTTPError, OpenAICompatProvider
from gongwen.llm.router import ModelRouter, build_provider
from gongwen.schemas.common import Clearance, EnvironmentRoute

KEY = "sk-test-presets-0123456789"
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
SYSTEM = "你是测试助手。"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """不读取本机用户级配置；预设的密钥变量统一设为测试密钥。"""
    monkeypatch.setenv("GONGWEN_HOME", str(tmp_path / "_home"))
    for p in presets.PRESETS.values():
        if p.api_key_env:
            monkeypatch.setenv(p.api_key_env, KEY)


def completion(model: str, content: Any = '{"ok": true}', **msg_extra) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content, **msg_extra}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


class Recorder:
    """MockTransport：记录请求，按处理函数返回响应。"""

    def __init__(self, handler: Callable[[httpx.Request, dict], httpx.Response] | None = None):
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict] = []
        self.handler = handler or (lambda req, body: httpx.Response(200, json=completion(body.get("model", "m"))))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append(request)
        self.bodies.append(body)
        return self.handler(request, body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def provider(preset: str, rec: Recorder, **kw) -> OpenAICompatProvider:
    return OpenAICompatProvider(preset, transport=rec.transport, max_retries=0, **kw)


def ask(p: OpenAICompatProvider, **kw):
    return p.complete([ChatMessage("user", "测试")], system=SYSTEM, json_schema=SCHEMA, **kw)


# ====================================================================== 预设与别名
def test_alias_resolution_and_backward_compat():
    assert {a: presets.canonical(a) for a in ("gpt", "glm", "kimi", "claude", "minimax-cn", "GPT", " Claude ")} == {
        "gpt": "openai",
        "glm": "zhipu",
        "kimi": "moonshot",
        "claude": "anthropic",
        "minimax-cn": "minimax",
        "GPT": "openai",
        " Claude ": "anthropic",
    }
    assert presets.resolve("no-such-provider") is None
    # 旧配置值照常可用；旧的 PRESETS 字典仍可导入
    for old in ("deepseek", "zhipu", "qwen", "moonshot", "xai", "ollama", "vllm", "openai_compat"):
        assert old in LEGACY_PRESETS and set(LEGACY_PRESETS[old]) == {"base_url", "api_key_env", "model"}
    assert LEGACY_PRESETS["zhipu"]["model"] == "glm-4.6" and LEGACY_PRESETS["deepseek"]["api_key_env"] == "DEEPSEEK_API_KEY"
    # 别名经 build_provider 建立对应适配
    p = build_provider(ModelConfig(provider="gpt"))
    assert isinstance(p, OpenAICompatProvider) and p.name == "openai" and p.model == "gpt-6.1-sol"
    assert build_provider(ModelConfig(provider="kimi")).endpoint == "https://api.moonshot.cn/v1/chat/completions"
    assert build_provider(ModelConfig(provider="minimax-cn")).endpoint == "https://api.minimaxi.com/v1/chat/completions"
    pytest.importorskip("anthropic")
    from gongwen.llm.anthropic_provider import AnthropicProvider

    c = build_provider(ModelConfig(provider="claude"))
    assert isinstance(c, AnthropicProvider) and c.model == "claude-opus-5-5"


def test_preset_registry_shape():
    assert {"anthropic", "openai", "deepseek", "zhipu", "minimax", "minimax_intl", "qwen", "moonshot", "xai", "ollama", "vllm", "openai_compat"} <= set(presets.PRESETS)
    assert presets.PRESETS["minimax_intl"].base_url == "https://api.minimax.io/v1"
    assert presets.PRESETS["zhipu"].base_for("intl") == "https://api.z.ai/api/paas/v4"
    assert presets.PRESETS["anthropic"].default_model(["light"]) == "claude-haiku-4-5"
    assert presets.PRESETS["anthropic"].default_model(["light", "heavy"]) == "claude-opus-5-5"
    with pytest.raises(ValueError, match="没有区域"):
        presets.PRESETS["deepseek"].base_for("intl")
    for p in presets.PRESETS.values():
        assert p.display and p.protocol in ("openai_compat", "anthropic")
        assert p.quirks.json_mode in presets.JSON_MODES


# 预设 → (完整地址, 是否带鉴权头, 输出上限参数, temperature（None 表示不发送）, response_format 类型（None 表示不发送）)
EXPECT = {
    "openai": ("https://api.openai.com/v1/chat/completions", True, "max_completion_tokens", None, "json_schema"),
    "deepseek": ("https://api.deepseek.com/chat/completions", True, "max_tokens", 0.2, "json_object"),
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4/chat/completions", True, "max_tokens", 0.2, "json_object"),
    "minimax": ("https://api.minimaxi.com/v1/chat/completions", True, "max_tokens", 0.2, None),
    "minimax_intl": ("https://api.minimax.io/v1/chat/completions", True, "max_tokens", 0.2, None),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", True, "max_tokens", 0.2, "json_object"),
    "moonshot": ("https://api.moonshot.cn/v1/chat/completions", True, "max_tokens", 0.2, "json_object"),
    "xai": ("https://api.x.ai/v1/chat/completions", True, "max_tokens", 0.2, "json_object"),
    "ollama": ("http://localhost:11434/v1/chat/completions", False, "max_tokens", 0.2, "json_object"),
    "vllm": ("http://localhost:8000/v1/chat/completions", False, "max_tokens", 0.2, "json_object"),
}


@pytest.mark.parametrize("name", sorted(EXPECT))
def test_preset_request_shape(name):
    url, auth, token_param, temp, rf = EXPECT[name]
    rec = Recorder()
    p = provider(name, rec, model="" if presets.PRESETS[name].heavy else "local-model")
    resp = ask(p)
    assert resp.json() == {"ok": True}
    req, body = rec.requests[0], rec.bodies[0]
    assert str(req.url) == url and req.method == "POST"
    assert (req.headers.get("authorization") == f"Bearer {KEY}") if auth else ("authorization" not in req.headers)
    assert body[token_param] == 4096 and ({"max_tokens", "max_completion_tokens"} - {token_param}).isdisjoint(body)
    assert body.get("temperature") == temp
    assert (body.get("response_format") or {}).get("type") == rf
    if rf == "json_schema":
        assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    # 提示中始终给出 JSON 约束（不支持 JSON 模式时只靠提示与宽容解析）
    assert body["messages"][0]["role"] == "system" and "JSON Schema" in body["messages"][0]["content"]
    assert body["model"] == (presets.PRESETS[name].heavy or "local-model")


def test_generic_openai_compat_needs_base_url_and_model():
    with pytest.raises(ValueError, match="base_url"):
        OpenAICompatProvider("openai_compat", model="m")
    with pytest.raises(ValueError, match="模型名称"):
        OpenAICompatProvider("openai_compat", base_url="http://127.0.0.1:9/v1")
    rec = Recorder()
    p = provider("my-gateway", rec, model="m", base_url="http://127.0.0.1:9/v1", api_key_env="OPENAI_COMPAT_API_KEY")
    ask(p)
    assert p.name == "my-gateway" and str(rec.requests[0].url) == "http://127.0.0.1:9/v1/chat/completions"


def test_temperature_clamped_to_provider_range():
    rec = Recorder()
    p = provider("minimax", rec, temperature=0.0)
    ask(p)
    ask(p, temperature=1.7)
    assert [b["temperature"] for b in rec.bodies] == [0.01, 1.0]
    rec2 = Recorder()
    ask(provider("zhipu", rec2, temperature=1.5))
    assert rec2.bodies[0]["temperature"] == 1.0
    rec3 = Recorder()
    ask(provider("deepseek", rec3, temperature=1.5))
    assert rec3.bodies[0]["temperature"] == 1.5


def test_gpt_reasoning_models_tool_calls_still_work():
    def handler(req, body):
        msg_calls = [{"id": "call_1", "type": "function", "function": {"name": "task_list", "arguments": "{\"limit\": 3}"}}]
        data = completion(body["model"], content=None, tool_calls=msg_calls)
        data["choices"][0]["finish_reason"] = "tool_calls"
        return httpx.Response(200, json=data)

    rec = Recorder(handler)
    p = provider("gpt", rec)
    tools = [ToolDef("task_list", "列出任务", {"type": "object", "properties": {"limit": {"type": "integer"}}})]
    r = p.complete([ChatMessage("user", "列出任务")], system=SYSTEM, tools=tools)
    body = rec.bodies[0]
    assert [c.name for c in r.tool_calls] == ["task_list"] and r.tool_calls[0].arguments == {"limit": 3}
    assert body["tools"][0]["function"]["name"] == "task_list" and body["tool_choice"] == "auto"
    assert "temperature" not in body and "max_tokens" not in body and body["max_completion_tokens"] == 4096
    assert "response_format" not in body


# ====================================================================== 思考内容
def test_deepseek_reasoner_ignores_reasoning_content():
    reasoning = "先分析：用户要求 {\"ok\": false} 吗？不，应该输出 true。"

    def handler(req, body):
        return httpx.Response(200, json=completion(body["model"], content='{"ok": true}', reasoning_content=reasoning))

    rec = Recorder(handler)
    audit: list[tuple[str, dict]] = []
    p = provider("deepseek", rec, model="deepseek-reasoner")
    router = ModelRouter(GongwenConfig(), EgressGateway(EnvironmentRoute.PUBLIC_DEV, ["api.deepseek.com"]), audit=lambda t, pl: audit.append((t, pl)), providers={"*": p})
    resp = router.call("heavy", [ChatMessage("user", "测试")], system=SYSTEM, json_schema=SCHEMA, clearances=[Clearance.PUBLIC])
    assert resp.text == '{"ok": true}' and resp.json() == {"ok": True}
    body = rec.bodies[0]
    assert "temperature" not in body and "response_format" not in body  # 推理型号：不发送
    assert p.last_json_mode == "prompt"
    # 思考内容不进入答案，也不进入审计
    assert "先分析" not in json.dumps(audit, ensure_ascii=False)
    assert [t for t, _ in audit] == ["model.request", "model.response"]


@pytest.mark.parametrize(
    "content,expected",
    [
        ('<think>推理过程里也有 {"ok": false} 这样的花括号</think>\n{"ok": true}', '{"ok": true}'),
        ('<thinking>想一想</thinking>{"ok": true}', '{"ok": true}'),
        ('<THINK>大写标签</THINK>\n```json\n{"ok": true}\n```', '```json\n{"ok": true}\n```'),
        ('只有闭合标签的思考 {"ok": false}</think>\n{"ok": true}', '{"ok": true}'),
        ('{"ok": true}\n<think>输出被截断的思考 {"ok": false', '{"ok": true}'),
        ('{"ok": true}', '{"ok": true}'),
    ],
)
def test_think_tags_stripped_before_json(content, expected):
    rec = Recorder(lambda req, body: httpx.Response(200, json=completion(body["model"], content=content)))
    resp = ask(provider("minimax", rec))
    assert resp.text == expected and resp.json() == {"ok": True}


def test_strip_reasoning_and_parse_json_helpers():
    assert strip_reasoning("<think>全部是思考</think>") == ""
    assert strip_reasoning("<think>被截断") == ""
    assert strip_reasoning("正文不含标签\n") == "正文不含标签\n"  # 无标签时原样返回
    assert parse_json('<think>{"x": 1}</think>{"ok": true}') == {"ok": True}


# ====================================================================== 参数被拒时的回退
def test_response_format_rejected_retries_once_without_it():
    def handler(req, body):
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "This model does not support response_format of type json_object", "type": "invalid_request_error"}})
        return httpx.Response(200, json=completion(body["model"]))

    rec = Recorder(handler)
    p = provider("deepseek", rec)
    assert ask(p).json() == {"ok": True}
    assert [("response_format" in b) for b in rec.bodies] == [True, False]
    assert p.last_json_mode == "prompt"
    ask(p)  # 记住：此后不再发送 response_format，不再多付一次失败请求
    assert len(rec.bodies) == 3 and "response_format" not in rec.bodies[2]


def test_temperature_and_max_tokens_rejections_are_adjusted():
    def handler(req, body):
        if "temperature" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported value: 'temperature' does not support 0.2 with this model."}})
        if "max_tokens" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead."}})
        return httpx.Response(200, json=completion(body["model"]))

    rec = Recorder(handler)
    p = provider("openai_compat", rec, model="o-model", base_url="http://127.0.0.1:9/v1")
    assert ask(p).json() == {"ok": True}
    final = rec.bodies[-1]
    assert "temperature" not in final and "max_tokens" not in final and final["max_completion_tokens"] == 4096
    assert len(rec.bodies) == 3


def test_unrelated_400_fails_without_retry_and_key_is_redacted():
    def handler(req, body):  # 恶意或有缺陷的服务把请求头回显到错误信息里
        return httpx.Response(400, json={"error": {"message": f"model not found; got header {req.headers.get('authorization')}"}})

    rec = Recorder(handler)
    p = provider("deepseek", rec, model="no-such-model")
    with pytest.raises(ModelHTTPError) as ei:
        ask(p)
    assert ei.value.status == 400 and len(rec.bodies) == 1
    assert KEY not in str(ei.value) and KEY not in ei.value.detail and "***" in str(ei.value)


def test_glm_thinking_switch_and_overrides():
    rec = Recorder()
    ask(provider("glm", rec, thinking=True))
    ask(provider("glm", rec, thinking=False))
    ask(provider("glm", rec))
    assert [b.get("thinking") for b in rec.bodies] == [{"type": "enabled"}, {"type": "disabled"}, None]
    with pytest.raises(ValueError, match="不支持思考开关"):
        provider("deepseek", rec, thinking=True)
    rec2 = Recorder()
    ask(provider("deepseek", rec2, json_mode="none", extra_body={"top_p": 0.9, "model": "hijack", "stream": True}))
    body = rec2.bodies[0]
    assert "response_format" not in body and body["top_p"] == 0.9 and body["model"] == "deepseek-chat" and "stream" not in body
    with pytest.raises(ValueError, match="json_mode"):
        provider("deepseek", rec2, json_mode="xml")


# ====================================================================== Claude
class _FakeStream:
    def __init__(self, sink: list, kwargs: dict):
        sink.append(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        block = SimpleNamespace(type="text", text='{"ok": true}')
        return SimpleNamespace(stop_reason="end_turn", content=[block], usage=SimpleNamespace(input_tokens=3, output_tokens=2), model="m")


def _fake_anthropic_client(sink: list, models: list | None = None):
    beta = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: _FakeStream(sink, kw)))
    return SimpleNamespace(beta=beta, models=SimpleNamespace(list=lambda: iter(models or [])))


@pytest.mark.parametrize("model,legacy", [("claude-opus-5-5", False), ("claude-sonnet-5-5", False), ("claude-haiku-4-5", True), ("claude-haiku-4-5-20251001", True), ("claude-sonnet-4-5", True)])
def test_claude_effort_and_fallbacks_only_on_supported_models(model, legacy):
    pytest.importorskip("anthropic")
    from gongwen.llm.anthropic_provider import FALLBACK_BETA, AnthropicProvider

    sink: list[dict] = []
    p = AnthropicProvider(model=model, client=_fake_anthropic_client(sink))
    assert p.complete([ChatMessage("user", "测试")], system=SYSTEM, json_schema=SCHEMA).json() == {"ok": True}
    p.complete([ChatMessage("user", "测试")], system=SYSTEM)
    with_schema, plain = sink
    assert with_schema["output_config"]["format"]["type"] == "json_schema"
    if legacy:
        assert "effort" not in with_schema["output_config"] and "output_config" not in plain
        assert "fallbacks" not in with_schema and "betas" not in with_schema
    else:
        assert with_schema["output_config"]["effort"] == "high" and plain["output_config"] == {"effort": "high"}
        assert with_schema["fallbacks"] == "default" and with_schema["betas"] == [FALLBACK_BETA]
    assert "temperature" not in with_schema and "thinking" not in with_schema


def test_claude_list_models_via_sdk_against_local_server():
    pytest.importorskip("anthropic")
    from gongwen.llm.anthropic_provider import AnthropicProvider

    srv = FakeAPI()
    try:
        srv.models = {
            "data": [
                {"type": "model", "id": "claude-opus-5-5", "display_name": "Claude Opus 5.5", "created_at": "2026-01-01T00:00:00Z"},
                {"type": "model", "id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5", "created_at": "2025-10-01T00:00:00Z"},
            ],
            "has_more": False,
            "first_id": "claude-opus-5-5",
            "last_id": "claude-haiku-4-5",
        }
        p = AnthropicProvider(base_url=srv.url, api_key_env="ANTHROPIC_API_KEY", max_retries=0)
        assert p.list_models() == [{"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5"}, {"id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5"}]
        assert srv.seen[-1]["path"].startswith("/v1/models") and srv.seen[-1]["headers"].get("x-api-key") == KEY
        srv.models_status = 404
        with pytest.raises(ModelListUnsupported):
            p.list_models()
    finally:
        srv.close()


# ====================================================================== 型号列表（OpenAI 兼容）与网关
def test_openai_compat_list_models_parsing_and_errors():
    def handler(req, body):
        assert req.method == "GET" and str(req.url) == "https://api.deepseek.com/models"
        return httpx.Response(200, json={"object": "list", "data": [{"id": "deepseek-chat", "owned_by": "deepseek"}, {"id": "deepseek-reasoner", "owned_by": "deepseek"}]})

    rec = Recorder(handler)
    assert provider("deepseek", rec).list_models() == [{"id": "deepseek-chat", "owned_by": "deepseek"}, {"id": "deepseek-reasoner", "owned_by": "deepseek"}]
    assert rec.requests[0].headers["authorization"] == f"Bearer {KEY}"
    with pytest.raises(ModelListUnsupported):
        provider("zhipu", Recorder(lambda req, body: httpx.Response(404, json={"error": "not found"}))).list_models()
    with pytest.raises(ModelHTTPError) as ei:
        provider("minimax", Recorder(lambda req, body: httpx.Response(401, text=f"bad key {req.headers['authorization']}"))).list_models()
    assert ei.value.status == 401 and KEY not in str(ei.value)
    # 也接受 {"models": [...]} 与字符串列表
    assert provider("qwen", Recorder(lambda r, b: httpx.Response(200, json={"models": ["qwen-plus"]}))).list_models() == [{"id": "qwen-plus"}]


def test_list_models_goes_through_egress_gateway():
    rec = Recorder(lambda req, body: httpx.Response(200, json={"data": [{"id": "x"}]}))
    p = provider("deepseek", rec)
    audit: list[str] = []
    blocked = ModelRouter(GongwenConfig(), EgressGateway(EnvironmentRoute.PUBLIC_DEV, []), providers={"*": p})
    with pytest.raises(ModelUnavailable, match="白名单"):
        blocked.list_models()
    assert rec.requests == []  # 网关拒绝在发出请求之前
    ok = ModelRouter(GongwenConfig(), EgressGateway(EnvironmentRoute.PUBLIC_DEV, ["api.deepseek.com"]), audit=lambda t, pl: audit.append(t), providers={"*": p})
    assert ok.list_models() == [{"id": "x", "owned_by": ""}] and audit == ["model.list"]


def test_agent_route_and_offline_flag(tmp_path):
    cfg = GongwenConfig.model_validate({"models": {"ds": {"provider": "deepseek"}}, "routing": {"agent": "ds"}})
    router = ModelRouter(cfg, EgressGateway(EnvironmentRoute.PUBLIC_DEV, []))
    assert router.describe()["agent"] == "deepseek:deepseek-chat" and router.describe()["heavy"] == "offline"
    ws = tmp_path / "ws"
    (ws / ".gongwen").mkdir(parents=True)
    (ws / ".gongwen" / "config.toml").write_text('[models.ds]\nprovider = "deepseek"\n[routing]\nagent = "ds"\nheavy = "ds"\n', encoding="utf-8")
    args = build_parser().parse_args(["-C", str(ws), "--offline", "doctor"])
    assert set(make_engine(args).rt.router().describe().values()) == {"offline"}  # --offline 同样关闭对话代理的路由


# ====================================================================== 配置写回
def test_config_edit_preserves_comments_and_rejects_duplicates():
    text = '# 顶部注释\n[egress]\nallowed_hosts = []   # 行尾注释 ["例子"]\n\n# 下一节的说明\n[budget]\nmax_model_calls = 60\n'
    out = set_key(text, "egress", "allowed_hosts", ["api.deepseek.com"])
    assert 'allowed_hosts = ["api.deepseek.com"]  # 行尾注释 ["例子"]' in out and "# 顶部注释" in out and "# 下一节的说明" in out
    out = set_key(out, "routing", "heavy", "ds")
    out = upsert_table(out, "models.ds", {"provider": "deepseek", "name": "deepseek-chat"}, comment="# 说明", marker="# 说明")
    with pytest.raises(FileExistsError):
        upsert_table(out, "models.ds", {"provider": "deepseek"})
    out = upsert_table(out, "models.ds", {"provider": "deepseek", "name": "deepseek-reasoner"}, comment="# 说明", replace=True, marker="# 说明")
    data = tomllib.loads(out)
    assert data["models"]["ds"]["name"] == "deepseek-reasoner" and data["routing"]["heavy"] == "ds" and data["budget"]["max_model_calls"] == 60
    assert out.count("[models.ds]") == 1 and out.count("# 说明") == 1
    multi = '[egress]\nallowed_hosts = [\n  "a.example",\n  "b.example",\n]\nblock_all = false\n'
    out = set_key(multi, "egress", "allowed_hosts", ["a.example", "c.example"])
    assert tomllib.loads(out)["egress"] == {"allowed_hosts": ["a.example", "c.example"], "block_all": False}


def _ws(tmp_path) -> list[str]:
    ws = ["-C", str(tmp_path / "ws")]
    assert cli([*ws, "init"]) == 0
    return ws


def test_cli_model_add_round_trip(tmp_path, capsys):
    ws = _ws(tmp_path)
    cfg_path = tmp_path / "ws" / ".gongwen" / "config.toml"
    original = cfg_path.read_text(encoding="utf-8")
    capsys.readouterr()
    assert cli([*ws, "model", "add", "ds", "--preset", "deepseek", "--role", "heavy"]) == 0
    out = capsys.readouterr().out
    assert 'allowed_hosts = ["api.deepseek.com"]' in out and "出网网关拒绝" in out and KEY not in out
    text = cfg_path.read_text(encoding="utf-8")
    # 用户的其他设置与注释保留，新增内容只在末尾追加
    assert text.startswith(original.rstrip("\n")) and "# 公文智能体配置（工作区级）" in text
    cfg = load_config(tmp_path / "ws")
    assert cfg.models["ds"].provider == "deepseek" and cfg.models["ds"].name == "deepseek-chat" and cfg.models["ds"].api_key_env == "DEEPSEEK_API_KEY"
    assert cfg.routing.heavy == "ds" and cfg.egress.allowed_hosts == [] and cfg.models["ds"].max_clearance == Clearance.PUBLIC
    assert KEY not in text  # 密钥绝不写入配置
    # 同名再加：须 --force
    assert cli([*ws, "model", "add", "ds", "--preset", "deepseek"]) == 2
    assert cli([*ws, "model", "add", "ds", "--preset", "deepseek", "--model", "deepseek-reasoner", "--role", "heavy,reviewer", "--force", "--allow-egress"]) == 0
    text = cfg_path.read_text(encoding="utf-8")
    cfg = load_config(tmp_path / "ws")
    assert cfg.models["ds"].name == "deepseek-reasoner" and cfg.routing.reviewer == "ds"
    assert cfg.egress.allowed_hosts == ["api.deepseek.com"] and text.count("[models.ds]") == 1 and text.splitlines().count("[routing]") == 1
    assert "# 例如" in text  # allowed_hosts 的行尾注释保留
    # 别名、区域、思考开关；只用于轻量角色时取轻量型号
    assert cli([*ws, "model", "add", "g", "--preset", "glm", "--region", "intl", "--thinking", "on", "--role", "light", "--allow-egress"]) == 0
    g = load_config(tmp_path / "ws").models["g"]
    assert (g.provider, g.name, g.base_url, g.thinking) == ("zhipu", "glm-4.5-air", "https://api.z.ai/api/paas/v4", True)
    assert "api.z.ai" in load_config(tmp_path / "ws").egress.allowed_hosts
    capsys.readouterr()
    # 参数错误：不写入任何内容
    before = cfg_path.read_text(encoding="utf-8")
    assert cli([*ws, "model", "add", "x", "--preset", "nope"]) == 2
    assert cli([*ws, "model", "add", "x", "--preset", "deepseek", "--thinking", "on"]) == 2
    assert cli([*ws, "model", "add", "x", "--preset", "ollama"]) == 2  # 本地部署须指定型号
    assert cli([*ws, "model", "add", "default", "--preset", "deepseek"]) == 2
    assert cli([*ws, "model", "add", "x", "--preset", "deepseek", "--region", "intl"]) == 2
    assert cfg_path.read_text(encoding="utf-8") == before
    err = capsys.readouterr().err
    assert "未知预设" in err and "--thinking" in err and "--model" in err


def test_cli_model_add_refuses_key_as_env_name(tmp_path, capsys, monkeypatch):
    ws = _ws(tmp_path)
    monkeypatch.setenv("MY_PROVIDER_KEY", "ABCDEF0123456789SECRET")
    assert cli([*ws, "model", "add", "x", "--preset", "deepseek", "--api-key-env", "ABCDEF0123456789SECRET"]) == 2
    assert cli([*ws, "model", "add", "x", "--preset", "deepseek", "--api-key-env", "sk-live-abc"]) == 2
    assert "ABCDEF0123456789SECRET" not in (tmp_path / "ws" / ".gongwen" / "config.toml").read_text(encoding="utf-8")
    assert "不是密钥本身" in capsys.readouterr().err
    assert cli([*ws, "model", "add", "x", "--preset", "deepseek", "--api-key-env", "MY_PROVIDER_KEY"]) == 0


def test_cli_model_add_requires_init(tmp_path, capsys):
    assert cli(["-C", str(tmp_path / "empty"), "model", "add", "ds", "--preset", "deepseek"]) == 2
    assert "gongwen init" in capsys.readouterr().err


def test_cli_model_list_hides_keys(tmp_path, capsys, monkeypatch):
    ws = _ws(tmp_path)
    assert cli([*ws, "model", "add", "ds", "--preset", "deepseek", "--role", "heavy", "--allow-egress"]) == 0
    assert cli([*ws, "model", "add", "gpt", "--preset", "gpt", "--role", "agent"]) == 0
    monkeypatch.delenv("OPENAI_API_KEY")
    capsys.readouterr()
    assert cli([*ws, "model", "list"]) == 0
    out = capsys.readouterr().out
    assert KEY not in out and "DEEPSEEK_API_KEY（已设置）" in out and "OPENAI_API_KEY（未设置）" in out and "已允许" in out and "未允许" in out
    assert cli([*ws, "model", "list", "--json"]) == 0
    raw = capsys.readouterr().out
    rows = {r["name"]: r for r in json.loads(raw)}
    assert KEY not in raw
    assert rows["ds"]["key_set"] is True and rows["ds"]["egress_allowed"] is True and rows["ds"]["roles"] == ["heavy"]
    assert rows["gpt"]["key_set"] is False and rows["gpt"]["egress_allowed"] is False and rows["gpt"]["model"] == "gpt-6.1-sol" and rows["gpt"]["roles"] == ["agent"]
    lines = doctor_lines(load_config(tmp_path / "ws"))
    assert any("模型配置 ds" in s and "已设置" in s for s in lines) and all(KEY not in s for s in lines)
    # 未配置任何模型
    assert cli(["-C", str(tmp_path / "w2"), "model", "list"]) == 0
    assert "未配置外部模型" in capsys.readouterr().out


def test_cli_model_test_egress_blocked_and_missing_key(tmp_path, capsys, monkeypatch):
    ws = _ws(tmp_path)
    assert cli([*ws, "model", "add", "ds", "--preset", "deepseek", "--role", "heavy"]) == 0
    capsys.readouterr()
    # 主机未列入白名单：网关在发出任何请求前拒绝（本测试环境不访问外网）
    assert cli([*ws, "model", "test", "ds"]) == 4
    out = capsys.readouterr().out
    assert "白名单" in out and 'allowed_hosts = ["api.deepseek.com"]' in out and KEY not in out
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert cli([*ws, "model", "test", "ds"]) == 2
    out = capsys.readouterr().out
    assert "未设置环境变量 DEEPSEEK_API_KEY" in out and "export DEEPSEEK_API_KEY=" in out
    assert cli([*ws, "model", "test", "nope"]) == 2
    assert "配置中没有模型 nope" in capsys.readouterr().err


# ====================================================================== 本机模拟服务：model test / model remote
class FakeAPI:
    """本机回环地址上的模拟服务（本机地址免列白名单），实现 /v1/chat/completions 与 /v1/models。"""

    def __init__(self):
        self.seen: list[dict] = []
        self.chat_status = 200
        self.chat_reply: Callable[[dict], Any] = lambda body: completion(body["model"] + "-0601")
        self.models: Any = {"object": "list", "data": [{"id": "gpt-6-luna", "owned_by": "openai"}, {"id": "gpt-6.1-sol", "owned_by": "openai"}]}
        self.models_status = 200
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _reply(self, status: int, payload: Any):
                raw = json.dumps(payload, ensure_ascii=False).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # 客户端已超时断开

            def do_GET(self):
                api.seen.append({"method": "GET", "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
                if self.path.split("?")[0].endswith("/models") and api.models_status == 200:
                    self._reply(200, api.models)
                else:
                    self._reply(api.models_status if api.models_status != 200 else 404, {"error": {"message": "not found"}})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                api.seen.append({"method": "POST", "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                if api.chat_status != 200:
                    self._reply(api.chat_status, {"error": {"message": f"echo {self.headers.get('Authorization')}"}})
                else:
                    self._reply(200, api.chat_reply(body))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def api():
    srv = FakeAPI()
    yield srv
    srv.close()


def test_cli_model_test_success_through_governed_router(tmp_path, capsys, api):
    ws = _ws(tmp_path)
    assert cli([*ws, "model", "add", "gpt", "--preset", "openai", "--base-url", f"{api.url}/v1", "--role", "heavy"]) == 0
    capsys.readouterr()
    assert cli([*ws, "model", "test", "gpt"]) == 0
    out = capsys.readouterr().out
    assert "耗时" in out and "返回型号：gpt-6.1-sol-0601" in out and "json_schema" in out and "可解析且符合约定" in out
    body = api.seen[-1]["body"]
    assert api.seen[-1]["path"] == "/v1/chat/completions" and api.seen[-1]["headers"]["authorization"] == f"Bearer {KEY}"
    assert body["max_completion_tokens"] == 4096 and "temperature" not in body and body["response_format"]["type"] == "json_schema"
    assert cli([*ws, "model", "test", "gpt", "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["ok"] and res["json_ok"] and res["json_mode"] == "json_schema" and res["returned_model"] == "gpt-6.1-sol-0601"
    # 审计：只记哈希，不含密钥与提示原文
    log = (tmp_path / "ws" / ".gongwen" / "audit" / "model-tests.jsonl").read_text(encoding="utf-8")
    kinds = [json.loads(line)["type"] for line in log.splitlines()]
    assert {"egress.decision", "model.request", "model.response"} <= set(kinds)
    assert KEY not in log and "连通性测试" not in log


@pytest.mark.parametrize("status,expect", [(401, "密钥无效"), (403, "无权访问"), (404, "型号或地址不存在"), (400, "请求被拒绝")])
def test_cli_model_test_classifies_http_errors(tmp_path, capsys, api, status, expect):
    ws = _ws(tmp_path)
    assert cli([*ws, "model", "add", "m", "--preset", "minimax", "--base-url", f"{api.url}/v1", "--role", "heavy"]) == 0
    api.chat_status = status
    capsys.readouterr()
    assert cli([*ws, "model", "test", "m"]) == 1
    out = capsys.readouterr().out
    assert expect in out and KEY not in out  # 服务端回显的密钥被打码


def test_cli_model_test_timeout(tmp_path, capsys, api):
    ws = _ws(tmp_path)
    assert cli([*ws, "model", "add", "m", "--preset", "deepseek", "--base-url", f"{api.url}/v1"]) == 0

    def slow(body):
        import time

        time.sleep(1.5)
        return completion(body["model"])

    api.chat_reply = slow
    capsys.readouterr()
    assert cli([*ws, "-c", "models.m.timeout=0.3", "-c", "models.m.max_retries=0", "model", "test", "m"]) == 1
    assert "请求超时" in capsys.readouterr().out


def test_cli_model_remote(tmp_path, capsys, api):
    ws = _ws(tmp_path)
    assert cli([*ws, "model", "add", "gpt", "--preset", "gpt", "--base-url", f"{api.url}/v1"]) == 0
    capsys.readouterr()
    assert cli([*ws, "model", "remote", "gpt"]) == 0
    out = capsys.readouterr().out
    assert "* gpt-6.1-sol" in out and "  gpt-6-luna" in out and KEY not in out
    assert api.seen[-1]["method"] == "GET" and api.seen[-1]["path"] == "/v1/models"
    assert cli([*ws, "model", "remote", "gpt", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [m["id"] for m in data["models"]] == ["gpt-6-luna", "gpt-6.1-sol"] and data["configured"] == "gpt-6.1-sol"
    # 未提供列表接口：给出说明与预设中的已知型号，不报堆栈
    api.models_status = 404
    assert cli([*ws, "model", "remote", "gpt"]) == 0
    out = capsys.readouterr().out
    assert "未提供模型列表接口" in out and "gpt-6-astra" in out
    # 非本机地址未列入白名单：拒绝且不发请求
    assert cli([*ws, "model", "add", "ds", "--preset", "deepseek"]) == 0
    n = len(api.seen)
    assert cli([*ws, "model", "remote", "ds"]) == 4
    assert len(api.seen) == n and "白名单" in capsys.readouterr().out

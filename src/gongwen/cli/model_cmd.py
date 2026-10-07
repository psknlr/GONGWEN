"""gongwen model：模型接入——查看预设、添加具名模型、列出配置、连通测试、查询服务商当前可用型号。

密钥只从环境变量读取：本命令不读取、不写入、不显示密钥内容，只显示对应环境变量是否已设置。
连通测试与型号查询同样经过出网网关（白名单）、预算与审计（审计只记哈希，写入数据目录 audit/model-tests.jsonl）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import tomllib
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_BLOCKED = 0, 1, 2, 4
ROLE_CHOICES = ("heavy", "light", "reviewer", "agent")
ROLE_TITLES = {"heavy": "起草与修订", "light": "分类与抽取", "reviewer": "独立审校", "agent": "对话代理"}
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
MARKER = "# 由 gongwen model add 添加"
DEFAULT_NAME = "default"

TEST_SYSTEM = "你是接口连通性测试助手。"
TEST_PROMPT = "这是一次接口连通性测试。请输出 JSON 对象：ok 为 true，echo 为“公文”。"
TEST_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}, "echo": {"type": "string"}}, "required": ["ok", "echo"], "additionalProperties": False}


# ---------------------------------------------------------------- 公共
def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _pad(s: str, n: int) -> str:
    return s + " " * max(0, n - _width(s))


def _table(rows: list[list[str]]) -> str:
    widths = [max(_width(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(_pad(c, widths[i]) if i < len(r) - 1 else c for i, c in enumerate(r)).rstrip() for r in rows)


def _load(args):
    from ..kernel.config import load_config
    from .main import _merge, _parse_override, workspace_of

    overrides: dict[str, Any] = {}
    for item in args.config or []:
        overrides = _merge(overrides, _parse_override(item))
    return load_config(workspace_of(args), profile=args.profile, overrides=overrides or None)


def _entries(cfg) -> list[tuple[str, Any, list[str]]]:
    """配置中的模型：(名称, ModelConfig, 使用它的角色)。[model] 记为 default（未路由的角色都用它）。"""
    from ..llm.router import ROLES

    roles: dict[str, list[str]] = {}
    for r in ROLES:
        target = getattr(cfg.routing, r, None)
        roles.setdefault(target if target in cfg.models else DEFAULT_NAME, []).append(r)
    out = [(n, mc, roles.get(n, [])) for n, mc in cfg.models.items()]
    if cfg.model.provider not in ("offline", "", None):
        out.insert(0, (DEFAULT_NAME, cfg.model, roles.get(DEFAULT_NAME, [])))
    return out


def _lookup(cfg, name: str):
    if name in cfg.models:
        return cfg.models[name]
    if name == DEFAULT_NAME:
        return cfg.model
    known = "、".join(list(cfg.models) + [DEFAULT_NAME]) or "（无）"
    raise KeyError(f"配置中没有模型 {name}（已配置：{known}）。可用 gongwen model add {name} --preset … 添加")


def describe_endpoint(mc) -> dict[str, Any]:
    """不建立连接、不读取密钥内容：推算模型的地址、主机与密钥环境变量。"""
    from ..llm import presets

    offline = mc.provider in ("offline", "", None)
    preset = None if offline else (presets.resolve(mc.provider) or presets.generic())
    base = mc.base_url
    if preset is not None and not base:
        base = os.environ.get("ANTHROPIC_BASE_URL", "") if preset.protocol == "anthropic" else ""
        base = base or preset.base_url
    key_env = mc.api_key_env or (preset.api_key_env if preset else "")
    return {
        "preset": preset,
        "base_url": base,
        "host": (urlparse(base).hostname or "").lower() if base else "",
        "key_env": key_env,
        "key_set": bool(os.environ.get(key_env)) if key_env else None,
        "model": mc.name or (preset.heavy if preset else ""),
    }


def egress_state(cfg, host: str) -> tuple[bool, str]:
    from ..harness.egress import is_local_host

    if not host:
        return False, "—"
    if cfg.egress.block_all:
        return False, "禁止（egress.block_all）"
    if is_local_host(host):
        return True, "本机（免列白名单）"
    if host in {h.lower() for h in cfg.egress.allowed_hosts}:
        return True, "已允许"
    return False, "未允许（调用会被出网网关拒绝）"


def _allow_line(cfg, host: str) -> str:
    from ..kernel.config_edit import toml_value

    hosts = list(dict.fromkeys([*cfg.egress.allowed_hosts, host]))
    return f"allowed_hosts = {toml_value(hosts)}"


def _key_hint(env: str) -> str:
    return f"请在本机终端设置：export {env}=…（密钥只从环境变量读取；不要写入配置文件、脚本或提交到版本库）"


def _classify(msg: str) -> str:
    """把接口错误归类为中文说明（原始信息另行显示，已打码）。"""
    m = re.search(r"HTTP (\d{3})", msg)
    code = int(m.group(1)) if m else 0
    if code == 401:
        return "密钥无效或已过期（HTTP 401）：请核对环境变量中的密钥是否属于该服务商、是否已启用"
    if code == 402:
        return "账户余额不足或未开通付费（HTTP 402）"
    if code == 403:
        return "无权访问（HTTP 403）：账号未开通该型号、额度不足或所在地区不受支持"
    if code == 404:
        return "型号或地址不存在（HTTP 404）：请核对型号名称与 base_url（可用 gongwen model remote <名称> 查询当前可用型号）"
    if code == 429:
        return "限流或额度用尽（HTTP 429）：稍后重试，或检查账户配额"
    if 300 <= code < 400:
        return f"服务端要求重定向（HTTP {code}）：为防止材料被转发到未获准的主机，不跟随重定向；请核对 base_url"
    if code >= 500:
        return f"服务端错误（HTTP {code}）：稍后重试"
    if code == 400:
        return "请求被拒绝（HTTP 400）：型号名称或参数可能不被该服务接受"
    if "超时" in msg:
        return "请求超时：网络不通或服务响应慢；可在模型配置中调大 timeout"
    if "连接失败" in msg:
        return "无法连接：请检查网络、代理设置与 base_url"
    return "接口调用失败"


def _governed_router(cfg, provider):
    """只含这一个模型的网关：出网网关、预算与审计照常生效（审计写入数据目录，只记哈希）。"""
    from ..harness.budget import BudgetGuard
    from ..harness.egress import EgressGateway
    from ..harness.session import SessionLog
    from ..llm.router import ModelRouter

    log = SessionLog(Path(cfg.environment.data_dir) / "audit" / "model-tests.jsonl")
    egress = EgressGateway(cfg.environment.route, cfg.egress.allowed_hosts, cfg.egress.block_all, audit=lambda t, p: log.append(t, p))
    return ModelRouter(cfg, egress, BudgetGuard(cfg.budget), audit=lambda t, p: log.append(t, p, actor="model-gateway"), providers={"*": provider})


def _build(cfg, name: str, as_json: bool):
    """按名称建立适配；密钥未设置等配置问题给出中文说明。返回 (适配, ModelConfig, 端点信息) 或退出码。"""
    from ..llm.router import build_provider

    mc = _lookup(cfg, name)
    info = describe_endpoint(mc)
    try:
        p = build_provider(mc)
    except ValueError as exc:
        msg = str(exc)
        hint = _key_hint(info["key_env"]) if msg.startswith("未设置环境变量") and info["key_env"] else ""
        _out(as_json, {"name": name, "ok": False, "error": msg, "hint": hint}, [f"模型 {name} 未就绪：{msg}", *([hint] if hint else [])])
        return EXIT_USAGE
    if p is None:
        _out(as_json, {"name": name, "ok": False, "error": "offline"}, [f"模型 {name} 为 offline：不调用任何外部模型（确定性路径）"])
        return EXIT_USAGE
    return p, mc, info


def _out(as_json: bool, data: dict[str, Any], lines: list[str]) -> None:
    if as_json:
        sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    else:
        for line in lines:
            print(line)


def _egress_denied(cfg, name: str, info: dict, exc: Exception, as_json: bool) -> int:
    host = info["host"]
    lines = [f"出网被拒：{exc}"]
    if host and "白名单" in str(exc):
        lines += [
            "确认该服务可以接收公开材料后，在 .gongwen/config.toml 的 [egress] 中写入：",
            f"  {_allow_line(cfg, host)}",
            f"或运行：gongwen model add {name} --preset {info['preset'].name if info['preset'] else '…'} --force --allow-egress（会覆盖该模型条目）",
        ]
    _out(as_json, {"name": name, "ok": False, "error": str(exc), "egress_blocked": True, "host": host}, lines)
    return EXIT_BLOCKED


# ---------------------------------------------------------------- presets
def cmd_model_presets(args) -> int:
    from ..llm import presets

    if args.json:
        rows = [
            {
                "name": p.name,
                "display": p.display,
                "protocol": p.protocol,
                "base_url": p.base_url,
                "regions": p.regions,
                "api_key_env": p.api_key_env,
                "heavy": p.heavy,
                "light": p.light,
                "models": list(p.models),
                "quirks": p.quirks.describe(),
                "notes": p.notes,
            }
            for p in presets.PRESETS.values()
        ]
        sys.stdout.write(json.dumps({"presets": rows, "aliases": presets.ALIASES}, ensure_ascii=False) + "\n")
        return EXIT_OK
    rows = [["预设", "显示名", "地址", "密钥环境变量", "默认型号（强 / 轻）"]]
    for p in presets.PRESETS.values():
        default = p.heavy if p.heavy == p.light else f"{p.heavy} / {p.light}"
        rows.append([p.name, p.display, p.base_url or "（须 --base-url）", p.api_key_env or "—", default or "（须 --model）"])
    print(_table(rows))
    print("\n说明：")
    for p in presets.PRESETS.values():
        extra = [p.quirks.describe()] if p.protocol == "openai_compat" else []
        if p.regions:
            extra.append("区域 " + "，".join(f"{k}={v}" for k, v in p.regions.items()))
        if p.models:
            extra.append("已知型号 " + "、".join(p.models))
        print(f"  {p.name}：{'；'.join(x for x in [p.notes, *extra] if x)}")
    print("\n别名：" + "，".join(presets.alias_lines()))
    print("型号会随服务商更新，以 gongwen model remote <名称> 查询到的为准。公共云模型默认只处理公开材料。")
    return EXIT_OK


# ---------------------------------------------------------------- add
def _roles(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for v in values or []:
        for r in (x.strip() for x in v.split(",")):
            if not r:
                continue
            if r == "all":
                out += list(ROLE_CHOICES)
            elif r in ROLE_CHOICES:
                out.append(r)
            else:
                raise ValueError(f"--role 只能是：{'、'.join(ROLE_CHOICES)}（或 all）；无效：{r}")
    return list(dict.fromkeys(out))


def cmd_model_add(args) -> int:
    from ..kernel.config import GongwenConfig
    from ..kernel.config_edit import set_key, upsert_table, write_atomic
    from ..llm import presets
    from ..schemas.common import Clearance
    from .main import _clearance, workspace_of

    name = args.name
    if not _NAME.match(name) or name == DEFAULT_NAME:
        raise ValueError("模型名称只能由字母、数字、下划线与连字符组成（不超过 40 个字符，且不能是 default）")
    preset = presets.resolve(args.preset)
    if preset is None:
        raise ValueError(f"未知预设：{args.preset}（可选：{'、'.join(presets.PRESETS)}；别名：{'、'.join(presets.ALIASES)}）。用 gongwen model presets 查看")
    roles = _roles(args.role)
    if args.region and args.base_url:
        raise ValueError("--region 与 --base-url 只能二选一")
    base_url = args.base_url or (preset.base_for(args.region) if args.region else "")
    effective_base = base_url or preset.base_url
    if not effective_base:
        raise ValueError(f"预设 {preset.name} 没有默认地址：请用 --base-url 指定")
    model_id = args.model_id or preset.default_model(roles)
    if not model_id:
        raise ValueError(f"预设 {preset.name} 没有默认型号：请用 --model 指定（添加后可用 gongwen model remote {name} 查询可用型号）")
    clearance = _clearance(args.max_clearance) or Clearance.PUBLIC
    thinking = None if args.thinking is None else args.thinking == "on"
    if thinking is not None and not preset.quirks_for(model_id).thinking_switch:
        raise ValueError(f"预设 {preset.name} 不支持 --thinking（目前仅智谱 GLM 提供思考开关）")
    key_env = args.api_key_env or preset.api_key_env
    if args.api_key_env:
        # 防止误把密钥本身当作变量名写进配置文件
        if not _ENV_NAME.match(args.api_key_env) or args.api_key_env in os.environ.values():
            raise ValueError("--api-key-env 应为环境变量的名称（大写字母、数字与下划线，如 DEEPSEEK_API_KEY），不是密钥本身；密钥不得写入配置文件")

    ws = workspace_of(args)
    path = ws / ".gongwen" / "config.toml"
    if not path.is_file():
        raise FileNotFoundError(f"未找到工作区配置 {path}：请先运行 gongwen init")
    before = _load(args)
    text = path.read_text(encoding="utf-8")
    try:
        ws_data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"工作区配置无法解析（{exc}）：请先修正 {path}") from exc

    entry: dict[str, Any] = {"provider": preset.name, "name": model_id}
    if base_url:
        entry["base_url"] = base_url
    if key_env:
        entry["api_key_env"] = key_env
    entry["max_clearance"] = clearance.value
    if thinking is not None:
        entry["thinking"] = thinking
    comment = f"{MARKER}：{preset.display}" + (f"；密钥从环境变量 {key_env} 读取，不写入本文件" if key_env else "")
    try:
        new = upsert_table(text, f"models.{name}", entry, comment=comment, replace=args.force, marker=MARKER)
    except FileExistsError as exc:
        raise ValueError(str(exc)) from exc
    for r in roles:
        new = set_key(new, "routing", r, name)

    host = (urlparse(effective_base).hostname or "").lower()
    allowed_now, _ = egress_state(before, host)
    added_host = False
    if args.allow_egress and not allowed_now and host and not before.egress.block_all:
        current = (ws_data.get("egress") or {}).get("allowed_hosts")
        hosts = list(current) if isinstance(current, list) else list(before.egress.allowed_hosts)
        new = set_key(new, "egress", "allowed_hosts", list(dict.fromkeys([*hosts, host])))
        added_host = True

    # 写回前校验：能解析、条目与预期一致、整体配置有效
    data = tomllib.loads(new)
    got = (data.get("models") or {}).get(name)
    if got != entry or any((data.get("routing") or {}).get(r) != name for r in roles):
        raise ValueError(f"无法安全地自动修改 {path}（格式较特殊）：请手工加入 [models.{name}]")
    GongwenConfig.model_validate(data)
    write_atomic(path, new)

    cfg = _load(args)
    allowed, state = egress_state(cfg, host)
    key_set = bool(os.environ.get(key_env)) if key_env else None
    print(f"已写入 {path}：[models.{name}]（{preset.display} · {model_id}）")
    if roles:
        print("  角色：" + "，".join(f"{r}（{ROLE_TITLES[r]}）→ {name}" for r in roles))
    else:
        print(f"  角色：未指定，该模型暂不会被使用；可加 --role heavy|light|reviewer|agent，或在 [routing] 中引用 \"{name}\"")
    if key_env:
        print(f"  密钥：从环境变量 {key_env} 读取（当前{'已设置' if key_set else '未设置'}）；密钥不写入配置文件")
        if not key_set:
            print(f"        {_key_hint(key_env)}")
    else:
        print("  密钥：无需密钥（本地部署）")
    if added_host:
        print(f"  出网：已将 {host} 加入 [egress] allowed_hosts")
    elif allowed:
        print(f"  出网：{state}")
    elif cfg.egress.block_all:
        print("  出网：当前配置禁止一切出网（egress.block_all = true），调用会被拒绝")
    else:
        print(f"  出网：主机 {host} 不在出网白名单内，调用会被出网网关拒绝（默认拒绝一切出网）。")
        print("        确认该服务可以接收公开材料后，在 [egress] 中写入：")
        print(f"          {_allow_line(cfg, host)}")
        print("        或重新运行本命令并加 --force --allow-egress。")
    print(f"  材料上限：{clearance.value}")
    if clearance != Clearance.PUBLIC and not (allowed and "本机" in state):
        print("  提示：公共云模型通常只应处理公开材料；提高材料上限须有单位制度依据（公开材料研发版中出网网关仍只放行公开材料）")
    if preset.protocol == "anthropic":
        try:
            __import__("anthropic")
        except ImportError:
            print('  提示：使用 Claude 需要安装可选依赖：pip install "gongwen[anthropic]"')
    print(f"下一步：gongwen model test {name}（一次最小调用，检查密钥、出网与 JSON 模式）")
    return EXIT_OK


# ---------------------------------------------------------------- list
def cmd_model_list(args) -> int:
    cfg = _load(args)
    rows_json = []
    rows = [["名称", "服务商", "型号", "角色", "密钥环境变量", "出网", "材料上限"]]
    for name, mc, roles in _entries(cfg):
        info = describe_endpoint(mc)
        allowed, state = egress_state(cfg, info["host"])
        key = "—（无需密钥）" if not info["key_env"] else f"{info['key_env']}（{'已设置' if info['key_set'] else '未设置'}）"
        rows.append([name, mc.provider if not info["preset"] else f"{info['preset'].name}", info["model"] or "—", "、".join(roles) or "—", key, state, mc.max_clearance.value])
        rows_json.append(
            {
                "name": name,
                "provider": mc.provider,
                "model": info["model"],
                "base_url": info["base_url"],
                "roles": roles,
                "api_key_env": info["key_env"],
                "key_set": info["key_set"],
                "egress_allowed": allowed,
                "max_clearance": mc.max_clearance.value,
            }
        )
    if args.json:
        sys.stdout.write(json.dumps(rows_json, ensure_ascii=False) + "\n")
        return EXIT_OK
    if len(rows) == 1:
        print("未配置外部模型（全部使用确定性路径）。用 gongwen model presets 查看可接入的服务商，gongwen model add 添加。")
        return EXIT_OK
    print(_table(rows))
    print("密钥只显示环境变量是否已设置，不显示内容。未路由的角色使用 default（[model]）。")
    return EXIT_OK


def doctor_lines(cfg) -> list[str]:
    """供 gongwen doctor 显示：每个已配置模型的密钥与出网状态。"""
    out = []
    for name, mc, roles in _entries(cfg):
        info = describe_endpoint(mc)
        _, state = egress_state(cfg, info["host"])
        key = "无需密钥" if not info["key_env"] else f"密钥 {info['key_env']} {'已设置' if info['key_set'] else '未设置'}"
        out.append(f"  模型配置 {name}：{mc.provider} {info['model'] or ''}　角色 {'、'.join(roles) or '—'}　{key}　出网 {state}　材料上限 {mc.max_clearance.value}")
    if not out:
        out.append("  模型配置：未配置外部模型（gongwen model presets 查看可接入的服务商）")
    return out


# ---------------------------------------------------------------- test
def cmd_model_test(args) -> int:
    from ..llm.base import ChatMessage, ModelCallFailed, ModelRefused, ModelUnavailable
    from ..schemas.common import Clearance

    cfg = _load(args)
    built = _build(cfg, args.name, args.json)
    if isinstance(built, int):
        return built
    p, mc, info = built
    router = _governed_router(cfg, p)
    head = f"模型 {args.name}（{p.name} · {p.model}）\n  地址：{p.endpoint}"
    t0 = time.monotonic()
    try:
        resp = router.call(
            "heavy",
            [ChatMessage("user", TEST_PROMPT)],
            system=TEST_SYSTEM,
            json_schema=TEST_SCHEMA,
            clearances=[Clearance.PUBLIC],
            purpose="model.test",
            template_id="model-test-v1",
            max_tokens=4096,  # 推理模型的思考也占输出额度：留足余量，按实际用量计费
        )
    except ModelUnavailable as exc:
        if not args.json:
            print(head)
        return _egress_denied(cfg, args.name, info, exc, args.json)
    except ModelRefused as exc:
        _out(args.json, {"name": args.name, "ok": False, "error": str(exc)}, [head, f"  模型拒答：{exc}（测试提示为无害内容，可重试）"])
        return EXIT_FAIL
    except ModelCallFailed as exc:
        why = _classify(str(exc))
        _out(args.json, {"name": args.name, "ok": False, "error": str(exc), "reason": why}, [head, f"  失败：{why}", f"  原始信息：{exc}"])
        return EXIT_FAIL
    latency = time.monotonic() - t0
    try:
        data = resp.json()
        json_ok = isinstance(data, dict) and data.get("ok") is True
    except ValueError:
        json_ok = False
    mode = getattr(p, "last_json_mode", "") or ("json_schema" if p.name == "anthropic" else "")
    mode_text = {"json_schema": "json_schema（结构化输出）", "json_object": "json_object", "prompt": "仅提示约束（服务商不支持或未启用 JSON 模式）"}.get(mode, mode or "—")
    result = {
        "name": args.name,
        "ok": True,
        "provider": p.name,
        "model": p.model,
        "endpoint": p.endpoint,
        "latency_ms": round(latency * 1000),
        "returned_model": resp.model,
        "json_mode": mode,
        "json_ok": json_ok,
        "usage": {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens},
    }
    _out(
        args.json,
        result,
        [
            head,
            f"  耗时：{latency:.2f} 秒",
            f"  返回型号：{resp.model}",
            f"  JSON：{mode_text}；输出{'可解析且符合约定' if json_ok else '不符合约定（办文时该模型步骤会回退确定性路径）'}",
            f"  用量：输入 {resp.usage.input_tokens} / 输出 {resp.usage.output_tokens} 令牌",
            "  审计：已记入数据目录 audit/model-tests.jsonl（只记哈希）",
        ],
    )
    return EXIT_OK


# ---------------------------------------------------------------- remote
def cmd_model_remote(args) -> int:
    from ..llm.base import ModelCallFailed, ModelListUnsupported, ModelUnavailable

    cfg = _load(args)
    built = _build(cfg, args.name, args.json)
    if isinstance(built, int):
        return built
    p, mc, info = built
    router = _governed_router(cfg, p)
    try:
        items = router.list_models("heavy")
    except ModelUnavailable as exc:
        return _egress_denied(cfg, args.name, info, exc, args.json)
    except ModelListUnsupported as exc:
        known = list(info["preset"].models) if info["preset"] else []
        lines = [f"{exc}。", "请查阅服务商文档确认型号名称，再用 gongwen model add … --model <型号> --force 修改。"]
        if known:
            lines.append("预设中记录的已知型号（仅供参考）：" + "、".join(known))
        _out(args.json, {"name": args.name, "supported": False, "error": str(exc), "known_models": known}, lines)
        return EXIT_OK
    except ModelCallFailed as exc:
        why = _classify(str(exc))
        _out(args.json, {"name": args.name, "supported": True, "ok": False, "error": str(exc), "reason": why}, [f"查询失败：{why}", f"原始信息：{exc}"])
        return EXIT_FAIL
    items = sorted(items, key=lambda x: x.get("id", ""))
    if args.json:
        sys.stdout.write(json.dumps({"name": args.name, "supported": True, "models": items, "configured": p.model}, ensure_ascii=False) + "\n")
        return EXIT_OK
    print(f"{p.name} 当前可用型号（{len(items)} 个；* 为本配置使用的型号）：")
    for it in items:
        mark = "*" if it.get("id") == p.model else " "
        extra = it.get("display_name") or it.get("owned_by") or ""
        print(f" {mark} {it.get('id')}" + (f"　{extra}" if extra else ""))
    if p.model not in {it.get("id") for it in items}:
        print(f"注意：配置中的型号 {p.model} 不在列表中，请核对（gongwen model add {args.name} --preset … --model <型号> --force）")
    return EXIT_OK


# ---------------------------------------------------------------- 解析器
def add_parser(sub) -> None:
    m = sub.add_parser("model", help="模型接入：预设、添加、列表、连通测试、查询可用型号").add_subparsers(dest="model_cmd", metavar="子命令")
    p = m.add_parser("presets", help="内置服务商预设（地址、密钥环境变量、默认型号、接口差异）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_model_presets)

    p = m.add_parser("add", help="把具名模型写入工作区配置（只记录密钥环境变量名，不写入密钥）")
    p.add_argument("name", help="模型名称（字母、数字、下划线、连字符），供 [routing] 引用")
    p.add_argument("--preset", required=True, help="预设：anthropic/openai/deepseek/zhipu/minimax/minimax_intl/qwen/moonshot/xai/ollama/vllm/openai_compat 或别名")
    p.add_argument("--model", dest="model_id", help="型号（默认取预设的默认型号）")
    p.add_argument("--base-url", help="接口地址（覆盖预设）")
    p.add_argument("--region", choices=["cn", "intl"], help="区域：cn 中国大陆站 / intl 国际站（预设提供时）")
    p.add_argument("--api-key-env", help="读取密钥的环境变量名（默认取预设）")
    p.add_argument("--role", action="append", help="使用该模型的角色：heavy、light、reviewer、agent 或 all（可重复或逗号分隔）")
    p.add_argument("--max-clearance", default="公开", help="允许发送给该模型的最高材料属性（默认“公开”）")
    p.add_argument("--thinking", choices=["on", "off"], help="思考模式开关（智谱 GLM）；不指定则按服务商默认")
    p.add_argument("--allow-egress", action="store_true", help="同时把该服务的主机加入出网白名单")
    p.add_argument("--force", action="store_true", help="覆盖同名模型条目")
    p.set_defaults(func=cmd_model_add)

    p = m.add_parser("list", help="已配置的模型、角色、密钥是否已设置、出网是否允许")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_model_list)

    p = m.add_parser("test", help="一次最小的受控调用：检查密钥、出网、型号与 JSON 模式")
    p.add_argument("name", help="模型名称（default 表示 [model]）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_model_test)

    p = m.add_parser("remote", help="查询服务商当前可用的型号（GET /models）")
    p.add_argument("name", help="模型名称（default 表示 [model]）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_model_remote)

"""插件内核：一切能力皆插件（借鉴 DeepSeek Harness / Cordis 的设计思想）。

核心性质：
* 空间可组合：插件通过 ``inject`` 声明依赖的服务；依赖缺失时插件保持“待激活”，
  不报错；服务一旦被提供，插件自动激活。
* 时间可组合：插件在其作用域内产生的副作用（注册服务、监听事件、启动资源）都登记为
  可撤销效果；插件卸载或其依赖消失时，按后进先出顺序完全撤销。

本模块是纯 Python 的小型实现，只保留公文智能体需要的部分：服务、事件、效果、插件。
模型、检索器、存储、技能、审批策略、出网网关、界面均以插件形式装配，单位可在配置中
替换或扩展任一能力，而无需修改源码。
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

log = logging.getLogger("gongwen.kernel")

Disposer = Callable[[], None]


class Disposable:
    """幂等的撤销函数。"""

    __slots__ = ("_fn", "disposed", "label")

    def __init__(self, fn: Disposer | None = None, label: str = ""):
        self._fn = fn
        self.disposed = False
        self.label = label

    def __call__(self) -> None:
        if self.disposed:
            return
        self.disposed = True
        if self._fn is not None:
            try:
                self._fn()
            except Exception:  # 撤销失败不得阻断其余撤销
                log.exception("dispose failed: %s", self.label)


class Scope:
    """插件作用域：收集效果，卸载时后进先出撤销。"""

    def __init__(self, name: str, parent: "Scope | None" = None):
        self.name = name
        self.parent = parent
        self.effects: list[Disposable] = []
        self.children: list[Scope] = []
        self.active = True
        if parent is not None:
            parent.children.append(self)

    def collect(self, fn: Disposer, label: str = "") -> Disposable:
        d = Disposable(fn, label)
        if not self.active:
            # 已卸载的作用域里再注册效果：立即撤销，避免泄漏
            d()
            return d
        self.effects.append(d)
        return d

    def dispose(self) -> None:
        if not self.active:
            return
        self.active = False
        for child in reversed(self.children):
            child.dispose()
        while self.effects:
            self.effects.pop()()
        if self.parent is not None and self in self.parent.children:
            self.parent.children.remove(self)


@dataclass
class _Listener:
    priority: int
    handler: Callable[..., Any]
    scope: Scope
    order: int


@dataclass
class PluginEntry:
    name: str
    target: Any
    config: Any
    inject: list[str]
    optional: list[str]
    scope: Scope | None = None
    instance: Any = None
    state: str = "pending"  # pending/active/failed/disposed
    error: str = ""
    owner: Scope | None = None
    deps_snapshot: dict[str, int] = field(default_factory=dict)


class _Registry:
    """在整个上下文树中共享的注册表。"""

    def __init__(self) -> None:
        self.services: dict[str, Any] = {}
        self.service_owner: dict[str, Scope] = {}
        self.service_version: dict[str, int] = {}
        self.listeners: dict[str, list[_Listener]] = {}
        self.plugins: list[PluginEntry] = []
        self.order = 0


class Context:
    """作用域化的上下文视图。插件拿到的是绑定到自身作用域的 Context。"""

    def __init__(self, registry: _Registry | None = None, scope: Scope | None = None):
        self._reg = registry or _Registry()
        self.scope = scope or Scope("root")

    # ------------------------------------------------------------ 作用域
    def fork(self, name: str) -> "Context":
        return Context(self._reg, Scope(name, self.scope))

    def effect(self, setup: Callable[[], Disposer | None], label: str = "") -> Disposable:
        disposer = setup()
        return self.scope.collect(disposer or (lambda: None), label or getattr(setup, "__name__", "effect"))

    # ------------------------------------------------------------ 服务
    def provide(self, name: str, value: Any) -> Disposable:
        if name in self._reg.services:
            raise KeyError(f"服务已存在：{name}（如需替换，请先卸载原提供者）")
        self._reg.services[name] = value
        self._reg.service_owner[name] = self.scope
        self._reg.service_version[name] = self._reg.service_version.get(name, 0) + 1
        log.debug("provide %s by %s", name, self.scope.name)

        def _remove() -> None:
            if self._reg.services.get(name) is value:
                del self._reg.services[name]
                self._reg.service_owner.pop(name, None)
                self._on_service_removed(name)

        d = self.scope.collect(_remove, f"service:{name}")
        self._activate_pending()
        return d

    def get(self, name: str) -> Any:
        try:
            return self._reg.services[name]
        except KeyError:
            raise KeyError(f"服务不可用：{name}") from None

    def maybe(self, name: str, default: Any = None) -> Any:
        return self._reg.services.get(name, default)

    def has(self, name: str) -> bool:
        return name in self._reg.services

    def services(self) -> list[str]:
        return sorted(self._reg.services)

    def __getitem__(self, name: str) -> Any:
        return self.get(name)

    # ------------------------------------------------------------ 事件
    def on(self, event: str, handler: Callable[..., Any], priority: int = 0) -> Disposable:
        self._reg.order += 1
        lst = self._reg.listeners.setdefault(event, [])
        entry = _Listener(priority, handler, self.scope, self._reg.order)
        lst.append(entry)
        lst.sort(key=lambda x: (-x.priority, x.order))

        def _off() -> None:
            if entry in lst:
                lst.remove(entry)

        return self.scope.collect(_off, f"listener:{event}")

    def emit(self, event: str, *args: Any, **kwargs: Any) -> list[Any]:
        results = []
        for lis in list(self._reg.listeners.get(event, [])):
            if lis.scope.active:
                results.append(lis.handler(*args, **kwargs))
        return results

    def bail(self, event: str, *args: Any, **kwargs: Any) -> Any:
        """依次调用监听者，返回第一个非 None 结果。"""
        return self.bail_if(lambda r: r is not None, event, *args, **kwargs)

    def bail_if(self, accept: Callable[[Any], bool], event: str, *args: Any, **kwargs: Any) -> Any:
        """依次调用监听者，返回第一个满足 accept 的结果（用于可阻断的钩子：只有阻断结果才中断，
        前面的监听者返回放行值时不会遮盖后面监听者的阻断）。"""
        for lis in list(self._reg.listeners.get(event, [])):
            if not lis.scope.active:
                continue
            r = lis.handler(*args, **kwargs)
            if accept(r):
                return r
        return None

    def listeners(self, event: str) -> int:
        return len(self._reg.listeners.get(event, []))

    # ------------------------------------------------------------ 插件
    def plugin(self, target: Any, config: Any = None, name: str | None = None) -> PluginEntry:
        pname = name or getattr(target, "name", None) or getattr(target, "__name__", "plugin")
        entry = PluginEntry(
            name=pname,
            target=target,
            config=config,
            inject=list(getattr(target, "inject", []) or []),
            optional=list(getattr(target, "optional", []) or []),
            owner=self.scope,
        )
        self._reg.plugins.append(entry)
        self.scope.collect(lambda: self._unload(entry), f"plugin:{pname}")
        self._try_activate(entry)
        return entry

    def plugin_states(self) -> dict[str, str]:
        return {p.name: p.state for p in self._reg.plugins}

    def unload(self, entry: PluginEntry) -> None:
        self._unload(entry)

    # ------------------------------------------------------------ 内部
    def _deps_ready(self, entry: PluginEntry) -> bool:
        return all(d in self._reg.services for d in entry.inject)

    def _try_activate(self, entry: PluginEntry) -> None:
        if entry.state != "pending" or not self._deps_ready(entry):
            return
        owner = entry.owner or self.scope
        if not owner.active:
            entry.state = "disposed"
            return
        scope = Scope(f"plugin:{entry.name}", owner)
        ctx = Context(self._reg, scope)
        entry.scope = scope
        entry.state = "active"
        entry.deps_snapshot = {d: self._reg.service_version.get(d, 0) for d in entry.inject}
        target = entry.target
        try:
            config = _coerce_config(target, entry.config)
            if inspect.isclass(target):
                inst = target(ctx, config)
                entry.instance = inst
                if hasattr(inst, "dispose"):
                    scope.collect(inst.dispose, f"instance:{entry.name}")
            elif hasattr(target, "apply"):
                entry.instance = target.apply(ctx, config)
            else:
                entry.instance = target(ctx, config)
        except Exception as exc:  # 激活失败要可见
            log.exception("plugin %s failed", entry.name)
            entry.state = "failed"
            entry.error = f"{type(exc).__name__}: {exc}"
            scope.dispose()

    def _activate_pending(self) -> None:
        changed = True
        while changed:
            changed = False
            for entry in list(self._reg.plugins):
                if entry.state == "pending" and self._deps_ready(entry):
                    self._try_activate(entry)
                    changed = True

    def _on_service_removed(self, name: str) -> None:
        for entry in list(self._reg.plugins):
            if entry.state == "active" and name in entry.inject:
                if entry.scope is not None:
                    entry.scope.dispose()
                entry.scope = None
                entry.instance = None
                entry.state = "pending"
        self._activate_pending()

    def _unload(self, entry: PluginEntry) -> None:
        if entry.scope is not None:
            entry.scope.dispose()
        entry.scope = None
        entry.instance = None
        entry.state = "disposed"
        if entry in self._reg.plugins:
            self._reg.plugins.remove(entry)


def _coerce_config(target: Any, config: Any) -> Any:
    schema = getattr(target, "Config", None)
    if schema is None:
        return config
    if config is None:
        return schema()
    if isinstance(config, schema):
        return config
    if isinstance(config, dict):
        return schema(**config)
    return config


class Plugin:
    """类形式插件的便利基类。"""

    name = "plugin"
    inject: Iterable[str] = ()
    optional: Iterable[str] = ()

    def __init__(self, ctx: Context, config: Any = None):
        self.ctx = ctx
        self.config = config

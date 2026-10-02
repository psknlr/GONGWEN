from gongwen.kernel import Context, Plugin


class ModelPlugin(Plugin):
    name = "model"

    def __init__(self, ctx, config=None):
        super().__init__(ctx, config)
        ctx.provide("model", {"kind": "fake"})


class DrafterPlugin(Plugin):
    name = "drafter"
    inject = ["model"]
    activations = 0
    disposals = 0

    def __init__(self, ctx, config=None):
        super().__init__(ctx, config)
        DrafterPlugin.activations += 1
        ctx.provide("drafter", self)
        ctx.on("draft", lambda x: f"drafted:{x}")

    def dispose(self):
        DrafterPlugin.disposals += 1


def test_plugin_waits_for_dependency_then_activates():
    DrafterPlugin.activations = DrafterPlugin.disposals = 0
    root = Context()
    entry = root.plugin(DrafterPlugin)
    assert entry.state == "pending"
    assert not root.has("drafter")
    model_entry = root.plugin(ModelPlugin)
    assert entry.state == "active"
    assert root.has("drafter")
    assert root.emit("draft", "x") == ["drafted:x"]
    # 卸载依赖后，依赖它的插件完全撤销其效果并回到待激活
    root.unload(model_entry)
    assert entry.state == "pending"
    assert not root.has("drafter")
    assert root.emit("draft", "x") == []
    assert DrafterPlugin.disposals == 1
    # 重新提供依赖后再次激活
    root.plugin(ModelPlugin)
    assert entry.state == "active"
    assert DrafterPlugin.activations == 2


def test_scope_dispose_is_lifo_and_complete():
    order = []
    root = Context()
    child = root.fork("child")
    child.effect(lambda: (lambda: order.append("a")))
    child.effect(lambda: (lambda: order.append("b")))
    child.provide("svc", 1)
    assert root.get("svc") == 1
    child.scope.dispose()
    assert order == ["b", "a"]
    assert not root.has("svc")


def test_bail_returns_first_non_none_by_priority():
    root = Context()
    root.on("check", lambda: None)
    root.on("check", lambda: "low", priority=0)
    root.on("check", lambda: "high", priority=10)
    assert root.bail("check") == "high"


def test_failed_plugin_is_visible():
    def broken(ctx, config):
        raise RuntimeError("boom")

    root = Context()
    entry = root.plugin(broken, name="broken")
    assert entry.state == "failed"
    assert "boom" in entry.error
